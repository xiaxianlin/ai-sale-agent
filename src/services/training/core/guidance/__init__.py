# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训端引导式交互核心包（NFR-USA-03，迭代 1.1）。

子模块（本包为 guidance 独占子包，只承载引导能力，不侵入其他培训业务模块）：
- ``config``：TourStep / TourConfig / TourRegistry + 内置 v1 首登导览（版本单调递增校验）；
- ``state``：TraineeGuidanceState + 存储协议与内存实现 + 克制门控 should_show / mark_*；
- ``service``：GuidanceService（导览获取与版本比较、状态上报）+ 8 类空状态文案注册表。
"""
from services.training.core.guidance.config import (
    DEFAULT_TOUR_ID,
    FIRST_LOGIN_TOUR,
    TourConfig,
    TourRegistry,
    TourStep,
    TourVersionError,
    build_default_registry,
    parse_tour_version,
)
from services.training.core.guidance.service import (
    EMPTY_STATE_REGISTRY,
    EmptyStateSpec,
    GuidanceService,
)
from services.training.core.guidance.state import (
    GuidanceStateStore,
    InMemoryGuidanceStateStore,
    TraineeGuidanceState,
    from_localstorage_payload,
    localstorage_key,
    mark_done,
    mark_skipped,
    should_show,
    to_localstorage_payload,
)

__all__ = [
    "DEFAULT_TOUR_ID",
    "EMPTY_STATE_REGISTRY",
    "FIRST_LOGIN_TOUR",
    "EmptyStateSpec",
    "GuidanceService",
    "GuidanceStateStore",
    "InMemoryGuidanceStateStore",
    "TourConfig",
    "TourRegistry",
    "TourStep",
    "TraineeGuidanceState",
    "TourVersionError",
    "build_default_registry",
    "from_localstorage_payload",
    "localstorage_key",
    "mark_done",
    "mark_skipped",
    "parse_tour_version",
    "should_show",
    "to_localstorage_payload",
]
