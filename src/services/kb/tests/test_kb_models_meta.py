# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""六库 metadata 内容模型校验测试（FR-KB-001~006，迭代 0.3）。

覆盖：每库合法/非法样例、discriminated union 判别、PII 禁入清单、
KnowledgeItemCreate 按 kb_type 自动校验、KnowledgeItem 审计字段默认值。
用例口径对齐各 SR 文档 §9 测试要点表（SR-KB-001~006）。
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from services.kb.models import (
    Cert,
    CustomerMeta,
    KnowledgeItem,
    KnowledgeItemCreate,
    NegativeCaseMeta,
    PriceBook,
    PriceFloor,
    ProcessMeta,
    ProductMeta,
    SkillMeta,
    TrainingMeta,
    assert_no_pii_keys,
)

# ── 合法样例（每库一份，非法用例在其深拷贝上改字段）───────────────────────────

PRODUCT_OK: dict = {
    "kb_type": "product",
    "category_path": ["AV支架", "电视挂架"],
    "sku": "st-3200",
    "sku_aliases": ["ST3200", "st-3200"],
    "params": {"材质": "冷轧钢", "最大承载": "85kg"},
    "price": {
        "currency": "usd",
        "tiers": [{"moq": 100, "unit_price": "12.5"}, {"moq": 500, "unit_price": "11.8"}],
        "floor_price": {"value": "11.2"},
    },
    "selling_points": [{"tag": "承重", "script": "85kg 承重适配 32-65 寸电视", "scenes": ["询价", "技术"]}],
    "scenes": ["询价", "技术", "样品"],
    "certs": [{"name": "CE", "cert_no": "<占位符>", "valid_until": "2027-05-01"}],
    "competitors": [{"competitor": "竞品A", "model": "X100", "dims": {"价格": "高 15%"}}],
    "moq": 100,
    "lead_time_days": 25,
    "hs_code": "9405",
}

CUSTOMER_OK: dict = {
    "kb_type": "customer",
    "category": "profile",
    "customer_ref": "0b1f6c2a-4d8e-4f2b-9a3c-1e5d7b8a9c0f",
    "grade": "A",
    "tags": {"country": "印度", "industry": "消费电子", "customer_type": "经销商",
             "purchase_scale": "月采购 5k+ 件", "risk_flags": ["价格敏感"]},
    "deal_snapshot": {"outcome": "win", "amount_band": "10k-50k", "at": "2026-09"},
    "pii_digests": ["a" * 64],
}

SKILL_OK: dict = {
    "kb_type": "skill",
    "skill_kind": "talk_track",
    "stage": "bargain",
    "scene_tags": ["砍价应对", "让步阶梯"],
    "customer_types": ["C-01"],
    "customer_grades": ["A", "B"],
    "difficulty": 2,
    "language": ["en", "zh"],
    "slots": [
        {"name": "product", "type": "string", "required": True, "source": "product_kb"},
        {"name": "price_range", "type": "range", "required": True, "source": "product_kb"},
        {"name": "moq", "type": "number", "source": "product_kb", "default": "100"},
    ],
}

PROCESS_OK: dict = {
    "kb_type": "process",
    "category": "platform_rule",
    "process_nodes": ["P03"],
    "prev_nodes": ["P02"],
    "next_nodes": ["P04"],
    "is_redline": True,
    "action_type": "offsite_trade",
    "risk_level": "high",
    "intercept_rule": {"match_type": "keyword", "patterns": ["加微信", "线下转账"], "case_sensitive": False},
    "advice": "引导买家留在平台沟通并说明资金安全",
    "rule_source": "阿里国际站规则中心 <占位> 2026-08 版",
    "effective_at": "2026-08-01",
}

NEGATIVE_OK: dict = {
    "kb_type": "negative_case",
    "case_category": "lost_order",
    "reason_type": "lost_order.price_handling",
    "scene_tag": "议价",
    "occurred_at": "2026-08",
    "lost_order": {
        "customer_ref": "CUST-****-0123",
        "stage": "报价",
        "order_amount_usd": "8000",
        "root_cause": "让步过快",
        "lesson": "坚守让步阶梯",
    },
    "script_pairs": [{
        "pair_uid": "sp-0001",
        "wrong_script": "我们保证最快 7 天交期，出问题全赔",
        "correct_script": "标准交期为 15-20 天，加急订单我需与工厂确认产能后答复您",
        "scene_tag": "议价",
        "risk_level": "high",
        "reason_leaf": "wrong_script.over_promise",
        "trigger_keywords": ["保证最快交期"],
    }],
}

