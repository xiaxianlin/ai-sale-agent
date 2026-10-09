# 知识产权归甲方所有（COM-IP-01）
"""VectorRecord / SearchHit / expr DSL 解析 / VectorStore 协议契约测试（FR-KB-014）。"""
from __future__ import annotations

import math

import pytest

from services.kb.core.vectorstore import (
    DENSE_DIM,
    FILTERABLE_FIELDS,
    InMemoryVectorStore,
    KB_TYPES,
    SearchHit,
    VectorRecord,
    VectorStore,
    parse_filter_expr,
)


def _dense(*values: float) -> list[float]:
    """构造 1024 维稠密向量：给定前几维，其余补 0（测试便利）。"""
    return [*values, *([0.0] * (DENSE_DIM - len(values)))]


def _record(chunk_id: int = 1, **overrides: object) -> VectorRecord:
    """构造合法 VectorRecord 的默认工厂，允许按需覆盖字段。"""
    payload: dict[str, object] = {
        "chunk_id": chunk_id,
        "kb_type": "product",
        "dense": _dense(1.0),
        "sparse": {1: 1.0},
    }
    payload.update(overrides)
    return VectorRecord(**payload)  # type: ignore[arg-type]


# ── VectorRecord：默认值与字段对齐（03-数据库设计 §4.2）──
def test_record_defaults() -> None:
    """默认值：tenant_id=1、item_status=published、五个过滤维度空串。"""
    record = _record()
    assert record.tenant_id == 1
    assert record.item_status == "published"
    assert (record.sku, record.scene_tag, record.process_node, record.reason_type,
            record.customer_ref) == ("", "", "", "", "")
    assert len(record.dense) == DENSE_DIM


def test_record_kb_type_enum_rejects_unknown() -> None:
    """kb_type 必须命中六大知识库枚举，白名单外拒绝。"""
    for kb_type in KB_TYPES:
        assert _record(kb_type=kb_type).kb_type == kb_type
    with pytest.raises(ValueError, match="六大知识库枚举"):
        _record(kb_type="faq")


def test_record_dense_dimension_enforced() -> None:
    """dense 维度固定 1024（BGE-M3），过短/过长均拒绝。"""
    with pytest.raises(ValueError, match="1024"):
        _record(dense=[1.0, 0.0])
    with pytest.raises(ValueError, match="1024"):
        _record(dense=[0.0] * (DENSE_DIM + 1))


def test_record_rejects_non_finite_vectors() -> None:
    """dense / sparse 拒绝 NaN 与 Inf 分量（脏向量入库前拦截）。"""
    dirty_dense = _dense(1.0)
    dirty_dense[0] = math.nan
    with pytest.raises(ValueError, match="NaN"):
        _record(dense=dirty_dense)
    with pytest.raises(ValueError, match="NaN"):
        _record(sparse={1: math.inf})
    with pytest.raises(ValueError, match="非负整数"):
        _record(sparse={-1: 0.5})


def test_record_key_and_varchar_limits() -> None:
    """chunk_id/tenant_id 正整数；VarChar 长度上限对齐 03 §4.2（64/16）。"""
    with pytest.raises(ValueError, match="chunk_id"):
        _record(chunk_id=0)
    with pytest.raises(ValueError, match="tenant_id"):
        _record(tenant_id=0)
    with pytest.raises(ValueError, match="过滤维度超长"):
        _record(sku="S" * 65)
    with pytest.raises(ValueError, match="item_status 超长"):
        _record(item_status="x" * 17)


def test_record_frozen_and_extra_forbidden() -> None:
    """入库后不可变（frozen）、未知字段拒绝（extra=forbid）。"""
    record = _record()
    with pytest.raises(Exception):
        record.chunk_id = 2  # type: ignore[misc]
    with pytest.raises(ValueError):
        _record(unknown_field="x")


