# 知识产权归甲方所有（COM-IP-01）
"""Milvus Collection 建表契约（FR-KB-014，对齐 02-设计文档/03-数据库设计 §4.2）。

集中定义单 Collection `knowledge_chunks` 的字段常量、索引参数与分区策略说明，
作为 MilvusStore（milvus_impl.py）建表 / 写入 / 检索的唯一定义源。本模块不依赖
pymilvus（重型依赖未安装的环境——如本仓库 CI——也可导入并测试）；真正调用
pymilvus 建 DDL 的逻辑在 milvus_impl.py 的 import 守护之后。

字段与标量维度表（与 03-数据库设计 §4.2 逐字段一致，顺序同 SR-KB-014 §4）：

| 字段            | 类型                   | 说明                                    |
|-----------------|------------------------|-----------------------------------------|
| chunk_id        | Int64（主键）          | = PG knowledge_chunk.id                 |
| tenant_id       | Int64                  | Partition Key（P5 生效；P0-P4 恒 1）    |
| kb_type         | VarChar(32)            | 六大知识库 + 标量过滤                    |
| dense_vec       | FloatVector(1024)      | BGE-M3 稠密向量                         |
| sparse_vec      | SparseFloatVector      | 稀疏关键词召回路                        |
| sku / scene_tag / process_node / reason_type / customer_ref | VarChar(64) | 高频过滤维度 |
| item_status     | VarChar(16)            | 仅写入 published 状态                   |

索引（SR-KB-014 §6.1 / 03 §4.2）：
- dense_vec：HNSW（M=16, efConstruction=200，metric=IP——BGE-M3 向量已归一化，IP 等价 cosine）；
- sparse_vec：SPARSE_INVERTED_INDEX（metric=IP）；
- 标量过滤字段：INVERTED（低基数字段可选 TRIE）。

分区策略（SR-KB-014 §6.2）：
- P0-P4：kb_type 手建六分区——写入按 kb_type 路由，查询 `kb_type in [...]` 触发分区裁剪，
  六库互为独立故障域；
- P5：切换为 tenant_id Partition Key（64 哈希分区自动路由）。Milvus 单 Collection 仅允许
  一个 Partition Key，无法与 kb_type 手建分区并存，故 P5 落地为「切换」而非「叠加」：
  kb_type 降为标量过滤字段（INVERTED 承担），租户隔离改由 Partition Key + 中间件强制注入
  承担（NFR-SEC-07）；切换涉及 Collection 重建，走变更管理取号（CD/CR）并隔离环境验证。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import DENSE_DIM, DEFAULT_TENANT_ID, ITEM_STATUS_PUBLISHED, KB_TYPES

if TYPE_CHECKING:  # 仅类型标注使用，避免运行时依赖环（base 为契约叶子模块）
    from .base import VectorRecord

# ── Collection 与字段名常量（Milvus 物理名；VectorRecord 侧字段名见 base.py）──
COLLECTION_NAME: str = "knowledge_chunks"
FIELD_CHUNK_ID: str = "chunk_id"
FIELD_TENANT_ID: str = "tenant_id"
FIELD_KB_TYPE: str = "kb_type"
FIELD_DENSE_VEC: str = "dense_vec"
FIELD_SPARSE_VEC: str = "sparse_vec"
FIELD_SKU: str = "sku"
FIELD_SCENE_TAG: str = "scene_tag"
FIELD_PROCESS_NODE: str = "process_node"
FIELD_REASON_TYPE: str = "reason_type"
FIELD_CUSTOMER_REF: str = "customer_ref"
FIELD_ITEM_STATUS: str = "item_status"

#: 建表字段全集（顺序即建表顺序，与 03 §4.2 / SR-KB-014 §4 字段表一致）
ALL_FIELDS: tuple[str, ...] = (
    FIELD_CHUNK_ID,
    FIELD_TENANT_ID,
    FIELD_KB_TYPE,
    FIELD_DENSE_VEC,
    FIELD_SPARSE_VEC,
    FIELD_SKU,
    FIELD_SCENE_TAG,
    FIELD_PROCESS_NODE,
    FIELD_REASON_TYPE,
    FIELD_CUSTOMER_REF,
    FIELD_ITEM_STATUS,
)
#: 五个高频过滤维度（对应 FR-KB-001~005）
SCALAR_VARCHAR_FIELDS: tuple[str, ...] = (
    FIELD_SKU,
    FIELD_SCENE_TAG,
    FIELD_PROCESS_NODE,
    FIELD_REASON_TYPE,
    FIELD_CUSTOMER_REF,
)
#: 建 INVERTED 索引的标量字段（kb_type 在 P5 降级后由该索引承担过滤）
SCALAR_INVERTED_FIELDS: tuple[str, ...] = (
    FIELD_KB_TYPE,
    *SCALAR_VARCHAR_FIELDS,
    FIELD_ITEM_STATUS,
)
#: VarChar 长度上限（与 03 §4.2 维度表一致；base.py 校验常量同源，单测守护两处一致）
VARCHAR_LIMITS: dict[str, int] = {
    FIELD_KB_TYPE: 32,
    FIELD_SKU: 64,
    FIELD_SCENE_TAG: 64,
    FIELD_PROCESS_NODE: 64,
    FIELD_REASON_TYPE: 64,
    FIELD_CUSTOMER_REF: 64,
    FIELD_ITEM_STATUS: 16,
}

# ── 索引与检索参数（SR-KB-014 §6.1/§6.3/§7 默认值；运行期可由 app_config 覆盖）──
HNSW_M: int = 16
HNSW_EF_CONSTRUCTION: int = 200
DEFAULT_SEARCH_EF: int = 128
DEFAULT_SPARSE_DROP_RATIO_SEARCH: float = 0.2
DEFAULT_RRF_K: int = 60
DENSE_INDEX_SPEC: dict[str, object] = {
    "field_name": FIELD_DENSE_VEC,
    "index_type": "HNSW",
    "metric_type": "IP",  # BGE-M3 向量已归一化，IP 等价 cosine
    "params": {"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
}
SPARSE_INDEX_SPEC: dict[str, object] = {
    "field_name": FIELD_SPARSE_VEC,
    "index_type": "SPARSE_INVERTED_INDEX",
    "metric_type": "IP",
}
SCALAR_INDEX_TYPE: str = "INVERTED"
#: 有界一致性：增量写入 ≤3s 可见（FR-KB-017 即插即查的可见性承诺）
CONSISTENCY_LEVEL: str = "Bounded"
#: hybrid_search 回传字段（SR-KB-014 §6.3，主键 + 诊断用标量）
SEARCH_OUTPUT_FIELDS: tuple[str, ...] = (
    FIELD_CHUNK_ID,
    FIELD_KB_TYPE,
    FIELD_SKU,
    FIELD_SCENE_TAG,
    FIELD_ITEM_STATUS,
)

# ── 分区策略常量（SR-KB-014 §6.2）──
PARTITION_MODE_KB_TYPE: str = "kb_type"  # P0-P4：六大知识库各一分区
PARTITION_MODE_TENANT: str = "tenant"  # P5：tenant_id Partition Key
TENANT_PARTITION_NUM: int = 64  # P5 租户哈希分区数


@dataclass(frozen=True)
class FieldSpec:
    """单个 Milvus 字段的建表规格（与 03-数据库设计 §4.2 字段表逐行对应）。"""

    name: str
    milvus_type: str  # 展示用类型名（INT64 / VARCHAR / FLOAT_VECTOR / SPARSE_FLOAT_VECTOR）
    description: str
    is_primary: bool = False
    is_partition_key: bool = False  # tenant_id 专属：P5 Partition Key 注记
    max_length: int | None = None  # VARCHAR 专属
    dim: int | None = None  # FLOAT_VECTOR 专属


#: 建表字段规格表（顺序与 03 §4.2 / SR-KB-014 §4 一致；单测据此守护逐字段对齐）
FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(FIELD_CHUNK_ID, "INT64", "= PG knowledge_chunk.id（主键）", is_primary=True),
    FieldSpec(
        FIELD_TENANT_ID,
        "INT64",
        "Partition Key（P5 生效；P0-P4 恒 1）",
        is_partition_key=True,
    ),
    FieldSpec(FIELD_KB_TYPE, "VARCHAR", "六大知识库枚举 + 标量过滤（P0-P4 兼作分区名）", max_length=32),
    FieldSpec(FIELD_DENSE_VEC, "FLOAT_VECTOR", "BGE-M3 稠密向量", dim=DENSE_DIM),
    FieldSpec(FIELD_SPARSE_VEC, "SPARSE_FLOAT_VECTOR", "稀疏关键词召回路（BGE-M3 learned sparse）"),
    FieldSpec(FIELD_SKU, "VARCHAR", "高频过滤维度（FR-KB-001 产品库）", max_length=64),
    FieldSpec(FIELD_SCENE_TAG, "VARCHAR", "高频过滤维度（FR-KB-002 场景）", max_length=64),
    FieldSpec(FIELD_PROCESS_NODE, "VARCHAR", "高频过滤维度（FR-KB-003 流程）", max_length=64),
    FieldSpec(FIELD_REASON_TYPE, "VARCHAR", "高频过滤维度（FR-KB-004 拒因）", max_length=64),
    FieldSpec(FIELD_CUSTOMER_REF, "VARCHAR", "高频过滤维度（FR-KB-005 客户背景）", max_length=64),
    FieldSpec(FIELD_ITEM_STATUS, "VARCHAR", "仅写入 published 状态", max_length=16),
)


def record_to_milvus_row(record: VectorRecord) -> dict[str, object]:
    """VectorRecord → Milvus 行字典（字段名对齐 ALL_FIELDS；稀疏下标转字符串键）。

    pymilvus 的稀疏向量行格式接受 {下标字符串: 权重}，此处统一转换；
    dense 直接透传 1024 维列表。本函数为纯映射，供 MilvusStore.upsert 使用。
    """
    return {
        FIELD_CHUNK_ID: int(record.chunk_id),
        FIELD_TENANT_ID: int(record.tenant_id),
        FIELD_KB_TYPE: record.kb_type,
        FIELD_DENSE_VEC: list(record.dense),
        FIELD_SPARSE_VEC: {str(index): float(weight) for index, weight in record.sparse.items()},
        FIELD_SKU: record.sku,
        FIELD_SCENE_TAG: record.scene_tag,
        FIELD_PROCESS_NODE: record.process_node,
        FIELD_REASON_TYPE: record.reason_type,
        FIELD_CUSTOMER_REF: record.customer_ref,
        FIELD_ITEM_STATUS: record.item_status,
    }


__all__ = [
    "COLLECTION_NAME",
    "FIELD_CHUNK_ID",
    "FIELD_TENANT_ID",
    "FIELD_KB_TYPE",
    "FIELD_DENSE_VEC",
    "FIELD_SPARSE_VEC",
    "FIELD_SKU",
    "FIELD_SCENE_TAG",
    "FIELD_PROCESS_NODE",
    "FIELD_REASON_TYPE",
    "FIELD_CUSTOMER_REF",
    "FIELD_ITEM_STATUS",
    "ALL_FIELDS",
    "SCALAR_VARCHAR_FIELDS",
    "SCALAR_INVERTED_FIELDS",
    "VARCHAR_LIMITS",
    "HNSW_M",
    "HNSW_EF_CONSTRUCTION",
    "DEFAULT_SEARCH_EF",
    "DEFAULT_SPARSE_DROP_RATIO_SEARCH",
    "DEFAULT_RRF_K",
    "DENSE_INDEX_SPEC",
    "SPARSE_INDEX_SPEC",
    "SCALAR_INDEX_TYPE",
    "CONSISTENCY_LEVEL",
    "SEARCH_OUTPUT_FIELDS",
    "PARTITION_MODE_KB_TYPE",
    "PARTITION_MODE_TENANT",
    "TENANT_PARTITION_NUM",
    "KB_TYPES",
    "DENSE_DIM",
    "DEFAULT_TENANT_ID",
    "ITEM_STATUS_PUBLISHED",
    "FieldSpec",
    "FIELD_SPECS",
    "record_to_milvus_row",
]
