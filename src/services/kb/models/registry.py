# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""六大知识库 Schema 注册表（FR-KB-020 动态表单与 FR-KB-021 导入模板共用的单一事实源）。

SR-KB-020 §2 决策 1 / SR-KB-021 §2 决策 1：六库字段定义（字段名 / 中文 label /
控件类型 / 必填 / 校验规则 / 选项来源）在 kb-service 侧集中注册，界面表单与
Excel 导入模板共用一套定义，杜绝「表单放行、后端拒绝」的口径漂移。

与 kb_types.py 的 Pydantic 元数据模型同源分工：
- 本表负责「界面/模板怎么渲染」（FieldDef）；
- Pydantic 负责「数据是否合法」；
- registry_issues() + 测试守护两侧一致：注册表键集与各库模型字段集逐一相等、
  模型必填字段必为注册表必填（条件必填白名单显式登记）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from pydantic import BaseModel

from .kb_types import (
    CASE_CATEGORIES,
    CUSTOMER_CATEGORIES,
    CUSTOMER_GRADES,
    KBType,
    NEGATIVE_CASE_REASON_TREE,
    PRESET_MAIN_CHAIN,
    PROCESS_ACTION_TYPES,
    PROCESS_CATEGORIES,
    QUESTION_TYPES,
    SKILL_KINDS,
    SKILL_STAGES,
    SLOT_SOURCES,
    TRAINING_MATERIAL_KINDS,
    CustomerMeta,
    NegativeCaseMeta,
    ProcessMeta,
    ProductMeta,
    SkillMeta,
    TrainingMeta,
)

#: 控件类型词表（SR-KB-020 §6.1 WIDGETS；前端 Element Plus 动态组件映射依据）
WIDGETS: frozenset[str] = frozenset({
    "input", "textarea", "number", "select", "multi_select",
    "cascader", "date", "switch", "object", "object_array", "readonly",
})

#: 模型可选但运行时/表单层必填的字段（注册表标 required=True 的合法差集；
#: 条件由各库模型的 model_validator 与 FieldDef.visible_when 共同表达）
_CONDITIONAL_REQUIRED: frozenset[str] = frozenset({
    "customer_ref",   # 客户库：分级标准类为空，其余三类必填（SR-KB-002 §6.1）
    "question_type",  # 培训库：question 素材必填题型（SR-KB-006 §4）
    "scenario_key", "customer_type", "stage_node", "opening_context",  # scenario 素材四要素
    "course_code", "seq",  # lesson 素材：课程包引用 + 课时顺序
    "process_nodes",  # 流程库：表单必填（SR-KB-020 §4），模型层允许空列表=跨节点通识（SR-KB-004 §6）
})


@dataclass(frozen=True)
class FieldDef:
    """字段定义（SR-KB-020 §6.1）：metadata 内键名 / 中文标签 / 控件类型 + 渲染属性。

    - rules：机器可读校验规则 {pattern, pattern_msg, min, max, custom}，
      前端 async-validator 与 FR-KB-021 导入行校验共用；
    - options：选项来源 {type: static|api|taxonomy, ref[, values]}，
      static 随包内置取值，api/taxonomy 在 Schema 端点内解析为当前值；
    - visible_when：条件显隐 {field, equals|in}（如流程库勾选红线才展开三字段）；
    - filterable：是否进后台列表页过滤栏（与 VectorRecord 标量维度对应）。
    """

    key: str
    label: str
    widget: str
    required: bool = False
    rules: dict[str, object] = field(default_factory=dict)
    options: dict[str, object] | None = None
    visible_when: dict[str, object] | None = None
    filterable: bool = False
    help: str = ""


def _static(values: tuple[str, ...] | list[str]) -> dict[str, object]:
    """构造 static 选项来源（随包内置受控取值）。"""
    return {"type": "static", "values": list(values)}


