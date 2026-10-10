# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""六库 Schema 注册表完备性测试（FR-KB-020/021 单一事实源，迭代 0.3）。

覆盖：六库全覆盖、字段中文 label 非空、控件类型合法、注册表键集与各库
Pydantic 模型字段集逐一相等、必填一致（条件必填白名单显式登记）、
get_schema JSON 可下发、关键 SR-KB-020 §4 表条目逐项抽查。
"""
from __future__ import annotations

import json

import pytest

from services.kb.core.vectorstore.base import KB_TYPES
from services.kb.models import (
    CASE_CATEGORIES,
    CUSTOMER_CATEGORIES,
    CUSTOMER_GRADES,
    KBType,
    META_MODELS,
    PROCESS_ACTION_TYPES,
    PROCESS_CATEGORIES,
    REGISTRY,
    WIDGETS,
    get_schema,
    registry_issues,
)


def test_kb_type_enum_matches_vectorstore_contract() -> None:
    """KBType 取值与向量库侧 KB_TYPES（03-数据库设计 §4.2 / 分区名）完全一致。"""
    assert {member.value for member in KBType} == set(KB_TYPES)
    assert len(list(KBType)) == 6


def test_registry_self_check_passes() -> None:
    """registry_issues 自检通过（覆盖键集/label/控件/字段一致/必填一致五类检查）。"""
    assert registry_issues() == []


def test_registry_covers_all_six_libraries() -> None:
    """六库全覆盖：REGISTRY 与 META_MODELS 键集都等于 KBType 取值集。"""
    expected = {member.value for member in KBType}
    assert set(REGISTRY) == expected
    assert set(META_MODELS) == expected


def test_registry_labels_nonempty_and_widgets_valid() -> None:
    """每个字段：中文 label 非空、控件类型在 WIDGETS 词表内、键无重复。"""
    for kb_type, field_defs in REGISTRY.items():
        keys = [f.key for f in field_defs]
        assert len(keys) == len(set(keys)), f"[{kb_type}] 字段键重复"
        for f in field_defs:
            assert f.key and f.key.strip(), f"[{kb_type}] 空字段键"
            assert f.label and f.label.strip(), f"[{kb_type}] {f.key} 中文 label 为空"
            assert f.widget in WIDGETS, f"[{kb_type}] {f.key} 控件 {f.widget} 不在词表"


def test_registry_keys_equal_model_fields() -> None:
    """单一事实源：注册表键集 == 各库模型字段集（判别字段 kb_type 除外）。"""
    for kb_type, model in META_MODELS.items():
        model_fields = set(model.model_fields) - {"kb_type"}
        assert {f.key for f in REGISTRY[kb_type]} == model_fields, (
            f"[{kb_type}] 注册表与模型字段漂移："
            f"缺失 {sorted(model_fields - {f.key for f in REGISTRY[kb_type]})}"
        )


def test_registry_required_consistency() -> None:
    """必填一致：模型必填 ⇒ 注册表必填；注册表额外必填已由自检白名单守护。"""
    for kb_type, model in META_MODELS.items():
        model_required = {
            name for name, f in model.model_fields.items()
            if f.is_required() and name != "kb_type"
        }
        registry_required = {f.key for f in REGISTRY[kb_type] if f.required}
        assert model_required <= registry_required, (
            f"[{kb_type}] 模型必填未标必填：{sorted(model_required - registry_required)}"
        )


def test_get_schema_json_ready() -> None:
    """get_schema 输出 JSON 可序列化（/api/v1/knowledge/schemas 下发零拼装）。"""
    for member in KBType:
        schema = get_schema(member)
        payload = json.dumps(schema, ensure_ascii=False)
        assert member.value in ("product", "customer", "skill", "process", "negative_case", "training")
        assert json.loads(payload) == schema
        assert schema["label"], f"{member.value} 库中文名缺失"
    with pytest.raises(ValueError, match="六大知识库"):
        get_schema("inventory")


# ── SR-KB-020 §4 注册表登记表逐项抽查 ─────────────────────────────────────────

def _field(kb: str, key: str):
    """取某库某字段的注册定义。"""
    return next(f for f in REGISTRY[kb] if f.key == key)


def test_field_product_sku_rules() -> None:
    """product.sku：input 控件、必填、SKU 正则规则、可过滤。"""
    f = _field("product", "sku")
    assert f.widget == "input" and f.required is True and f.filterable is True
    assert f.rules["pattern"] == "^[A-Za-z0-9\\-]{3,32}$"


def test_field_customer_grade_options() -> None:
    """customer.grade：select 控件 + static 选项 A/B/C/D。"""
    f = _field("customer", "grade")
    assert f.options is not None and f.options["values"] == list(CUSTOMER_GRADES)


def test_field_customer_ref_conditional() -> None:
    """customer.customer_ref：条件必填（分级标准类隐藏）。"""
    f = _field("customer", "customer_ref")
    assert f.visible_when is not None
    assert "grade_standard" not in f.visible_when["in"]


def test_field_process_redline_visibility() -> None:
    """process 三机器字段：is_redline=true 时显隐（SR-KB-020 §4 表）。"""
    for key in ("action_type", "risk_level", "intercept_rule"):
        f = _field("process", key)
        assert f.visible_when == {"field": "is_redline", "equals": True}
    assert _field("process", "intercept_rule").rules.get("custom") == "intercept_rule_required_when_redline"


def test_field_skill_slots_visible_when_talk_track() -> None:
    """skill.slots：仅 talk_track 类显示（SR-KB-020 §4 表）。"""
    f = _field("skill", "slots")
    assert f.visible_when == {"field": "skill_kind", "equals": "talk_track"}


def test_field_negative_case_pairs_and_tree() -> None:
    """negative_case：script_pairs 仅 wrong_script 显示；reason_type 走分类树词表。"""
    pairs = _field("negative_case", "script_pairs")
    assert pairs.visible_when == {"field": "case_category", "equals": "wrong_script"}
    reason = _field("negative_case", "reason_type")
    assert reason.options is not None and reason.options["type"] == "taxonomy"
    assert reason.required is True and reason.filterable is True


def test_static_option_values_match_constants() -> None:
    """static 选项取值与内容模型常量一致（两处同源，防口径漂移）。"""
    assert _field("customer", "category").options["values"] == list(CUSTOMER_CATEGORIES)
    assert _field("negative_case", "case_category").options["values"] == list(CASE_CATEGORIES)
    assert _field("process", "category").options["values"] == list(PROCESS_CATEGORIES)
    assert _field("process", "action_type").options["values"] == list(PROCESS_ACTION_TYPES)


def test_filterable_fields_match_vector_scalars() -> None:
    """注册表 filterable 字段必须有标量维度或 PG 预过滤支撑（列表页过滤栏口径）。

    三条合法通道：
    - 直接对应 VectorRecord 标量（FILTERABLE_FIELDS）；
    - 经 record_dims 映射的别名（scene_tags→scene_tag / process_nodes→process_node /
      stage_node→process_node）；
    - SR 方案已立项的扩展/预过滤维度（SR-KB-001 §4 扩展① category/competitor、
      SR-KB-002 §2 偏差② cust_grade、SR-KB-003 §6.2 多标签与 stage 走 PG 结构化预过滤、
      SR-KB-006 知识点组卷过滤）——新增 filterable 字段必须归入其一，防止无支撑过滤键。
    """
    from services.kb.core.vectorstore.base import FILTERABLE_FIELDS

    scalar_aliases = {"scene_tags": "scene_tag", "process_nodes": "process_node", "stage_node": "process_node"}
    pg_prefilter = {"category_path", "competitors", "grade", "stage", "knowledge_points"}
    for kb_type, field_defs in REGISTRY.items():
        for f in field_defs:
            if f.filterable:
                supported = (
                    f.key in FILTERABLE_FIELDS
                    or f.key in scalar_aliases
                    or f.key in pg_prefilter
                )
                assert supported, f"[{kb_type}] filterable 字段 {f.key} 无标量/预过滤维度支撑"
