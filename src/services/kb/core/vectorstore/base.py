# 知识产权归甲方所有（COM-IP-01）
"""向量存储抽象契约（FR-KB-014 向量数据库，迭代 0.2）。

定义检索（FR-KB-015 / FR-KB-030）与增量更新（FR-KB-017）模块共同依赖的存储层接口：
- VectorRecord：入库行契约，字段与 02-设计文档/03-数据库设计 §4.2 逐字段对齐；
- SearchHit：检索命中结果（chunk_id + 融合得分）；
- VectorStore：存储后端协议（upsert / delete / hybrid_search），内存与 Milvus 双实现。

hybrid_search 的 expr 参数为简化 DSL 字符串：
- 词法：`字段 in ('值1','值2')` 多值成员判断 / `字段 = '值'` 单值等值（单引号包裹，不支持转义）；
- 组合：仅小写 ` and ` 关键字连接，不支持 or / 括号 / 大小写变体；
- 字段白名单：kb_type / sku / scene_tag / process_node / reason_type / customer_ref / item_status，
  白名单之外的字段一律拒绝（fail-closed，收敛表达式注入面）；
- tenant_id 不入 DSL：P5 多租户过滤由检索中间件强制注入（SR-KB-014 §6.2），存储层不重复承担。

parse_filter_expr() 是该 DSL 的唯一解析实现，供内存实现（谓词求值）与 Milvus 实现
（expr 方言翻译）共用，保证两端过滤语义一致。

依据文档：
- docs/07-实现方案/KB/SR-KB-014-向量数据库.md §4（数据模型）、§6.3（混合检索）
- docs/02-设计文档/03-数据库设计.md §4.2（字段与标量维度表，逐字段一致）
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

# ── 契约常量：collection_schema.py 的 Milvus 建表定义与此同源（单测守护两处一致）──
#: 六大知识库枚举（03-数据库设计 §4.2 kb_type 取值；同时是 P0-P4 的六个分区名）
# CR A1 修复：枚举权威下沉 foundation.core.kbtypes（跨服务共享单点），此处 re-export
from foundation.core.kbtypes import KB_TYPES  # noqa: E402  (保持既有导入路径兼容)
#: BGE-M3 稠密向量维度（03-数据库设计 §4.2 FloatVector(1024)）
DENSE_DIM: int = 1024
#: 默认租户编号（P0-P4 单租户恒为 1，SR-KB-014 §4）
DEFAULT_TENANT_ID: int = 1
#: 已发布状态（03-数据库设计 §4.2：仅索引 published 状态）
ITEM_STATUS_PUBLISHED: str = "published"

# VarChar 长度上限（与 03-数据库设计 §4.2 维度表一致；collection_schema.VARCHAR_LIMITS 同源）
KB_TYPE_MAX_LENGTH: int = 32
FILTER_VALUE_MAX_LENGTH: int = 64
ITEM_STATUS_MAX_LENGTH: int = 16

#: expr DSL 允许过滤的标量字段白名单（与 VectorRecord 标量字段一一对应）
FILTERABLE_FIELDS: frozenset[str] = frozenset(
    {"kb_type", "sku", "scene_tag", "process_node", "reason_type", "customer_ref", "item_status"}
)


class VectorRecord(BaseModel):
    """向量入库行（Milvus knowledge_chunks 的 Python 侧投影，字段与 03 §4.2 逐字段一致）。

    对应关系：chunk_id/tenant_id/kb_type/五个过滤维度/item_status 为标量列，
    dense → dense_vec（FloatVector(1024)），sparse → sparse_vec（SparseFloatVector）。
    frozen=True：入库后不可变，保证内存实现「快照读」的线程安全前提。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: int  # 主键 = PG knowledge_chunk.id
    tenant_id: int = DEFAULT_TENANT_ID  # Partition Key（P5 生效；P0-P4 恒 1）
    kb_type: str  # 六大知识库枚举
    dense: list[float]  # BGE-M3 稠密向量（固定 1024 维）
    sparse: dict[int, float]  # BGE-M3 learned sparse 稀疏向量（下标 → 权重）
    sku: str = ""  # 高频过滤维度（FR-KB-001 产品库）
    scene_tag: str = ""  # 场景标签（FR-KB-002）
    process_node: str = ""  # 流程节点（FR-KB-003）
    reason_type: str = ""  # 拒因类型（FR-KB-004）
    customer_ref: str = ""  # 客户背景引用（FR-KB-005）
    item_status: str = ITEM_STATUS_PUBLISHED  # 非 published 条目入库但不参与检索

    @field_validator("chunk_id")
    @classmethod
    def _chunk_id_positive(cls, value: int) -> int:
        """主键必须为正整数（PG 自增主键从 1 起）。"""
        if value < 1:
            raise ValueError(f"chunk_id 必须为正整数（=PG knowledge_chunk.id），收到：{value!r}")
        return value

    @field_validator("tenant_id")
    @classmethod
    def _tenant_id_positive(cls, value: int) -> int:
        """租户编号必须为正整数（P0-P4 恒 1）。"""
        if value < 1:
            raise ValueError(f"tenant_id 必须为正整数（P0-P4 恒 1），收到：{value!r}")
        return value

    @field_validator("kb_type")
    @classmethod
    def _kb_type_in_enum(cls, value: str) -> str:
        """kb_type 必须命中六大知识库枚举（同时是分区名，白名单外拒绝）。"""
        if value not in KB_TYPES:
            raise ValueError(f"kb_type 必须为六大知识库枚举之一 {KB_TYPES}，收到：{value!r}")
        return value

    @field_validator("dense")
    @classmethod
    def _dense_dim_and_finite(cls, value: list[float]) -> list[float]:
        """稠密向量维度固定 1024，且不允许 NaN/Inf 分量（脏向量入库前拦截）。"""
        if len(value) != DENSE_DIM:
            raise ValueError(f"dense 维度必须为 {DENSE_DIM}（BGE-M3 稠密维度），收到 {len(value)} 维")
        if not all(math.isfinite(x) for x in value):
            raise ValueError("dense 不允许 NaN/Inf 分量")
        return value

    @field_validator("sparse")
    @classmethod
    def _sparse_keys_and_finite(cls, value: dict[int, float]) -> dict[int, float]:
        """稀疏下标必须为非负整数、权重必须有限（与 Milvus 稀疏向量约束一致）。"""
        for index, weight in value.items():
            if index < 0:
                raise ValueError(f"sparse 维度下标必须为非负整数，收到：{index!r}")
            if not math.isfinite(weight):
                raise ValueError("sparse 权重不允许 NaN/Inf")
        return value

    @field_validator("sku", "scene_tag", "process_node", "reason_type", "customer_ref")
    @classmethod
    def _filter_field_length(cls, value: str) -> str:
        """五个高频过滤维度对齐 VarChar(64)（03 §4.2），超长在建模层即拦截。"""
        if len(value) > FILTER_VALUE_MAX_LENGTH:
            raise ValueError(f"过滤维度超长（上限 {FILTER_VALUE_MAX_LENGTH} 字符）：{value[:32]!r}…")
        return value

    @field_validator("item_status")
    @classmethod
    def _item_status_length(cls, value: str) -> str:
        """item_status 对齐 VarChar(16)（03 §4.2）。"""
        if len(value) > ITEM_STATUS_MAX_LENGTH:
            raise ValueError(f"item_status 超长（上限 {ITEM_STATUS_MAX_LENGTH} 字符）：{value!r}")
        return value


