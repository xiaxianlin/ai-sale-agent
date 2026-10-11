# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""考试认证系统核心包（FR-TR-007，迭代 1.3；SR-TR-007 §6 工程实现）。

模块划分：
- :mod:`paper`——AI 组卷：薄弱维蓝图（确定性配额）/ 校验修复 / 种子化抽题与降级链 / 试卷快照；
- :mod:`proctor`——在线监考：事件 → 证据链 → 加权规则 → 阈值终判（宁漏勿误，阈值配置化）；
- :mod:`service`——应用服务：评分编排（客观精确 + 主观 rubric/降级）/ cert_status 状态机 /
  重考策略 / 上岗拦截数据源与审计（require_certified 依赖在 api 层）。

与并行交付的 loop（FR-TR-008 闭环）互不依赖；本包为 exam_api 独占后端。
"""
from services.training.core.exam.paper import (
    BankInsufficientError,
    Blueprint,
    BlueprintInvalidError,
    DIMENSIONS,
    InMemoryQuestionBank,
    Paper,
    PaperGenerator,
    QuestionBank,
    QuestionBankItem,
    build_blueprint,
    build_demo_questions,
    is_objective,
    validate_and_repair,
)
from services.training.core.exam.proctor import (
    ProctorConfig,
    ProctorEvent,
    ProctorFlag,
    ProctorSession,
)
from services.training.core.exam.service import (
    CERT_CERTIFIED,
    CERT_EXPIRED,
    CERT_UNCERTIFIED,
    AttemptCooldownError,
    AttemptExceededError,
    CertRequiredError,
    CertTransitionError,
    ExamConfig,
    ExamNotFoundError,
    ExamNotOngoingError,
    ExamOngoingError,
    ExamOutcome,
    ExamRecord,
    ExamService,
    ExamStore,
    FakeRubricScorer,
    InMemoryExamStore,
    ReviewNotPendingError,
    ScoringError,
    SubjectiveScorer,
    Trainee,
    transition_cert,
)

__all__ = [
    # paper
    "DIMENSIONS",
    "BankInsufficientError",
    "Blueprint",
    "BlueprintInvalidError",
    "InMemoryQuestionBank",
    "Paper",
    "PaperGenerator",
    "QuestionBank",
    "QuestionBankItem",
    "build_blueprint",
    "build_demo_questions",
    "is_objective",
    "validate_and_repair",
    # proctor
    "ProctorConfig",
    "ProctorEvent",
    "ProctorFlag",
    "ProctorSession",
    # service
    "CERT_CERTIFIED",
    "CERT_EXPIRED",
    "CERT_UNCERTIFIED",
    "AttemptCooldownError",
    "AttemptExceededError",
    "CertRequiredError",
    "CertTransitionError",
    "ExamConfig",
    "ExamNotFoundError",
    "ExamNotOngoingError",
    "ExamOngoingError",
    "ExamOutcome",
    "ExamRecord",
    "ExamService",
    "ExamStore",
    "FakeRubricScorer",
    "InMemoryExamStore",
    "ReviewNotPendingError",
    "ScoringError",
    "SubjectiveScorer",
    "Trainee",
    "transition_cert",
]
