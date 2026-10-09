# 知识产权归甲方所有（COM-IP-01）
"""向量存储包（FR-KB-014 向量数据库，SR-KB-014 §3 代码落点的迭代 0.2 子集）。

模块划分（与 SR-KB-014 §3 的映射关系——迭代 0.2 先落抽象层 + 内存/Milvus 双后端）：
- base              契约层：VectorRecord / SearchHit / VectorStore 协议 + expr DSL 解析
                    （对应 SR §5 内部函数契约的存储层原语）；
- collection_schema Milvus 建表契约：字段/索引/分区常量（对应 SR §3 schema.py，
                    03-数据库设计 §4.2 逐字段对齐；不含 pymilvus 依赖）；
- inmemory_impl     内存后端：稠密余弦 + 稀疏点积 + 标量过滤 + RRF（单测/联调对拍）；
- milvus_impl       生产后端：MilvusStore（SR §6.1~§6.4 建表/入库/检索逻辑写全，
                    pymilvus import 守护，联调条件见其模块 docstring）。

SR §3 中的 client.py（连接池/健康检查）、reconcile.py（PG↔Milvus 对账）与
api/admin/vectorstore.py（管理端点）留待后续迭代，不在本子集范围内。
"""
from .base import (
    DEFAULT_TENANT_ID,
    DENSE_DIM,
    FILTERABLE_FIELDS,
    FILTER_VALUE_MAX_LENGTH,
    FilterCondition,
    ITEM_STATUS_MAX_LENGTH,
    ITEM_STATUS_PUBLISHED,
    KB_TYPE_MAX_LENGTH,
    KB_TYPES,
    SearchHit,
    VectorRecord,
    VectorStore,
    parse_filter_expr,
)
from .collection_schema import (
    ALL_FIELDS,
    COLLECTION_NAME,
    CONSISTENCY_LEVEL,
    DEFAULT_RRF_K,
    DENSE_INDEX_SPEC,
    FIELD_SPECS,
    FieldSpec,
    PARTITION_MODE_KB_TYPE,
    PARTITION_MODE_TENANT,
    SCALAR_INVERTED_FIELDS,
    SCALAR_VARCHAR_FIELDS,
    SEARCH_OUTPUT_FIELDS,
    SPARSE_INDEX_SPEC,
    VARCHAR_LIMITS,
    record_to_milvus_row,
)
from .inmemory_impl import InMemoryVectorStore
from .milvus_impl import (
    MilvusDependencyError,
    MilvusStore,
    MilvusStoreError,
    MilvusUnavailableError,
    translate_expr_to_milvus,
)

__all__ = [
    # 契约层
    "VectorRecord",
    "SearchHit",
    "VectorStore",
    "FilterCondition",
    "parse_filter_expr",
    "KB_TYPES",
    "DENSE_DIM",
    "DEFAULT_TENANT_ID",
    "ITEM_STATUS_PUBLISHED",
    "FILTERABLE_FIELDS",
    "KB_TYPE_MAX_LENGTH",
    "FILTER_VALUE_MAX_LENGTH",
    "ITEM_STATUS_MAX_LENGTH",
    # 建表契约
    "COLLECTION_NAME",
    "ALL_FIELDS",
    "SCALAR_VARCHAR_FIELDS",
    "SCALAR_INVERTED_FIELDS",
    "VARCHAR_LIMITS",
    "DENSE_INDEX_SPEC",
    "SPARSE_INDEX_SPEC",
    "CONSISTENCY_LEVEL",
    "SEARCH_OUTPUT_FIELDS",
    "PARTITION_MODE_KB_TYPE",
    "PARTITION_MODE_TENANT",
    "DEFAULT_RRF_K",
    "FieldSpec",
    "FIELD_SPECS",
    "record_to_milvus_row",
    # 双后端
    "InMemoryVectorStore",
    "MilvusStore",
    "MilvusStoreError",
    "MilvusDependencyError",
    "MilvusUnavailableError",
    "translate_expr_to_milvus",
]