#: 六库字段注册表（键为 kb_type 取值；字段清单与 SR-KB-001~006 §4/§6 内容模型一致）
REGISTRY: dict[str, list[FieldDef]] = {
    # ── 产品知识库（FR-KB-001，SR-KB-020 §4 表 + SR-KB-001 §4 全字段）──
    "product": [
        FieldDef("category_path", "品类路径", "cascader", True,
                 options={"type": "api", "ref": "/api/v1/knowledge/taxonomies/category"},
                 filterable=True, help="末级品类用于检索过滤，上级走查询改写扩展"),
        FieldDef("sku", "SKU", "input", True,
                 rules={"pattern": "^[A-Za-z0-9\\-]{3,32}$", "pattern_msg": "3-32 位字母数字连字符"},
                 filterable=True, help="主 SKU，入库时统一大写"),
        FieldDef("sku_aliases", "SKU别名", "input", help="多个别名以顿号/分号分隔，自动大写去重"),
        FieldDef("params", "产品参数", "object_array",
                 rules={"custom": "param_whitelist_warn"},
                 options={"type": "static", "ref": "kb.product.param_whitelist"},
                 help="参数键值对；白名单外键告警放行（运营容错）"),
        FieldDef("price", "价格体系", "object",
                 rules={"custom": "price_ladder"}, help="阶梯价：MOQ 越高单价应越低；仅授权角色可见，不入向量正文"),
        FieldDef("selling_points", "卖点话术", "object_array", help="逐条分块，卖点块按自身场景打标"),
        FieldDef("scenes", "应用场景", "multi_select", help="条目级应用场景标签"),
        FieldDef("certs", "认证证书", "object_array", help="过期/临期不阻断入库，自动标记 expired 并检索降权"),
        FieldDef("competitors", "竞品对比", "object_array", filterable=True, help="按竞品独立成块"),
        FieldDef("moq", "起订量", "number", rules={"min": 1}, help="AI 报价结构化取数来源"),
        FieldDef("lead_time_days", "交期(天)", "number", rules={"min": 1}, help="AI 报价结构化取数来源"),
        FieldDef("hs_code", "海关编码", "input"),
    ],
    # ── 客户知识库（FR-KB-002，SR-KB-020 §4 表 + SR-KB-002 §6.1 全字段）──
    "customer": [
        FieldDef("customer_ref", "关联客户", "select", True,
                 options={"type": "api", "ref": "/api/v1/customers?fields=uid,name_masked"},
                 visible_when={"field": "category", "in": list(CUSTOMER_CATEGORIES[:3])},
                 filterable=True, help="customer.uid；分级标准类不关联具体客户"),
        FieldDef("category", "内容类别", "select", True, options=_static(CUSTOMER_CATEGORIES),
                 help="画像/沟通历史/成单丢单复盘/分级标准四类"),
        FieldDef("grade", "客户分级", "select", options=_static(CUSTOMER_GRADES),
                 filterable=True, help="条目形成时的分级快照（A/B/C/D）"),
        FieldDef("tags", "画像标签", "object",
                 rules={"custom": "tag_len_le_64"}, help="国家/行业/类型/规模/风险标记，禁入明文 PII"),
        FieldDef("deal_snapshot", "成单摘要", "object", help="{outcome, amount_band, at} 结构"),
        FieldDef("pii_digests", "PII指纹", "readonly",
                 help="HMAC-SHA256 指纹，供密文等值检索；明文联系方式禁入（NFR-SEC-01）"),
    ],
    # ── 销售技能库（FR-KB-003，SR-KB-020 §4 表 + SR-KB-003 §6.1 全字段）──
    "skill": [
        FieldDef("skill_kind", "技能类型", "select", True, options=_static(SKILL_KINDS),
                 help="话术模板/谈判策略/成功案例/失败案例"),
        FieldDef("stage", "适用阶段", "select", True, options=_static(SKILL_STAGES),
                 filterable=True, help="外贸全流程七阶段，与流程库节点对齐"),
        FieldDef("scene_tags", "场景标签", "multi_select", True,
                 options={"type": "taxonomy", "ref": "scene_taxonomy.json"},
                 filterable=True, help="主标签（首个）写入向量库 scene_tag，受控词表"),
        FieldDef("customer_types", "适用客户类型", "multi_select",
                 options={"type": "api", "ref": "/api/v1/knowledge/taxonomies/customer_type"},
                 help="C-01~ 类型库（SRS §6.3）"),
        FieldDef("customer_grades", "适用分级", "multi_select", options=_static(CUSTOMER_GRADES)),
        FieldDef("difficulty", "难度", "number", rules={"min": 1, "max": 3}, help="使用难度（对应培训分级）"),
        FieldDef("language", "语言", "multi_select", help="如 en/zh"),
        FieldDef("slots", "话术槽位", "object_array",
                 visible_when={"field": "skill_kind", "equals": "talk_track"},
                 rules={"custom": "slots_declared_for_talk_track"},
                 options={"type": "static", "values": list(SLOT_SOURCES)},
                 help="价格/交期槽位强制结构化来源（R-03）"),
        FieldDef("outcome", "案例结果", "select",
                 options=_static(["win", "lose", "neutral"]),
                 visible_when={"field": "skill_kind", "in": ["success_case", "failure_case"]}),
    ],
    # ── 流程知识库（FR-KB-004，SR-KB-020 §4 表 + SR-KB-004 §4 全字段）──
    "process": [
        FieldDef("category", "内容类别", "select", True, options=_static(PROCESS_CATEGORIES),
                 help="外贸SOP/平台规则红线/物流支付报关知识"),
        FieldDef("process_nodes", "关联节点", "multi_select", True,
                 options={"type": "api", "ref": "/api/v1/knowledge/process/nodes"},
                 filterable=True, help=f"预置主链 {' → '.join(PRESET_MAIN_CHAIN)}"),
        FieldDef("prev_nodes", "前置节点", "multi_select",
                 options={"type": "api", "ref": "/api/v1/knowledge/process/nodes"},
                 help="节点链 DAG 前置，供双向追踪"),
        FieldDef("next_nodes", "后置节点", "multi_select",
                 options={"type": "api", "ref": "/api/v1/knowledge/process/nodes"},
                 help="节点链 DAG 后置，供双向追踪"),
        FieldDef("is_redline", "红线标记", "switch",
                 help="勾选后必须填写三个机器可执行字段"),
        FieldDef("action_type", "拦截动作", "select", options=_static(PROCESS_ACTION_TYPES),
                 visible_when={"field": "is_redline", "equals": True},
                 help="与风控引擎 FR-AG-005 动作白名单对齐"),
        FieldDef("risk_level", "风险级别", "select", options=_static(["high", "medium"]),
                 visible_when={"field": "is_redline", "equals": True}),
        FieldDef("intercept_rule", "拦截规则", "object",
                 visible_when={"field": "is_redline", "equals": True},
                 rules={"custom": "intercept_rule_required_when_redline"},
                 help="keyword/regex/action + patterns，供拦截清单导出"),
        FieldDef("advice", "处置建议", "textarea", help="命中红线的建议话术"),
        FieldDef("rule_source", "规则出处", "input", help="如阿里国际站规则中心版本"),
        FieldDef("effective_at", "生效日期", "date"),
    ],
    # ── 负面案例库（FR-KB-005，SR-KB-020 §4 表 + SR-KB-005 §4 全字段）──
    "negative_case": [
        FieldDef("case_category", "案例大类", "select", True, options=_static(CASE_CATEGORIES),
                 help="丢单/投诉/错误话术/平台处罚"),
        FieldDef("reason_type", "原因分类", "cascader", True,
                 options={"type": "taxonomy", "ref": "NEGATIVE_CASE_REASON_TREE"},
                 filterable=True, help=f"两级分类树（{len(NEGATIVE_CASE_REASON_TREE)} 大类）"),
        FieldDef("scene_tag", "场景标签", "select",
                 options={"type": "taxonomy", "ref": "scene_taxonomy.json#top"},
                 filterable=True, help="与技能库共用词表"),
        FieldDef("occurred_at", "发生时间", "input", True,
                 rules={"pattern": "^\\d{4}(-\\d{2})?(-\\d{2})?$", "pattern_msg": "YYYY / YYYY-MM / YYYY-MM-DD（可模糊到月）"}),
        FieldDef("lost_order", "丢单详情", "object",
                 visible_when={"field": "case_category", "equals": "lost_order"},
                 rules={"custom": "detail_required_by_category"}),
        FieldDef("complaint", "投诉详情", "object",
                 visible_when={"field": "case_category", "equals": "complaint"},
                 rules={"custom": "detail_required_by_category"}),
        FieldDef("penalty", "处罚详情", "object",
                 visible_when={"field": "case_category", "equals": "platform_penalty"},
                 rules={"custom": "detail_required_by_category"}),
        FieldDef("script_pairs", "错误话术对照", "object_array",
                 visible_when={"field": "case_category", "equals": "wrong_script"},
                 rules={"custom": "script_pairs_required_for_wrong_script"},
                 help="错误话术-正确话术对照对，错误话术独立成向量子块"),
    ],
    # ── 新人带教素材库（FR-KB-006，SR-KB-020 §4 表 + SR-KB-006 §4 全字段）──
    "training": [
        FieldDef("material_kind", "素材类型", "select", True, options=_static(TRAINING_MATERIAL_KINDS),
                 help="课时正文/考试题干/模拟场景剧本（课程包为结构化引用）"),
        FieldDef("course_code", "课程包编码", "select", True,
                 visible_when={"field": "material_kind", "equals": "lesson"},
                 options={"type": "api", "ref": "/api/v1/knowledge/training/courses"},
                 help="结构化课程包引用（导入幂等键）"),
        FieldDef("course_title", "课程包标题", "input", help="展示用冗余字段"),
        FieldDef("seq", "课时顺序", "number", True, rules={"min": 1},
                 visible_when={"field": "material_kind", "equals": "lesson"},
                 help="学习路径按此展开"),
        FieldDef("scene_tag", "场景标签", "select",
                 options={"type": "taxonomy", "ref": "scene_taxonomy.json"},
                 filterable=True, help="与销售技能库联动"),
        FieldDef("question_type", "题型", "select", True, options=_static(QUESTION_TYPES),
                 visible_when={"field": "material_kind", "equals": "question"},
                 help="单选/多选/判断/简答/案例；答案与评分要点永不入库 metadata"),
        FieldDef("knowledge_points", "知识点", "multi_select",
                 options={"type": "api", "ref": "/api/v1/knowledge/taxonomies/knowledge_point"},
                 filterable=True, help="组卷蓝图过滤维度"),
        FieldDef("ability_dims", "能力维度", "object",
                 rules={"custom": "ability_dims_sum_one"},
                 help="产品/沟通/议价/平台四维权重（雷达回填依据，和为 1）"),
        FieldDef("difficulty", "难度", "number", rules={"min": 1, "max": 5}),
        FieldDef("est_minutes", "预计分钟", "number", rules={"min": 1},
                 visible_when={"field": "material_kind", "equals": "lesson"}),
        FieldDef("scenario_key", "场景标识", "input", True,
                 visible_when={"field": "material_kind", "equals": "scenario"},
                 help="对齐 exercise.scenario_key 的稳定标识"),
        FieldDef("customer_type", "模拟客户类型", "select", True,
                 visible_when={"field": "material_kind", "equals": "scenario"},
                 options={"type": "api", "ref": "/api/v1/knowledge/taxonomies/customer_type"},
                 help="C-01~ 类型库（SRS §6.3 最低配置）"),
        FieldDef("stage_node", "流程节点", "select", True,
                 visible_when={"field": "material_kind", "equals": "scenario"},
                 options=_static(("inquiry", "quote", "negotiation", "sample", "order", "logistics", "after_sales")),
                 filterable=True, help="场景须覆盖外贸全流程节点（SRS §6.3）"),
        FieldDef("opening_context", "开场情境", "textarea", True,
                 visible_when={"field": "material_kind", "equals": "scenario"},
                 help="AI 客户第一句 + 背景设定（FR-TR-002 Prompt 输入）"),
        FieldDef("hidden_needs", "隐藏诉求", "multi_select", help="复盘时对照"),
        FieldDef("assessment_points", "考核点", "object_array",
                 visible_when={"field": "material_kind", "equals": "scenario"},
                 help="[{dim, desc, weight}] 能力维度考核点"),
    ],
}

