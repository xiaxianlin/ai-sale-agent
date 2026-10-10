# 知识产权归甲方所有（COM-IP-01）
"""collection_schema 建表契约测试：字段/索引/分区与 03-数据库设计 §4.2 逐项对齐。"""
from __future__ import annotations

from services.kb.core.vectorstore import (
    DENSE_DIM,
    DENSE_INDEX_SPEC,
    FIELD_SPECS,
    KB_TYPES,
    CONSISTENCY_LEVEL,
    PARTITION_MODE_KB_TYPE,
    PARTITION_MODE_TENANT,
    SCALAR_INVERTED_FIELDS,
    SCALAR_VARCHAR_FIELDS,
    SPARSE_INDEX_SPEC,
    VARCHAR_LIMITS,
    record_to_milvus_row,
)
from services.kb.core.vectorstore.base import (
    FILTER_VALUE_MAX_LENGTH,
    KB_TYPE_MAX_LENGTH,
    VectorRecord,
    ITEM_STATUS_MAX_LENGTH,
)
from services.kb.core.vectorstore.collection_schema import (
    ALL_FIELDS,
    COLLECTION_NAME,
    TENANT_PARTITION_NUM,
)


def _dense_1024() -> list[float]:
    """1024 维单位向量的第 0 维为 1（测试便利）。"""
    return [1.0] + [0.0] * (DENSE_DIM - 1)


# ── 字段表：与 03-数据库设计 §4.2 / SR-KB-014 §4 逐字段一致 ──
def test_field_specs_match_design_doc() -> None:
    """11 个字段、名称/顺序/类型/主键/Partition Key 注记逐项对齐设计文档。"""
    assert [spec.name for spec in FIELD_SPECS] == [
        "chunk_id", "tenant_id", "kb_type", "dense_vec", "sparse_vec",
        "sku", "scene_tag", "process_node", "reason_type", "customer_ref", "item_status",
    ]
    assert [spec.milvus_type for spec in FIELD_SPECS] == [
        "INT64", "INT64", "VARCHAR", "FLOAT_VECTOR", "SPARSE_FLOAT_VECTOR",
        "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR",
    ]
    primary = [spec.name for spec in FIELD_SPECS if spec.is_primary]
    partition_key = [spec.name for spec in FIELD_SPECS if spec.is_partition_key]
    assert primary == ["chunk_id"]  # 主键 = PG knowledge_chunk.id
    assert partition_key == ["tenant_id"]  # P5 Partition Key 注记（P0-P4 恒 1）


def test_varchar_limits_and_vector_dim() -> None:
    """VarChar 长度上限（32/64/16）与稠密维度 1024 对齐 §4.2 维度表。"""
    assert VARCHAR_LIMITS["kb_type"] == 32
    assert all(VARCHAR_LIMITS[f] == 64 for f in SCALAR_VARCHAR_FIELDS)
    assert VARCHAR_LIMITS["item_status"] == 16
    dense_spec = next(spec for spec in FIELD_SPECS if spec.name == "dense_vec")
    assert dense_spec.dim == 1024
    # base.py 建模校验与 Milvus 建表限制同源（两处常量必须一致）
    assert KB_TYPE_MAX_LENGTH == VARCHAR_LIMITS["kb_type"]
    assert FILTER_VALUE_MAX_LENGTH == 64
    assert ITEM_STATUS_MAX_LENGTH == VARCHAR_LIMITS["item_status"]


def test_collection_and_partition_constants() -> None:
    """单 Collection knowledge_chunks；P0-P4 kb_type 六分区；P5 tenant 64 哈希分区。"""
    assert COLLECTION_NAME == "knowledge_chunks"
    assert len(KB_TYPES) == 6
    assert set(KB_TYPES) == {
        "product", "customer", "skill", "process", "negative_case", "training"}
    assert PARTITION_MODE_KB_TYPE == "kb_type"
    assert PARTITION_MODE_TENANT == "tenant"
    assert TENANT_PARTITION_NUM == 64


def test_index_specs_match_design_doc() -> None:
    """索引对齐 §4.2：稠密 HNSW(M=16, efC=200, IP)；稀疏倒排(IP)；标量 INVERTED。"""
    assert DENSE_INDEX_SPEC["index_type"] == "HNSW"
    assert DENSE_INDEX_SPEC["metric_type"] == "IP"
    assert DENSE_INDEX_SPEC["params"] == {"M": 16, "efConstruction": 200}
    assert SPARSE_INDEX_SPEC["index_type"] == "SPARSE_INVERTED_INDEX"
    assert SPARSE_INDEX_SPEC["metric_type"] == "IP"
    assert set(SCALAR_INVERTED_FIELDS) == {
        "kb_type", "sku", "scene_tag", "process_node", "reason_type", "customer_ref",
        "item_status"}
    assert CONSISTENCY_LEVEL == "Bounded"  # 增量 ≤3s 可见（FR-KB-017）


def test_record_to_milvus_row_mapping() -> None:
    """VectorRecord → Milvus 行：11 字段名对齐、稀疏下标转字符串键、标量原样透传。"""
    record = VectorRecord(
        chunk_id=7, tenant_id=1, kb_type="product",
        dense=_dense_1024(), sparse={3: 0.5, 128: 2.0},
        sku="ST-3200", scene_tag="after_sale", process_node="quote",
        reason_type="price", customer_ref="ACME",
    )
    row = record_to_milvus_row(record)
    assert set(row) == set(ALL_FIELDS)
    assert row["chunk_id"] == 7 and row["tenant_id"] == 1 and row["kb_type"] == "product"
    assert row["dense_vec"] == _dense_1024()
    assert row["sparse_vec"] == {"3": 0.5, "128": 2.0}
    assert row["sku"] == "ST-3200" and row["item_status"] == "published"