TRAINING_OK: dict = {
    "kb_type": "training",
    "material_kind": "question",
    "question_type": "single",
    "knowledge_points": ["让步阶梯"],
    "ability_dims": {"negotiation": 0.8, "communication": 0.2},
    "difficulty": 3,
    "scene_tag": "议价",
}

LEGAL_PAYLOADS: dict[str, dict] = {
    "product": PRODUCT_OK,
    "customer": CUSTOMER_OK,
    "skill": SKILL_OK,
    "process": PROCESS_OK,
    "negative_case": NEGATIVE_OK,
    "training": TRAINING_OK,
}


def mutated(kb: str, **overrides: object) -> dict:
    """深拷贝合法样例并覆盖顶层键（非法用例构造器）。"""
    payload = copy.deepcopy(LEGAL_PAYLOADS[kb])
    payload.update(overrides)
    return payload


# ── 产品知识库（FR-KB-001 / SR-KB-001 §9）─────────────────────────────────────

def test_product_meta_legal() -> None:
    """合法样例通过；SKU/别名统一大写归一化并去重（SR-KB-001 §6.1/§6.3）。"""
    meta = ProductMeta.model_validate(PRODUCT_OK)
    assert meta.sku == "ST-3200"  # 小写输入归一化为大写
    assert meta.sku_aliases == ["ST3200"]  # 去空、大写、与主 SKU 去重
    assert meta.price is not None and meta.price.currency == "USD"
    assert meta.price.floor_price is not None
    assert meta.price.floor_price.value == Decimal("11.2")


def test_product_sku_pattern_invalid() -> None:
    """SKU 含空格/井号 → 校验失败（SR-KB-001 §9 test_sku_pattern_invalid）。"""
    with pytest.raises(ValidationError, match="SKU 格式非法"):
        ProductMeta.model_validate(mutated("product", sku="ST 3200#"))


def test_product_price_ladder_not_monotonic() -> None:
    """MOQ 500 档单价高于 MOQ 100 档 → 拒绝（SR-KB-001 §9，40001 明细指向价格阶梯）。"""
    payload = mutated("product")
    payload["price"]["tiers"] = [
        {"moq": 100, "unit_price": "12.5"}, {"moq": 500, "unit_price": "13.0"},
    ]
    with pytest.raises(ValidationError, match="MOQ 500 档单价高于 MOQ 100 档"):
        ProductMeta.model_validate(payload)


def test_product_currency_whitelist() -> None:
    """币种白名单外（GBP 未配置）→ 拒绝。"""
    payload = mutated("product")
    payload["price"]["currency"] = "GBP"
    with pytest.raises(ValidationError, match="币种必须为"):
        ProductMeta.model_validate(payload)


def test_product_floor_price_authorized_only_marker() -> None:
    """底线价标记字段：authorized_only 恒为 True，显式传 False 被拒（仅授权可见语义唯一）。"""
    floor = PriceFloor(value=Decimal("11.2"))
    assert floor.authorized_only is True
    with pytest.raises(ValidationError):
        PriceFloor.model_validate({"value": "11.2", "authorized_only": False})


def test_product_cert_expired_flagged() -> None:
    """valid_until 昨日 → expired=True 且入库成功（SR-KB-001 §9 test_cert_expired_flagged）。"""
    payload = mutated("product")
    payload["certs"] = [{"name": "CE", "valid_until": (date.today() - timedelta(days=1)).isoformat()}]
    meta = ProductMeta.model_validate(payload)
    assert meta.certs[0].expired is True
    future = Cert(name="UL", valid_until=date.today() + timedelta(days=400))
    assert future.expired is False


def test_product_unknown_field_rejected() -> None:
    """extra=forbid：白名单外字段（如 floor_discount）拒绝，收敛脏数据入口。"""
    with pytest.raises(ValidationError):
        ProductMeta.model_validate(mutated("product", floor_discount="10%"))


def test_price_book_requires_tier() -> None:
    """价格体系至少一档阶梯。"""
    with pytest.raises(ValidationError):
        PriceBook.model_validate({"currency": "USD", "tiers": []})


# ── 客户知识库（FR-KB-002 / SR-KB-002 §9）─────────────────────────────────────

