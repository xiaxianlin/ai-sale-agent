# 知识产权归甲方所有（COM-IP-01）
"""知识库清洗包（FR-KB-011 数据清洗 + NFR-SEC-01 数据脱敏，SR-KB-011 §3 代码落点）。

模块划分（对应 SR-KB-011 §3 app/pipeline/clean/）：
- base       共享留痕结构：CleanAction diff 链 / CleanedBlock / CleanedDoc / ParsedBlock（§6.1）
- dedup      全量 md5 + simhash 64 位 4 段查表近似去重（§6.2 / §6.3）
- correction 纠错规则引擎：格式 / 日期 / 错别字 / 单位，规则可注册（§6.4）
- masking    统一脱敏引擎 MaskingEngine + 可逆 vault 占位接口（SR-NFR-SEC-01 §6.1 / §6.2）
- pipeline   三段编排：去重 → 纠错 → 脱敏，全程 CleanAction 留痕（§6.6）
"""
from .base import (
    CleanAction,
    CleanedBlock,
    CleanedDoc,
    ParsedBlock,
    STAGE_CORRECT,
    STAGE_DEDUP,
    STAGE_MASK,
    STATUS_DROPPED_EXACT,
    STATUS_DROPPED_NEAR,
    STATUS_KEPT,
    table_repr,
)
from .correction import (
    ActionRecorder,
    CorrectionRule,
    Corrector,
    DateFormatRule,
    FormatNormalizeRule,
    TypoMapRule,
    UnitNormalizeRule,
    default_corrector,
)
from .dedup import (
    ExactDeduper,
    Fingerprint,
    FingerprintStore,
    InMemoryFingerprintStore,
    NearDeduper,
    SimHash,
    hamming_distance,
    md5_fingerprint,
    simhash_segments,
)
from .masking import (
    Finding,
    InMemoryVault,
    MaskResult,
    MaskRule,
    MaskingEngine,
    RuleSet,
    Vault,
    content_digest,
    default_ruleset,
    passes_luhn,
)
from .pipeline import CleaningPipeline, build_pipeline

__all__ = [
    "CleanAction",
    "CleanedBlock",
    "CleanedDoc",
    "ParsedBlock",
    "STAGE_CORRECT",
    "STAGE_DEDUP",
    "STAGE_MASK",
    "STATUS_DROPPED_EXACT",
    "STATUS_DROPPED_NEAR",
    "STATUS_KEPT",
    "table_repr",
    "ActionRecorder",
    "CorrectionRule",
    "Corrector",
    "DateFormatRule",
    "FormatNormalizeRule",
    "TypoMapRule",
    "UnitNormalizeRule",
    "default_corrector",
    "ExactDeduper",
    "Fingerprint",
    "FingerprintStore",
    "InMemoryFingerprintStore",
    "NearDeduper",
    "SimHash",
    "hamming_distance",
    "md5_fingerprint",
    "simhash_segments",
    "Finding",
    "InMemoryVault",
    "MaskResult",
    "MaskRule",
    "MaskingEngine",
    "RuleSet",
    "Vault",
    "content_digest",
    "default_ruleset",
    "passes_luhn",
    "CleaningPipeline",
    "build_pipeline",
]