def test_searchhit_fields() -> None:
    """SearchHit 仅含 chunk_id 与 score。"""
    hit = SearchHit(chunk_id=3, score=0.25)
    assert hit.chunk_id == 3
    assert hit.score == pytest.approx(0.25)


# ── expr 简化 DSL 解析 ──
def test_parse_expr_none_and_empty() -> None:
    """None / 空串 / 纯空白返回空条件（不过滤）。"""
    assert parse_filter_expr(None) == []
    assert parse_filter_expr("") == []
    assert parse_filter_expr("   ") == []


def test_parse_expr_single_and_combined() -> None:
    """单条件与 and 组合：契约示例逐字验证。"""
    (condition,) = parse_filter_expr("kb_type in ('product','skill')")
    assert (condition.field, condition.op, condition.values) == (
        "kb_type", "in", ("product", "skill"))
    (condition,) = parse_filter_expr("sku = 'ST-3200'")
    assert (condition.field, condition.op, condition.values) == ("sku", "=", ("ST-3200",))
    first, second = parse_filter_expr("kb_type in ('product','skill') and sku = 'ST-3200'")
    assert (first.field, second.field) == ("kb_type", "sku")
    assert second.values == ("ST-3200",)


def test_parse_expr_whitespace_and_spacing_tolerant() -> None:
    """空白宽容：多空格 / 紧凑写法均可解析，值内空格原样保留。"""
    conditions = parse_filter_expr("  kb_type in ( 'product' ,  'skill' )  and  sku='AB-1'")
    assert conditions[0].values == ("product", "skill")
    assert conditions[1].values == ("AB-1",)
    (condition,) = parse_filter_expr("sku = 'A and B'")
    assert condition.values == ("A and B",)  # 引号内的 and 不是分隔符


def test_parse_expr_value_containing_and_keyword() -> None:
    """值内含 ` and ` 子串不破坏解析（引号感知，不从值中间切分）。"""
    (condition,) = parse_filter_expr("customer_ref = 'ACME and Sons Ltd'")
    assert condition.values == ("ACME and Sons Ltd",)


def test_parse_expr_rejects_malformed() -> None:
    """fail-closed：白名单外字段 / or / 括号 / 双引号 / 大写 AND / 空列表 / 悬空 and。"""
    for bad in (
        "foo = 'x'",                                  # 白名单外字段
        "tenant_id == 1",                             # tenant 不入 DSL（== 亦不支持）
        "kb_type in ('product') or sku = 'AB-1'",     # or 不支持
        "(kb_type = 'product')",                      # 括号不支持
        'kb_type in ("product")',                     # 双引号不支持
        "kb_type IN ('product')",                     # 关键字仅小写
        "kb_type in ('product') AND sku = 'x'",       # AND 大写不支持
        "kb_type in ()",                              # 空值列表
        "kb_type = 'product' and",                    # 悬空 and
        "kb_type = 'product' extra",                  # 尾部垃圾字符
        "kb_type != 'product'",                       # != 不支持
    ):
        with pytest.raises(ValueError):
            parse_filter_expr(bad)


def test_parse_expr_all_whitelist_fields() -> None:
    """白名单七个字段全部可过滤，与 VectorRecord 标量字段一一对应。"""
    assert FILTERABLE_FIELDS == {
        "kb_type", "sku", "scene_tag", "process_node", "reason_type", "customer_ref",
        "item_status"}
    for field in sorted(FILTERABLE_FIELDS):
        (condition,) = parse_filter_expr(f"{field} = 'v'")
        assert condition.field == field
        if field == "kb_type":
            # 枚举字段不能用任意值构造记录，取值约束已在 test_record_kb_type_enum_rejects_unknown 覆盖
            continue
        assert condition.matches(_record(**{field: "v"}))
        assert not condition.matches(_record())


# ── 协议 ──
def test_inmemory_store_satisfies_protocol() -> None:
    """InMemoryVectorStore 结构化满足 VectorStore 协议（runtime_checkable）。"""
    assert isinstance(InMemoryVectorStore(), VectorStore)