def test_customer_meta_legal() -> None:
    meta = CustomerMeta.model_validate(CUSTOMER_OK)
    assert meta.customer_ref == "0b1f6c2a-4d8e-4f2b-9a3c-1e5d7b8a9c0f"
    assert meta.grade == "A"
    assert meta.pii_digests == ["a" * 64]


def test_customer_grade_standard_has_no_ref() -> None:
    """分级标准类为全局文档：customer_ref 必须为空，其余三类必须携带。"""
    meta = CustomerMeta.model_validate({"kb_type": "customer", "category": "grade_standard"})
    assert meta.customer_ref is None
    with pytest.raises(ValidationError, match="不得关联具体 customer_ref"):
        CustomerMeta.model_validate(mutated("customer", category="grade_standard"))
    with pytest.raises(ValidationError, match="必须携带 customer_ref"):
        CustomerMeta.model_validate(mutated("customer", customer_ref=None))


def test_customer_ref_must_be_uuid() -> None:
    """customer_ref 非法 UUID → 拒绝（SR-KB-002 §9 ut_meta_ref_invalid）。"""
    with pytest.raises(ValidationError, match="UUID"):
        CustomerMeta.model_validate(mutated("customer", customer_ref="abc"))


def test_customer_grade_pattern() -> None:
    """分级快照必须命中 A/B/C/D。"""
    with pytest.raises(ValidationError, match="客户分级必须为"):
        CustomerMeta.model_validate(mutated("customer", grade="E"))


def test_customer_pii_forbidden_keys() -> None:
    """PII 禁入清单：metadata 携带明文 email/whatsapp → 字段级中文错误（NFR-SEC-01）。"""
    with pytest.raises(ValidationError, match="禁入明文 PII 字段.*email"):
        CustomerMeta.model_validate(mutated("customer", email="buyer@example.com"))
    with pytest.raises(ValidationError, match="禁入明文 PII 字段"):
        CustomerMeta.model_validate(mutated("customer", whatsapp="+8613800000000"))


def test_assert_no_pii_keys_helper() -> None:
    """禁入清单独立可调用（供 FR-KB-021 导入行预处理共用）。"""
    with pytest.raises(ValueError, match="禁入明文 PII 字段"):
        assert_no_pii_keys({"phone": "13800000000"})
    assert_no_pii_keys({"country": "印度"}) is None


def test_customer_bad_digest_rejected() -> None:
    """指纹必须为 64 位十六进制（HMAC-SHA256 hexdigest）。"""
    with pytest.raises(ValidationError, match="64 位十六进制"):
        CustomerMeta.model_validate(mutated("customer", pii_digests=["xyz"]))


# ── 销售技能库（FR-KB-003 / SR-KB-003 §9）─────────────────────────────────────

def test_skill_meta_legal() -> None:
    meta = SkillMeta.model_validate(SKILL_OK)
    assert meta.scene_tags[0] == "砍价应对"  # 主标签约定
    assert [slot.name for slot in meta.slots] == ["product", "price_range", "moq"]


def test_skill_scene_out_of_vocabulary() -> None:
    """词表外标签『砍价技巧』→ 拒绝（SR-KB-003 §9 test_metadata_invalid_scene）。"""
    with pytest.raises(ValidationError, match="不在受控词表内"):
        SkillMeta.model_validate(mutated("skill", scene_tags=["砍价技巧"]))


def test_skill_talk_track_requires_slots() -> None:
    """话术模板必须声明 ≥1 槽位（SR-KB-003 §9 test_metadata_slots_required）。"""
    with pytest.raises(ValidationError, match="必须声明至少 1 个变量槽位"):
        SkillMeta.model_validate(mutated("skill", slots=[]))


def test_skill_non_talk_track_without_slots_ok() -> None:
    """谈判策略类不强制槽位。"""
    meta = SkillMeta.model_validate(
        mutated("skill", skill_kind="negotiation", slots=[], outcome=None)
    )
    assert meta.slots == []


def test_skill_price_slot_must_be_structured_source() -> None:
    """价格槽位 source=free → 拒绝（R-03 价格防幻觉，强制结构化来源）。"""
    payload = mutated("skill")
    payload["slots"] = [{"name": "price_range", "type": "range", "source": "free"}]
    with pytest.raises(ValidationError, match="结构化来源"):
        SkillMeta.model_validate(payload)
    # lead_time 同样受限，来自 config 合法
    payload["slots"] = [{"name": "lead_time", "type": "number", "source": "config"}]
    assert SkillMeta.model_validate(payload).slots[0].name == "lead_time"


