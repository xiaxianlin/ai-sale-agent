# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 包（FR-SC-005，SR-SC-005；Phase 4 第二批）。

三核心（读写分离：监控不直连 28 容器；批量不回滚；大盘只读预聚合）：
- :mod:`wall`——心跳汇聚器：健康三态判定 → 快照版本号 + WS 增量帧语义；
- :mod:`batch`——批量执行器：子项级事务、部分失败清单、仅重试失败项、幂等；
- :mod:`roi`——ROI 统计：账号级/整体口径映射 + metric_daily 预聚合（幂等重跑）。

依据文档：docs/07-实现方案/SC/SR-SC-005-管理后台V3.md §6；SRS §9.2（FR-SC-005）。
"""

from services.agent.core.adminv3.batch import ACTIONS, BatchError, BatchItem, BatchOps, BatchTask
from services.agent.core.adminv3.roi import InquiryFact, RoiStats
from services.agent.core.adminv3.wall import ClusterWall, Heartbeat, InstanceMeta

__all__ = [
    "ACTIONS", "BatchError", "BatchItem", "BatchOps", "BatchTask",
    "InquiryFact", "RoiStats",
    "ClusterWall", "Heartbeat", "InstanceMeta",
]