#: 六库 Pydantic 元数据模型索引（注册表一致性自检的对照面；kb_type 判别字段除外）
META_MODELS: dict[str, type[BaseModel]] = {
    "product": ProductMeta,
    "customer": CustomerMeta,
    "skill": SkillMeta,
    "process": ProcessMeta,
    "negative_case": NegativeCaseMeta,
    "training": TrainingMeta,
}


def registry_issues() -> list[str]:
    """注册表完备性自检：返回问题清单（空列表 = 通过）。

    检查项（供 FR-KB-020/021 启动自检与单元测试共用）：
    1. 六库全覆盖（键集与 KBType 取值一致），字段无重复键；
    2. 中文 label 非空、控件类型在 WIDGETS 词表内；
    3. 键集与各库 Pydantic 模型字段集逐一相等（判别字段 kb_type 除外）——单一事实源守护；
    4. 模型必填字段必须在注册表中 required=True；注册表额外的必填必须落在
       _CONDITIONAL_REQUIRED 白名单内（模型层按条件校验的条件必填字段）。
    """
    issues: list[str] = []
    expected_kbs = {member.value for member in KBType}
    if set(REGISTRY) != expected_kbs:
        issues.append(f"注册表必须恰好覆盖六大知识库 {sorted(expected_kbs)}，实际 {sorted(REGISTRY)}")
    if set(META_MODELS) != expected_kbs:
        issues.append(f"META_MODELS 必须恰好覆盖六大知识库 {sorted(expected_kbs)}，实际 {sorted(META_MODELS)}")

    for kb_type, field_defs in REGISTRY.items():
        model = META_MODELS.get(kb_type)
        if model is None:
            continue  # 上面已记问题，此处跳过避免二次报错

        keys = [f.key for f in field_defs]
        if len(keys) != len(set(keys)):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            issues.append(f"[{kb_type}] 字段键重复：{dupes}")
        for f in field_defs:
            if not f.label or not f.label.strip():
                issues.append(f"[{kb_type}] 字段 {f.key} 的中文 label 不能为空")
            if not f.key or not f.key.strip():
                issues.append(f"[{kb_type}] 存在空字段键：{f!r}")
            if f.widget not in WIDGETS:
                issues.append(f"[{kb_type}] 字段 {f.key} 控件类型 {f.widget!r} 不在 WIDGETS 词表内")

        model_fields = set(model.model_fields) - {"kb_type"}
        registry_keys = set(keys)
        if registry_keys != model_fields:
            missing = sorted(model_fields - registry_keys)
            extra = sorted(registry_keys - model_fields)
            issues.append(f"[{kb_type}] 注册表与模型字段不一致：缺失 {missing}，多余 {extra}")

        model_required = {
            name for name, f in model.model_fields.items()
            if f.is_required() and name != "kb_type"
        }
        registry_required = {f.key for f in field_defs if f.required}
        undeclared = sorted(model_required - registry_required)
        if undeclared:
            issues.append(f"[{kb_type}] 模型必填字段未在注册表标记必填：{undeclared}")
        conditional = sorted(registry_required - model_required - _CONDITIONAL_REQUIRED)
        if conditional:
            issues.append(f"[{kb_type}] 注册表必填字段未获模型支撑（不在条件必填白名单）：{conditional}")
    return issues


def get_schema(kb_type: str | KBType) -> dict[str, object]:
    """下发单库字段定义（JSON 就绪形态，供 /api/v1/knowledge/schemas 与模板生成器）。

    返回 {label, fields:[FieldDef 字典]}；label 为知识库中文名，help/rules/
    options/visible_when 随行携带，前端零拼装（SR-KB-020 §6.1 get_schema 契约）。
    """
    key = str(kb_type)
    if key not in REGISTRY:
        raise ValueError(f"kb_type 必须为六大知识库之一 {sorted(REGISTRY)}，收到：{key!r}")
    labels = {
        "product": "产品知识库",
        "customer": "客户知识库",
        "skill": "销售技能库",
        "process": "流程知识库",
        "negative_case": "负面案例库",
        "training": "新人带教素材库",
    }
    return {
        "label": labels[key],
        "fields": [asdict(f) for f in REGISTRY[key]],
    }


__all__ = [
    "WIDGETS",
    "FieldDef",
    "REGISTRY",
    "META_MODELS",
    "registry_issues",
    "get_schema",
]