def test_skill_enum_slot_needs_values() -> None:
    """enum 槽位必须声明受控取值。"""
    payload = mutated("skill", skill_kind="negotiation", slots=[])
    payload["slots"] = [{"name": "tone", "type": "enum", "source": "config"}]
    with pytest.raises(ValidationError, match="enum_values"):
        SkillMeta.model_validate(payload)


def test_skill_outcome_only_for_cases() -> None:
    """outcome 仅案例类条目可携带。"""
    with pytest.raises(ValidationError, match="outcome 仅案例类"):
        SkillMeta.model_validate(mutated("skill", outcome="win"))


def test_skill_duplicate_slot_names() -> None:
    """槽位名重复 → 拒绝。"""
    payload = mutated("skill")
    payload["slots"] = [
        {"name": "product", "source": "product_kb"},
        {"name": "product", "source": "customer_kb"},
    ]
    with pytest.raises(ValidationError, match="槽位名重复"):
        SkillMeta.model_validate(payload)


# ── 流程知识库（FR-KB-004 / SR-KB-004 §9）─────────────────────────────────────

def test_process_meta_legal() -> None:
    meta = ProcessMeta.model_validate(PROCESS_OK)
    assert meta.is_redline is True
    assert meta.intercept_rule is not None and meta.intercept_rule.patterns == ["加微信", "线下转账"]


def test_process_node_unknown_rejected() -> None:
    """节点不在预置主链（P99）→ 拒绝（悬空引用入库前拦截）。"""
    with pytest.raises(ValidationError, match="不在预置主链"):
        ProcessMeta.model_validate(mutated("process", process_nodes=["P99"]))


def test_process_node_format_rejected() -> None:
    r"""节点编号格式（P9 非 P\d{2}）→ 拒绝。"""
    with pytest.raises(ValidationError, match="如 P03"):
        ProcessMeta.model_validate(mutated("process", process_nodes=["P9"]))


def test_process_chain_direction_violation() -> None:
    """前置节点顺序号必须小于主节点（DAG 方向，防环）。"""
    with pytest.raises(ValidationError, match="DAG 方向"):
        ProcessMeta.model_validate(mutated("process", prev_nodes=["P04"]))
    with pytest.raises(ValidationError, match="DAG 方向"):
        ProcessMeta.model_validate(mutated("process", next_nodes=["P02"]))


def test_process_redline_missing_structured_fields() -> None:
    """红线缺 intercept_rule → 拒绝且错误列明字段（SR-KB-004 §9 test_validator_redline_missing_rule）。"""
    payload = mutated("process", intercept_rule=None)
    with pytest.raises(ValidationError, match="intercept_rule"):
        ProcessMeta.model_validate(payload)
    payload = mutated("process", risk_level=None)
    with pytest.raises(ValidationError, match="risk_level"):
        ProcessMeta.model_validate(payload)


def test_process_bad_regex_pattern_rejected() -> None:
    """regex 拦截规则不可编译 → 入库即拦截。"""
    payload = mutated("process")
    payload["intercept_rule"] = {"match_type": "regex", "patterns": ["([bad"]}
    with pytest.raises(ValidationError, match="不可编译"):
        ProcessMeta.model_validate(payload)


def test_process_non_redline_ok_without_machine_fields() -> None:
    """非红线 SOP 条目：无需三字段，节点链关系可为空。"""
    meta = ProcessMeta.model_validate(
        mutated("process", category="sop", is_redline=False, action_type=None,
                risk_level=None, intercept_rule=None, process_nodes=["P01", "P02"],
                prev_nodes=[], next_nodes=[])
    )
    assert meta.is_redline is False and meta.intercept_rule is None


# ── 负面案例库（FR-KB-005 / SR-KB-005 §9）─────────────────────────────────────

def test_negative_case_legal() -> None:
    meta = NegativeCaseMeta.model_validate(NEGATIVE_OK)
    assert meta.reason_type == "lost_order.price_handling"
    assert meta.script_pairs[0].reason_leaf == "wrong_script.over_promise"


def test_negative_reason_path_invalid() -> None:
    """分类树外路径 → 拒绝（SR-KB-005 §9 test_reason_path_bad）。"""
    with pytest.raises(ValidationError, match="非法原因类型"):
        NegativeCaseMeta.model_validate(mutated("negative_case", reason_type="lost_order.typo"))


