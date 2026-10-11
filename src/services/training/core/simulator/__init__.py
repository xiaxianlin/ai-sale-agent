# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟客户系统包（FR-TR-002，SR-TR-002 §3 代码落点；迭代 1.2）。

模块划分：
- :mod:`persona`——PersonaSpec/ScenarioSpec 数据契约 + 内置 C-01~C-06 + 扩展
  注册接口（≥10 目录守卫）+ System Prompt 组装；
- :mod:`engine`——确定性状态机（议价立场/情绪/耐心）+ 对话循环 + 难度动态
  调节 + 结束三分支判定 + LLM 可插拔协议与确定性 Fake 桩；
- :mod:`store`——SimSessionStore 协议 + 内存实现（request_id 幂等重放）；
- :mod:`debrief`——复盘报告生成器（scores 三维/建议三段）+ 复盘存储协议与
  内存实现。

demo 口径：LLM 全部走可插拔 Protocol + 确定性 Fake 实现（零网络请求）；
生产注入 DeepSeek-V3 客户端（RFP §3.2 指定栈），协议不变。
"""
from services.training.core.simulator.debrief import (
    DebriefGenerator,
    DebriefLlm,
    DebriefReport,
    DebriefStore,
    FakeDebriefLlm,
    InMemoryDebriefStore,
    PointResult,
)
from services.training.core.simulator.engine import (
    DEFAULT_MAX_ROUNDS,
    CustomerState,
    CustomerStateMachine,
    FakeSimulatorLlm,
    SimAlreadyEnded,
    SimBadRequest,
    SimNotFoundError,
    SimSession,
    SimSessionNotFound,
    SimulatorEngine,
    SimulatorError,
    SimulatorLlm,
    SimulatorLlmError,
    stance_label,
)
from services.training.core.simulator.persona import (
    BUILT_IN_PERSONAS,
    BUILT_IN_SCENARIOS,
    CatalogInsufficientError,
    DEMO_EXTENSION_PERSONAS,
    MIN_PERSONA_TYPES,
    PersonaNotFoundError,
    PersonaRegistry,
    PersonaSpec,
    ScenarioCatalog,
    ScenarioNotFoundError,
    ScenarioSpec,
    StateInit,
    build_system_prompt,
)
from services.training.core.simulator.store import (
    InMemorySimSessionStore,
    SimSessionStore,
    START_REPLAY_SCOPE,
)

__all__ = [
    # persona
    "BUILT_IN_PERSONAS",
    "BUILT_IN_SCENARIOS",
    "DEMO_EXTENSION_PERSONAS",
    "MIN_PERSONA_TYPES",
    "CatalogInsufficientError",
    "PersonaNotFoundError",
    "ScenarioNotFoundError",
    "PersonaRegistry",
    "ScenarioCatalog",
    "PersonaSpec",
    "ScenarioSpec",
    "StateInit",
    "build_system_prompt",
    # engine
    "DEFAULT_MAX_ROUNDS",
    "CustomerState",
    "CustomerStateMachine",
    "SimSession",
    "SimulatorEngine",
    "SimulatorLlm",
    "FakeSimulatorLlm",
    "SimulatorError",
    "SimBadRequest",
    "SimAlreadyEnded",
    "SimNotFoundError",
    "SimSessionNotFound",
    "SimulatorLlmError",
    "stance_label",
    # store
    "SimSessionStore",
    "InMemorySimSessionStore",
    "START_REPLAY_SCOPE",
    # debrief
    "DebriefGenerator",
    "DebriefLlm",
    "FakeDebriefLlm",
    "DebriefReport",
    "PointResult",
    "DebriefStore",
    "InMemoryDebriefStore",
]
