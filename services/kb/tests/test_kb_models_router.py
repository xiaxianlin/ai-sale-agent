# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""入库路由测试（FR-KB-001~006 维度映射 / 敏感键剥离 / 红线导出，迭代 0.3）。

覆盖：
- 六库 metadata 维度 → VectorRecord 标量维度映射正确性（只读引用
  vectorstore.base 的 FILTERABLE_FIELDS / VectorRecord 做契约验证）；
- 与增量同步引擎事件模型（sync.engine.ItemVectorMeta / KnowledgeItemEvent）
  的维度字段同名对齐（引擎导入链较重，契约守护放测试层）；
- 价格体系（含底线价）不入向量正文的标记行为（SR-KB-001 §2 决策 2）；
- 红线条目 → 风控拦截规则清单导出（SR-KB-004 §5② 契约）。
"""
from __future__ import annotations

import dataclasses
import json
import uuid

import pytest

from services.kb.core.pipeline.sync.engine import ItemVectorMeta, KnowledgeItemEvent
from services.kb.core.vectorstore.base import (
    DENSE_DIM,
    FILTERABLE_FIELDS,
    KB_TYPES,
    VectorRecord,
)
from services.kb.models import (
    DIM_FIELDS,
    DIM_PAYLOAD_KEYS,
    FLOOR_PRICE_AUTHORIZED_ONLY_MARKER,
    PRICE_REDACTED_MARKER,
    ItemStatus,
    KnowledgeItem,
    KnowledgeItemCreate,
    export_intercept_rules,
    record_dims,
    embedding_safe_metadata,
)
from services.kb.tests.test_kb_models_meta import (  # 复用六库合法样例
    CUSTOMER_OK,
    LEGAL_PAYLOADS,
    NEGATIVE_OK,
    PROCESS_OK,
    PRODUCT_OK,
    SKILL_OK,
    TRAINING_OK,
)


def _item(kb: str, payload: dict, **overrides: object) -> KnowledgeItem:
    """由合法样例构造条目（可覆盖 status 等字段，覆盖后重校验固化）。"""
    create = KnowledgeItemCreate(kb_type=kb, title=f"{kb} 条目", content="正文", metadata=payload)
    item = KnowledgeItem.from_create(create).model_copy(update=overrides)
    return KnowledgeItem.model_validate(item.model_dump())


def _empty_except(kb_type: str, **dims: str) -> dict[str, str]:
    """期望维度字典：kb_type + 五维度（缺省空串）。"""
    expected = {"kb_type": kb_type, "sku": "", "scene_tag": "", "process_node": "",
                "reason_type": "", "customer_ref": ""}
    expected.update(dims)
    return expected


# ── 维度映射正确性（六库逐一）─────────────────────────────────────────────────

def test_dims_keys_align_vector_record_scalars() -> None:
    """返回键集 = kb_type + VectorRecord 五个过滤标量，且落在过滤白名单内。"""
    assert set(DIM_FIELDS) <= FILTERABLE_FIELDS
    assert DIM_PAYLOAD_KEYS == {"kb_type", *FILTERABLE_FIELDS - {"item_status"}}
    item = _item("product", PRODUCT_OK)
    assert set(record_dims(item)) == DIM_PAYLOAD_KEYS


def test_dims_product_sku() -> None:
    """产品库 → sku 维度（建模层已大写归一化）；scene_tag 为卖点块级维度，条目粒度不落。"""
    dims = record_dims(_item("product", PRODUCT_OK))
    assert dims == _empty_except("product", sku="ST-3200")


def test_dims_customer_ref() -> None:
    """客户库 → customer_ref 维度。"""
    dims = record_dims(_item("customer", CUSTOMER_OK))
    assert dims == _empty_except("customer", customer_ref=CUSTOMER_OK["customer_ref"])


def test_dims_customer_grade_standard_empty_ref() -> None:
    """分级标准类无 customer_ref → 维度为空串（全库检索不按客户过滤）。"""
    dims = record_dims(_item("customer", {"kb_type": "customer", "category": "grade_standard"}))
    assert dims["customer_ref"] == ""


def test_dims_skill_main_scene_tag() -> None:
    """技能库 → scene_tag = 主标签 scene_tags[0]（SR-KB-003 决策 4）。"""
    dims = record_dims(_item("skill", SKILL_OK))
    assert dims == _empty_except("skill", scene_tag="砍价应对")


def test_dims_process_single_vs_multi_node() -> None:
    """流程库：单节点条目落节点编号；多节点/无节点为空串（块级由 heading 解析覆盖）。"""
    single = record_dims(_item("process", PROCESS_OK))
    assert single == _empty_except("process", process_node="P03")
    multi = _item("process", {**PROCESS_OK, "process_nodes": ["P03", "P04"]})
    assert record_dims(multi)["process_node"] == ""
    none_meta = {k: v for k, v in PROCESS_OK.items() if k != "process_nodes"}
    assert record_dims(_item("process", none_meta))["process_node"] == ""


def test_dims_negative_case_reason_type() -> None:
    """负面案例库 → reason_type 维度（分类树路径）。"""
    dims = record_dims(_item("negative_case", NEGATIVE_OK))
    assert dims == _empty_except("negative_case", reason_type="lost_order.price_handling")


def test_dims_training_scenario_maps_stage_node() -> None:
    """培训库：scene_tag 落场景标签；scenario 素材另落 process_node=stage_node（SR-KB-006 §4）。"""
    scenario = {
        "kb_type": "training", "material_kind": "scenario", "scenario_key": "SIM-001",
        "customer_type": "C-01", "stage_node": "negotiation", "opening_context": "ctx",
        "scene_tag": "议价",
    }
    dims = record_dims(_item("training", scenario))
    assert dims == _empty_except("training", scene_tag="议价", process_node="negotiation")
    # 非场景素材（题目）不落 process_node
    question_dims = record_dims(_item("training", TRAINING_OK))
    assert question_dims["process_node"] == ""


def test_dims_feed_vector_record() -> None:
    """映射结果可直接填充 VectorRecord 标量列（六库全通过入库行校验）。"""
    for kb, payload in LEGAL_PAYLOADS.items():
        dims = record_dims(_item(kb, payload))
        record = VectorRecord(
            chunk_id=1,
            dense=[0.0] * DENSE_DIM,
            sparse={},
            **{key: dims[key] for key in ("kb_type", *DIM_FIELDS)},
        )
        assert record.kb_type in KB_TYPES


def test_dims_align_sync_engine_event_model() -> None:
    """与增量同步引擎事件模型同名对齐：ItemVectorMeta 字段 = kb_type + 五维度。"""
    item = _item("product", PRODUCT_OK)
    dims = record_dims(item)
    meta_fields = {f.name for f in dataclasses.fields(ItemVectorMeta)}
    assert meta_fields == set(dims)
    event_fields = {f.name for f in dataclasses.fields(KnowledgeItemEvent)}
    assert meta_fields <= event_fields
    # 事件可直接由映射结果构造（引擎消费契约）
    event = KnowledgeItemEvent(item_id=1, body="正文", **dims)
    assert event.sku == "ST-3200"


# ── 敏感键剥离：价格（含底线价）不入向量正文的标记行为 ────────────────────────

def test_embedding_safe_metadata_strips_price_and_marks() -> None:
    """产品投影：price 键整体剥离（含底线价数值），落 price_redacted 与
    floor_price_authorized_only 布尔标记——底线价不出现在投影 JSON 任何位置。"""
    item = _item("product", PRODUCT_OK)
    safe = embedding_safe_metadata(item)
    text = json.dumps(safe, ensure_ascii=False)
    assert "price" not in safe or safe.get("price") is None
    assert safe[PRICE_REDACTED_MARKER] is True
    assert safe[FLOOR_PRICE_AUTHORIZED_ONLY_MARKER] is True
    assert "11.2" not in text and "12.5" not in text and "11.8" not in text
    # 非敏感维度保留
    assert safe["sku"] == "ST-3200" and safe["category_path"] == ["AV支架", "电视挂架"]


def test_embedding_safe_metadata_floor_absent_marker_false() -> None:
    """未声明底线价：price 仍剥离，标记 floor_price_authorized_only=False。"""
    payload = {k: v for k, v in PRODUCT_OK.items()}
    payload["price"] = {"currency": "USD", "tiers": [{"moq": 100, "unit_price": "9.9"}]}
    safe = embedding_safe_metadata(_item("product", payload))
    assert safe[PRICE_REDACTED_MARKER] is True
    assert safe[FLOOR_PRICE_AUTHORIZED_ONLY_MARKER] is False


def test_embedding_safe_metadata_no_price_at_all() -> None:
    """未声明价格体系：不落价格标记（无剥离即无标记）。"""
    payload = {k: v for k, v in PRODUCT_OK.items() if k != "price"}
    safe = embedding_safe_metadata(_item("product", payload))
    assert PRICE_REDACTED_MARKER not in safe


def test_embedding_safe_metadata_customer_digests_removed() -> None:
    """客户投影：pii_digests 指纹剥离（等值检索走标量列，不做语义化）。"""
    safe = embedding_safe_metadata(_item("customer", CUSTOMER_OK))
    assert "pii_digests" not in safe
    assert safe["tags"]["country"] == "印度"


# ── 红线结构化导出（FR-AG-005 风控消费契约，SR-KB-004 §5②）────────────────────

def _redline_item(title: str, status: ItemStatus = ItemStatus.PUBLISHED) -> KnowledgeItem:
    """已发布流程红线索材（可指定状态）。"""
    return _item("process", PROCESS_OK, status=status, title=title)


def test_export_only_published_redlines() -> None:
    """仅导出 published 红线：草稿红线、非红线 SOP、他库条目全部排除。"""
    items = [
        _redline_item("禁止诱导站外交易"),
        _redline_item("草稿红线", status=ItemStatus.DRAFT),
        _item("process", {**PROCESS_OK, "is_redline": False, "action_type": None,
                          "risk_level": None, "intercept_rule": None}),
        _item("customer", CUSTOMER_OK),
    ]
    result = export_intercept_rules(items, version=7)
    assert result["version"] == 7 and result["unchanged"] is False
    assert len(result["rules"]) == 1
    rule = result["rules"][0]
    assert rule["rule_id"] == "RL-0001"
    assert rule["action_type"] == "offsite_trade"
    assert rule["risk_level"] == "high"
    assert rule["intercept_rule"]["patterns"] == ["加微信", "线下转账"]
    assert rule["advice"] == "引导买家留在平台沟通并说明资金安全"
    assert rule["item_version"] == 1


def test_export_contract_fields() -> None:
    """导出条目字段与 SR-KB-004 §5② 响应契约逐键一致。"""
    (rule,) = export_intercept_rules([_redline_item("红线")])["rules"]
    assert set(rule) == {
        "rule_id", "item_uid", "title", "action_type", "risk_level", "intercept_rule",
        "advice", "rule_source", "effective_at", "item_version",
    }
    assert uuid.UUID(rule["item_uid"])  # item_uid 为合法 UUID
    assert rule["effective_at"] == "2026-08-01"


def test_export_version_negotiation_unchanged() -> None:
    """client_version 与当前一致 → unchanged=True、rules 为空（风控免重建）。"""
    result = export_intercept_rules([_redline_item("红线")], version=5, client_version=5)
    assert result == {"version": 5, "unchanged": True, "rules": []}
    stale = export_intercept_rules([_redline_item("红线")], version=5, client_version=4)
    assert stale["unchanged"] is False and len(stale["rules"]) == 1


def test_export_rule_ids_stable_ordering() -> None:
    """rule_id 按 (title, uid) 稳定排序编号，重放结果确定（RL-0001 起）。"""
    items = [_redline_item("B 规则"), _redline_item("A 规则")]
    first = export_intercept_rules(items)
    again = export_intercept_rules(items)
    assert [r["rule_id"] for r in first["rules"]] == ["RL-0001", "RL-0002"]
    assert first["rules"][0]["title"] == "A 规则"
    assert first == again


def test_export_empty_inputs() -> None:
    """空输入 / 全非红线 → rules 为空。"""
    assert export_intercept_rules([])["rules"] == []
    assert export_intercept_rules([_item("skill", SKILL_OK)])["rules"] == []