def test_negative_category_mismatch() -> None:
    """reason_type 一级 ≠ case_category → 拒绝（SR-KB-005 §9 test_meta_category_mismatch）。"""
    with pytest.raises(ValidationError, match="一级须等于 case_category"):
        NegativeCaseMeta.model_validate(mutated("negative_case", case_category="complaint"))


def test_negative_wrong_script_requires_pairs() -> None:
    """wrong_script 类无对照对 → 拒绝（SR-KB-005 §9 test_wrong_script_requires_pairs）。"""
    payload = mutated("negative_case", case_category="wrong_script",
                      reason_type="wrong_script.over_promise", lost_order=None, script_pairs=[])
    with pytest.raises(ValidationError, match="至少 1 组对照对"):
        NegativeCaseMeta.model_validate(payload)


def test_negative_penalty_requires_penalty_detail() -> None:
    """platform_penalty 类缺 penalty 结构化字段 → 拒绝。"""
    payload = mutated("negative_case", case_category="platform_penalty",
                      reason_type="platform_penalty.offsite_induce", lost_order=None, penalty=None)
    with pytest.raises(ValidationError, match="penalty 字段"):
        NegativeCaseMeta.model_validate(payload)


def test_negative_penalty_detail_format_guards() -> None:
    """处罚详情：证据须占位符格式、时间可模糊到月。"""
    payload = mutated("negative_case", case_category="platform_penalty",
                      reason_type="platform_penalty.offsite_induce", lost_order=None)
    payload["penalty"] = {
        "violation_type": "platform_penalty.offsite_induce", "penalty_level": "suspend",
        "occurred_at": "2026-08", "root_cause": "诱导站外交易",
        "evidence_ref": "oss://bucket/evidence.pdf",  # 非占位符 → 拒绝
    }
    with pytest.raises(ValidationError, match="占位符格式"):
        NegativeCaseMeta.model_validate(payload)
    payload["penalty"]["evidence_ref"] = "<OSS-OBJ-KEY>"
    assert NegativeCaseMeta.model_validate(payload).penalty is not None


def test_negative_script_pair_reason_leaf_guard() -> None:
    """对照对 reason_leaf 必为 wrong_script.* 叶子。"""
    payload = mutated("negative_case")
    payload["script_pairs"][0]["reason_leaf"] = "lost_order.price_handling"
    with pytest.raises(ValidationError, match="wrong_script"):
        NegativeCaseMeta.model_validate(payload)


def test_negative_scene_tag_shared_vocabulary() -> None:
    """scene_tag 与技能库共用词表，词表外拒绝。"""
    with pytest.raises(ValidationError, match="共用词表"):
        NegativeCaseMeta.model_validate(mutated("negative_case", scene_tag="砍价"))


# ── 新人带教素材库（FR-KB-006 / SR-KB-006 §9，场景定义对齐 SRS §6.3）─────────

def test_training_question_legal() -> None:
    meta = TrainingMeta.model_validate(TRAINING_OK)
    assert meta.question_type == "single"
    assert meta.ability_dims.negotiation == pytest.approx(0.8)


def test_training_question_requires_type() -> None:
    """题目素材缺题型 → 拒绝。"""
    with pytest.raises(ValidationError, match="question_type"):
        TrainingMeta.model_validate(mutated("training", question_type=None))


def test_training_scenario_legal_and_requirements() -> None:
    """模拟场景四要素（scenario_key/customer_type/stage_node/opening_context）必填。"""
    scenario = {
        "kb_type": "training", "material_kind": "scenario",
        "scenario_key": "SIM-2026-001", "customer_type": "C-01", "stage_node": "negotiation",
        "opening_context": "买家第一句：What's your best price?",
        "hidden_needs": ["要赠品"], "scene_tag": "议价",
        "assessment_points": [{"dim": "negotiation", "desc": "坚守底价", "weight": 0.6}],
        "difficulty": 2,
    }
    meta = TrainingMeta.model_validate(scenario)
    assert meta.customer_type == "C-01"  # SRS §6.3 印度砍价型
    broken = dict(scenario)
    broken.pop("stage_node")
    with pytest.raises(ValidationError, match="stage_node"):
        TrainingMeta.model_validate(broken)


def test_training_customer_type_pattern() -> None:
    """客户类型编号对齐 SRS §6.3 类型库（C-数字）。"""
    payload = {
        "kb_type": "training", "material_kind": "scenario", "scenario_key": "S-1",
        "customer_type": "印度砍价型", "stage_node": "quote", "opening_context": "x",
    }
    with pytest.raises(ValidationError, match="C-数字"):
        TrainingMeta.model_validate(payload)