class SearchHit(BaseModel):
    """混合检索单条命中：chunk_id + 融合得分（RRF 分数，越大越靠前）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: int
    score: float


@dataclass(frozen=True)
class FilterCondition:
    """expr DSL 解析产物：单个标量过滤条件（字段、算子、取值集合）。

    等值 `=` 与多值 `in` 统一退化为「记录字段值 ∈ 取值集合」判断，
    内存实现求值与 Milvus 方言翻译共用这一结构。
    """

    field: str  # FILTERABLE_FIELDS 之一（解析时已校验）
    op: str  # 'in' | '='（仅用于 Milvus 方言翻译区分写法）
    values: tuple[str, ...]

    def matches(self, record: VectorRecord) -> bool:
        """对该记录求值：字段当前值是否命中取值集合。"""
        return getattr(record, self.field) in self.values


# DSL 单个条件词法：字段名 +（in 多值列表 | = 单值），词尾消费 ` and ` 分隔或行尾
_TERM_PATTERN: re.Pattern[str] = re.compile(
    r"""^\s*
        (?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*
        (?:
            in\s*\(\s*(?P<values>'[^']*'(?:\s*,\s*'[^']*')*)\s*\)
          | =\s*'(?P<value>[^']*)'
        )
        (?:\s+and\s+|\s*$)
    """,
    re.VERBOSE,
)
# in 列表内取值抽取（词法已约束为逗号分隔的单引号串，findall 安全）
_VALUE_PATTERN: re.Pattern[str] = re.compile(r"'([^']*)'")


def parse_filter_expr(expr: str | None) -> list[FilterCondition]:
    """解析简化 DSL expr 为条件列表；空/None 返回空列表（无过滤）。

    解析规则见模块 docstring；任何语法偏差（or / 括号 / 双引号 / 大写 AND /
    空列表 / 悬空 and / 白名单外字段 / 尾部垃圾字符）一律抛 ValueError——
    fail-closed：宁可拒绝合法边缘写法，不可放过注入面。
    """
    if expr is None or not expr.strip():
        return []
    conditions: list[FilterCondition] = []
    rest: str = expr
    while True:
        matched = _TERM_PATTERN.match(rest)
        if matched is None:
            raise ValueError(
                "expr 含不支持的语法（仅支持「字段 in ('值',…)」与「字段 = '值'」的小写 and 组合）："
                f"{rest.strip()[:50]!r}"
            )
        field = matched.group("field")
        if field not in FILTERABLE_FIELDS:
            raise ValueError(f"expr 字段 {field!r} 不在过滤白名单 {sorted(FILTERABLE_FIELDS)} 内（fail-closed）")
        if matched.group("values") is not None:
            values = tuple(_VALUE_PATTERN.findall(matched.group("values")))
            conditions.append(FilterCondition(field=field, op="in", values=values))
        else:
            conditions.append(FilterCondition(field=field, op="=", values=(matched.group("value"),)))
        rest = rest[matched.end():]
        if not rest.strip():
            return conditions


@runtime_checkable
class VectorStore(Protocol):
    """向量存储后端协议（FR-KB-014 存储层原语，内存 / Milvus 双实现共同满足）。

    语义约定（实现方必须遵守，调用方可以依赖）：
    - upsert 按 chunk_id 幂等覆盖：重试/重放无副作用，新增/修改即插即查（FR-KB-017）；
    - delete 对不存在的 chunk_id 静默忽略（幂等）；
    - hybrid_search 至少提供 dense / sparse 一路查询向量；仅 item_status == published
      的记录可见；expr 为空时不过滤；返回按融合得分降序、同分按 chunk_id 升序的稳定排序。
    """

    def upsert(self, records: list[VectorRecord]) -> None:
        """批量写入/覆盖向量记录（按 chunk_id 幂等）。"""
        ...

    def delete(self, chunk_ids: list[int]) -> None:
        """按主键批量删除（条目下线同步删除，数据完整性 KPI）。"""
        ...

    def hybrid_search(
        self,
        dense: Sequence[float] | None = None,
        sparse: Mapping[int, float] | None = None,
        expr: str | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """混合检索：稠密 + 稀疏多路召回 → RRF 融合 → 标量过滤，截断返回 top_k 条。"""
        ...


__all__ = [
    "KB_TYPES",
    "DENSE_DIM",
    "DEFAULT_TENANT_ID",
    "ITEM_STATUS_PUBLISHED",
    "KB_TYPE_MAX_LENGTH",
    "FILTER_VALUE_MAX_LENGTH",
    "ITEM_STATUS_MAX_LENGTH",
    "FILTERABLE_FIELDS",
    "VectorRecord",
    "SearchHit",
    "FilterCondition",
    "parse_filter_expr",
    "VectorStore",
]
