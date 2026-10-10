# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""六大知识库内容模型与入库路由（FR-KB-001~006，迭代 0.3）。

- kb_types：KBType 枚举 + 六库 metadata 模型 + discriminated union（KbMetadata）；
- item：KnowledgeItemCreate / KnowledgeItem（按 kb_type 自动校验 metadata）；
- router：维度 → VectorRecord 标量映射、敏感键剥离投影、红线拦截规则导出；
- registry：六库 Schema 注册表（FR-KB-020 动态表单 / FR-KB-021 导入模板单一事实源）。
"""
from .item import (
    ITEM_STATUSES,
    ItemStatus,
    KnowledgeItem,
    KnowledgeItemCreate,
)
from .kb_types import (
    ABILITY_DIMENSIONS,
    ALL_SCENE_TAGS,
    CASE_CATEGORIES,
    CUSTOMER_CATEGORIES,
    CUSTOMER_GRADES,
    CUSTOMER_TYPE_PATTERN,
    KBType,
    KbMetadata,
    NEGATIVE_CASE_REASON_TREE,
    PII_FORBIDDEN_METADATA_KEYS,
    PRESET_MAIN_CHAIN,
    PRESET_PROCESS_NODES,
    PRICE_CURRENCIES,
    PRICE_SLOT_NAMES,
    PROCESS_ACTION_TYPES,
    PROCESS_CATEGORIES,
    PROCESS_RISK_LEVELS,
    QUESTION_TYPES,
    SCENE_TAXONOMY,
    SCENE_VOCABULARY,
    SIM_STAGE_NODES,
    SKILL_KINDS,
    SKILL_STAGES,
    SLOT_SOURCES,
    STRUCTURED_SLOT_SOURCES,
    TRAINING_MATERIAL_KINDS,
    AbilityDims,
    AssessmentPoint,
    Cert,
    ComplaintDetail,
    Competitor,
    CustomerMeta,
    CustomerProfileTags,
    InterceptRule,
    LostOrderDetail,
    NegativeCaseMeta,
    PenaltyDetail,
    PriceBook,
    PriceFloor,
    PriceTier,
    ProcessMeta,
    ProductMeta,
    ScriptPair,
    SellingPoint,
    SkillMeta,
    SlotSpec,
    TrainingMeta,
    assert_no_pii_keys,
    validate_reason_path,
    validate_scene_tags,
)
from .registry import (
    META_MODELS,
    REGISTRY,
    WIDGETS,
    FieldDef,
    get_schema,
    registry_issues,
)
from .router import (
    DIM_FIELDS,
    DIM_PAYLOAD_KEYS,
    FLOOR_PRICE_AUTHORIZED_ONLY_MARKER,
    PRICE_REDACTED_MARKER,
    VECTOR_EXCLUDED_METADATA_KEYS,
    embedding_safe_metadata,
    export_intercept_rules,
    record_dims,
)

__all__ = [
    # kb_types
    "KBType",
    "KbMetadata",
    "ProductMeta", "PriceBook", "PriceTier", "PriceFloor", "SellingPoint", "Cert", "Competitor",
    "CustomerMeta", "CustomerProfileTags", "PII_FORBIDDEN_METADATA_KEYS", "assert_no_pii_keys",
    "SkillMeta", "SlotSpec", "validate_scene_tags",
    "ProcessMeta", "InterceptRule", "PRESET_PROCESS_NODES", "PRESET_MAIN_CHAIN",
    "NegativeCaseMeta", "ScriptPair", "LostOrderDetail", "ComplaintDetail", "PenaltyDetail",
    "validate_reason_path", "NEGATIVE_CASE_REASON_TREE",
    "TrainingMeta", "AbilityDims", "AssessmentPoint",
    "SKILL_KINDS", "SKILL_STAGES", "SLOT_SOURCES", "STRUCTURED_SLOT_SOURCES", "PRICE_SLOT_NAMES",
    "SCENE_TAXONOMY", "SCENE_VOCABULARY", "ALL_SCENE_TAGS",
    "PROCESS_CATEGORIES", "PROCESS_ACTION_TYPES", "PROCESS_RISK_LEVELS",
    "CASE_CATEGORIES", "CUSTOMER_CATEGORIES", "CUSTOMER_GRADES",
    "TRAINING_MATERIAL_KINDS", "QUESTION_TYPES", "ABILITY_DIMENSIONS",
    "SIM_STAGE_NODES", "CUSTOMER_TYPE_PATTERN", "PRICE_CURRENCIES",
    # item
    "ITEM_STATUSES", "ItemStatus", "KnowledgeItem", "KnowledgeItemCreate",
    # router
    "DIM_FIELDS", "DIM_PAYLOAD_KEYS", "VECTOR_EXCLUDED_METADATA_KEYS",
    "PRICE_REDACTED_MARKER", "FLOOR_PRICE_AUTHORIZED_ONLY_MARKER",
    "record_dims", "embedding_safe_metadata", "export_intercept_rules",
    # registry
    "WIDGETS", "FieldDef", "REGISTRY", "META_MODELS", "registry_issues", "get_schema",
]