def test_training_lesson_requires_course_ref() -> None:
    """课时素材必须携带课程包引用 + 课时顺序（结构化课程包）。"""
    with pytest.raises(ValidationError, match="course_code"):
        TrainingMeta.model_validate({"kb_type": "training", "material_kind": "lesson"})
    meta = TrainingMeta.model_validate(
        {"kb_type": "training", "material_kind": "lesson", "course_code": "NC-NEG-01", "seq": 1}
    )
    assert meta.seq == 1


def test_training_ability_dims_sum() -> None:
    """能力维度权重和必须为 1（或全零未声明）。"""
    with pytest.raises(ValidationError, match="权重之和必须为 1"):
        TrainingMeta.model_validate(
            mutated("training", ability_dims={"negotiation": 0.9, "communication": 0.6})
        )
    TrainingMeta.model_validate(mutated("training", ability_dims={}))


# ── discriminated union 判别与条目模型（FR-KB-020 创建链路）───────────────────

@pytest.mark.parametrize("kb", sorted(LEGAL_PAYLOADS))
def test_union_validates_each_kb(kb: str) -> None:
    """判别联合按 kb_type 标签路由到对应模型并全量校验六库合法样例。"""
    create = KnowledgeItemCreate(kb_type=kb, title="t", content="c", metadata=LEGAL_PAYLOADS[kb])
    assert create.metadata.kb_type == kb


@pytest.mark.parametrize("kb", sorted(LEGAL_PAYLOADS))
def test_union_rejects_wrong_library(kb: str) -> None:
    """条目 kb_type 与 metadata 判别标签错库组合 → 交叉校验拒绝。"""
    other = "product" if kb != "product" else "customer"
    with pytest.raises(ValidationError, match="判别标签不一致"):
        KnowledgeItemCreate(kb_type=kb, title="t", metadata=LEGAL_PAYLOADS[other])


def test_union_unknown_tag_rejected() -> None:
    """判别标签为六库之外的值 → 联合判别失败。"""
    payload = dict(PRODUCT_OK)
    payload["kb_type"] = "inventory"
    with pytest.raises(ValidationError):
        KnowledgeItemCreate(kb_type="product", title="t", metadata=payload)


def test_union_tag_injection_from_item_kb_type() -> None:
    """metadata 可省写判别标签：创建模型从条目级 kb_type 自动注入后再走联合校验。"""
    payload = {k: v for k, v in PRODUCT_OK.items() if k != "kb_type"}
    create = KnowledgeItemCreate(kb_type="product", title="ST-3200", metadata=payload)
    assert isinstance(create.metadata, ProductMeta)
    # 注入不修改调用方原始字典
    assert "kb_type" not in payload


def test_create_validates_metadata_by_kb_type() -> None:
    """创建即校验：非法 metadata（价格阶梯违例）在创建层直接拦截。"""
    payload = mutated("product")
    payload["price"]["tiers"] = [{"moq": 1, "unit_price": "10"}, {"moq": 10, "unit_price": "11"}]
    with pytest.raises(ValidationError, match="价格阶梯异常"):
        KnowledgeItemCreate(kb_type="product", title="t", metadata=payload)


def test_knowledge_item_defaults_and_audit() -> None:
    """KnowledgeItem：uid 自动生成、status=draft、version=1、审计四字段就位。"""
    create = KnowledgeItemCreate(kb_type="customer", title="客户画像", metadata=CUSTOMER_OK)
    item = KnowledgeItem.from_create(create, actor="admin-01")
    assert item.uid is not None
    assert item.status == "draft"
    assert item.version == 1
    assert item.created_by == "admin-01" == item.updated_by
    assert item.created_at == item.updated_at
    assert item.metadata.category == "profile"


def test_knowledge_item_round_trip() -> None:
    """模型 dump → re-validate 往返一致（判别标签随 dump 落盘）。"""
    item = KnowledgeItem(kb_type="process", title="红线", metadata=PROCESS_OK)
    again = KnowledgeItem.model_validate(item.model_dump(mode="json"))
    assert again.metadata == item.metadata


def test_knowledge_item_bad_status_rejected() -> None:
    """状态枚举外（pending）→ 拒绝（03-数据库设计 §3.1 CHECK）。"""
    with pytest.raises(ValidationError):
        KnowledgeItem.model_validate(
            {"kb_type": "product", "title": "t", "metadata": PRODUCT_OK, "status": "pending"}
        )
