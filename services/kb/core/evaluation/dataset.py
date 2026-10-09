# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""评测集模型（FR-KB-016 / SR-KB-016 §4、§6.6，迭代 0.3）。

人工出题测试集的纯标准库模型层（无框架依赖，CLI 门禁可独立引用）：
- EvalCase：一条人工用例 = query + 期望命中块集合 + 维度标签（六库之一 + 自定义维度）；
- EvalDataset：版本化评测集（name + version 唯一），重复 case_id 构造期即拒绝，
  ≥100 条的容量校验（SRS §5.3 口径）由 validate_capacity/freeze 显式触发——
  允许小规模草稿集存在，冻结（对外跑分）才强制规模；
- JSON 文件加载/保存（eval/sets/*.json 快照，版本化评审载体；DB 为源的
  eval_set 表随数据访问层迭代接入，本迭代以文件形态承接同一契约）。

与 SR-KB-016 §4 的映射：eval_set → EvalDataset(name/version/status)，eval_case →
EvalCase；checksum 防漂移与 DB↔git 双向同步（sync.py）不在本迭代范围。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 六大知识库枚举（03-数据库设计 §4.2 kb_type 取值；与 api/schemas.KB_TYPE_ENUM 同源。
#: 本包保持零框架依赖（CLI 独立运行），故独立声明同一取值集，改动须两处同步）
KB_TYPES: tuple[str, ...] = (
    "product",
    "customer",
    "skill",
    "process",
    "negative_case",
    "training",
)

#: 冻结最小用例数（SRS §5.3「≥100 条」；KB_EVAL_SET_MIN_CASES 默认值，SR-KB-016 §7）
MIN_FROZEN_CASES: int = 100

#: 评测集状态机（SR-KB-016 §4 eval_set.status：draft → reviewing → frozen → retired）
DATASET_STATUS_FLOW: tuple[str, ...] = ("draft", "reviewing", "frozen", "retired")

#: query 长度上限（与统一检索 API 契约 KB_API_QUERY_MAX_LEN 同源，冻结 v1 口径）
QUERY_MAX_LENGTH: int = 512


class DatasetError(ValueError):
    """评测集构造/校验错误基类（fail-closed：调用方捕获后拒绝操作并回显原因）。"""


class DuplicateCaseIdError(DatasetError):
    """重复 case_id：同一评测集内用例编号必须唯一（SR-KB-016 §4 PRIMARY KEY 语义）。"""


class DatasetTooSmallError(DatasetError):
    """容量不足：冻结/正式跑分要求 ≥100 条（SRS §5.3；E_EVAL_SET_TOO_SMALL 同义）。"""


class DatasetStateError(DatasetError):
    """状态机违规：如对已冻结集合重复冻结（冻结集只读，修改须派生新版本）。"""


def _require_str(value: Any, field_name: str, *, max_length: int | None = None) -> str:
    """校验非空字符串字段（可选长度上限），非法即抛 DatasetError。"""
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{field_name} 必须为非空字符串: {value!r}")
    if max_length is not None and len(value) > max_length:
        raise DatasetError(f"{field_name} 长度 {len(value)} 超过上限 {max_length}")
    return value


def _require_int(value: Any, field_name: str) -> int:
    """校验整数（拒绝 bool 与浮点形态），非法即抛 DatasetError。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatasetError(f"{field_name} 必须为整数: {value!r}")
    return value


@dataclass(frozen=True)
class EvalCase:
    """单条评测用例：人工出题的「问法 + 标准答案 + 维度标签」。

    字段口径（SR-KB-016 §4 eval_case）：
    - expected_chunk_ids：期望 Top5 至少命中其一（多块 = 多答案/同义块）；
    - kb_type：六库之一（评测按库隔离发起检索）；
    - dimensions：自定义维度标签（如 {"scene": "询价", "sku": "ST-3200"}），
      参与分维度聚合与归因下钻，不参与检索过滤；
    - filters：可选的结构化过滤（统一检索 API 五维白名单），直传 search_fn；
      缺省 None（绝大多数用例考察自然语言检索本身，不带过滤）。
    """

    case_id: str
    query: str
    expected_chunk_ids: tuple[int, ...]
    kb_type: str
    dimensions: Mapping[str, str] = field(default_factory=dict)
    filters: Mapping[str, Sequence[str]] | None = None

    def __post_init__(self) -> None:
        """构造期完整性校验（fail-closed）：编号/问法/答案/库类型逐项校验。"""
        _require_str(self.case_id, "case_id")
        _require_str(self.query, "query", max_length=QUERY_MAX_LENGTH)
        if not self.expected_chunk_ids:
            raise DatasetError(f"用例 {self.case_id} 的 expected_chunk_ids 不能为空（每条须含标准答案）")
        for chunk_id in self.expected_chunk_ids:
            _require_int(chunk_id, f"用例 {self.case_id} 的 expected_chunk_ids 元素")
        if self.kb_type not in KB_TYPES:
            raise DatasetError(
                f"用例 {self.case_id} 的 kb_type={self.kb_type!r} 不在六大库枚举 {list(KB_TYPES)}"
            )
        for key, value in self.dimensions.items():
            _require_str(key, f"用例 {self.case_id} 的维度名")
            _require_str(value, f"用例 {self.case_id} 的维度 {key} 取值")

    def dimension_groups(self) -> list[str]:
        """分维度聚合的分组键序列：kb_type 组 + 各自定义维度组（键序确定，保证聚合稳定）。

        形如 ["kb_type=product", "scene=询价", "sku=ST-3200"]——kb_type 恒为首键，
        自定义维度按名排序，同一评测集在任何执行序下分组结果一致。
        """
        groups = [f"kb_type={self.kb_type}"]
        groups.extend(f"{key}={self.dimensions[key]}" for key in sorted(self.dimensions))
        return groups

    def dimension_label(self) -> str:
        """维度标签的单行展示串（报告明细列）：分号连接的键值对。"""
        return "; ".join(self.dimension_groups())

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（JSON 快照的用例行；写全可选字段，保证 round-trip 无损）。"""
        data: dict[str, Any] = {
            "case_id": self.case_id,
            "query": self.query,
            "expected_chunk_ids": list(self.expected_chunk_ids),
            "kb_type": self.kb_type,
        }
        if self.dimensions:
            data["dimensions"] = dict(self.dimensions)
        if self.filters is not None:
            data["filters"] = {key: list(values) for key, values in self.filters.items()}
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvalCase":
        """反序列化（fail-closed）：必填键齐全、未知键拒绝（防快照漂移/字段拼错静默吞没）。"""
        required = ("case_id", "query", "expected_chunk_ids", "kb_type")
        missing = [key for key in required if key not in data]
        if missing:
            raise DatasetError(f"用例缺少必填字段: {missing}")
        unknown = set(data) - set(required) - {"dimensions", "filters"}
        if unknown:
            raise DatasetError(f"用例含未知字段: {sorted(unknown)}")
        dimensions = data.get("dimensions") or {}
        if not isinstance(dimensions, Mapping):
            raise DatasetError(f"用例 {data.get('case_id')!r} 的 dimensions 须为对象")
        filters = data.get("filters")
        if filters is not None and not isinstance(filters, Mapping):
            raise DatasetError(f"用例 {data.get('case_id')!r} 的 filters 须为对象")
        return cls(
            case_id=data["case_id"],
            query=data["query"],
            expected_chunk_ids=tuple(data["expected_chunk_ids"]),
            kb_type=data["kb_type"],
            dimensions={str(key): str(value) for key, value in dimensions.items()},
            filters=None if filters is None else {str(k): list(v) for k, v in filters.items()},
        )


@dataclass
class EvalDataset:
    """版本化评测集：同名递增 version（如 frozen_L1 v3），冻结后只读。

    规模口径：构造不强制 ≥100（允许小规模草稿/冒烟集），freeze() 与
    validate_capacity() 显式执行 SRS「≥100 条」校验——跑分验收用冻结集，
    回归联调可用任意子集，两类场景共用同一模型。
    """

    name: str
    version: int
    cases: list[EvalCase]
    status: str = "draft"

    def __post_init__(self) -> None:
        """构造期校验：命名/版本/状态/规模非空 + case_id 全局唯一（fail-closed）。"""
        _require_str(self.name, "评测集 name")
        if self.version < 1:
            raise DatasetError(f"评测集 {self.name} 的 version 须 ≥1: {self.version!r}")
        if self.status not in DATASET_STATUS_FLOW:
            raise DatasetError(
                f"评测集 {self.name} 的 status={self.status!r} 不在 {list(DATASET_STATUS_FLOW)}"
            )
        if not self.cases:
            raise DatasetError(f"评测集 {self.name} 至少需要 1 条用例（空集不可构造）")
        seen: set[str] = set()
        for case in self.cases:
            if case.case_id in seen:
                raise DuplicateCaseIdError(
                    f"评测集 {self.name} 存在重复 case_id: {case.case_id!r}（用例编号必须唯一）"
                )
            seen.add(case.case_id)

    # ---- 规模与状态机 ----

    @property
    def case_count(self) -> int:
        """用例条数（冻结校验的分母）。"""
        return len(self.cases)

    def validate_capacity(self, min_cases: int = MIN_FROZEN_CASES) -> None:
        """容量校验：低于 min_cases 抛 DatasetTooSmallError（SRS ≥100 条口径）。"""
        if self.case_count < min_cases:
            raise DatasetTooSmallError(
                f"评测集 {self.name} 仅 {self.case_count} 条用例，低于最小规模 {min_cases} 条"
                "（SRS §5.3 要求人工测试集 ≥100 条）"
            )

    def freeze(self, min_cases: int = MIN_FROZEN_CASES) -> "EvalDataset":
        """冻结：容量校验通过后置 status=frozen（冻结集只读，再冻结即状态机违规）。

        冻结校验「99 条 → 拒绝」对应 SR-KB-016 §5 错误码 E_EVAL_SET_TOO_SMALL。
        """
        if self.status == "frozen":
            raise DatasetStateError(
                f"评测集 {self.name} v{self.version} 已冻结（只读）；修改须派生新版本"
            )
        if self.status == "retired":
            raise DatasetStateError(f"评测集 {self.name} v{self.version} 已退役，不可冻结")
        self.validate_capacity(min_cases)
        self.status = "frozen"
        return self

    # ---- 序列化（JSON 文件快照）----

    def to_dict(self) -> dict[str, Any]:
        """整体序列化（JSON 快照结构：name/version/status/cases）。"""
        return {
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "cases": [case.to_dict() for case in self.cases],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvalDataset":
        """反序列化（fail-closed）：必填键齐全、未知键拒绝，用例行逐一校验。"""
        required = ("name", "version", "cases")
        missing = [key for key in required if key not in data]
        if missing:
            raise DatasetError(f"评测集缺少必填字段: {missing}")
        unknown = set(data) - set(required) - {"status"}
        if unknown:
            raise DatasetError(f"评测集含未知字段: {sorted(unknown)}")
        if not isinstance(data["cases"], Sequence) or isinstance(data["cases"], (str, bytes)):
            raise DatasetError("评测集 cases 须为用例数组")
        return cls(
            name=data["name"],
            version=_require_int(data["version"], "评测集 version"),
            cases=[EvalCase.from_dict(row) for row in data["cases"]],
            status=data.get("status", "draft"),
        )

    def save(self, path: str | Path) -> Path:
        """保存为 JSON 文件（UTF-8、缩进 2、ensure_ascii=False 保中文可读）。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "EvalDataset":
        """从 JSON 文件加载（文件缺失/JSON 解析失败/结构非法统一抛 DatasetError）。"""
        source = Path(path)
        try:
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise DatasetError(f"评测集文件不可读: {source}（{exc}）") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"评测集文件不是合法 JSON: {source}（{exc}）") from exc
        if not isinstance(data, Mapping):
            raise DatasetError(f"评测集文件顶层须为 JSON 对象: {source}")
        return cls.from_dict(data)
