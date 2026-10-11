# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""错峰调度器（FR-SC-004-②，SR-SC-004 §6.2；账号级操作时段错峰，demo 进程内形态）。

职责（多账号关联风险的时间维解耦，§17 R-06）：
- **窗口黄金比例错开**（§6.2 ①）：以黄金比例分割（低差异序列）把每账号工作窗口起点
  在 ±spread 域内两两错开——28 账号窗口起点两两互异（SR §9 验收线「无两账号同窗口」），
  窗口时长保持（平移不错切）；账号缩容（10 账号 P2 形态）算法同样成立（NFR-SCA-01）；
- **槽位配额**（§6.2 ②）：当日在线按槽位分配，同槽在线账号 ≤ slot_quota（默认 10），
  每账号至少 1 槽（公平性下界，不允许饿死）；分配确定性/幂等（同日两次分配一致）；
- **计划复现**：plan(day) 纯函数（无共享可变状态），多线程并发取计划结果一致。

demo 形态边界：槽位账本由内存承载（生产 = Redis cl:stagger:{day} + 每日预排任务）；
黄金比例常数取 φ = 0.6180339887…（无理数保证 frac(kφ) 两两互异——三间隙定理下
N 点最小间隔恒正，不聚堆）。
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from datetime import date, datetime, time, timedelta

#: 黄金比例（低差异序列乘数：frac(k·φ) 均匀铺满 [0,1) 且两两互异）
GOLDEN_RATIO = 0.6180339887498949

#: 默认基准工作窗口与错峰幅度（SR §7 sc.stagger.* 配置外置口径）
DEFAULT_WINDOW_START = time(8, 30)
DEFAULT_WINDOW_END = time(18, 30)
DEFAULT_SPREAD_MIN = 45.0

#: 默认槽位参数：30 分钟粒度、同槽在线配额 10（28 账号 3 槽即可承载）
DEFAULT_SLOT_MINUTES = 30
DEFAULT_SLOT_QUOTA = 10


class StaggerScheduler:
    """错峰调度器：窗口起点黄金比例错开 + 槽位配额分配（纯函数式，无状态竞争）。

    :param account_ids: 参与错峰的账号清单（顺序决定错峰序列，缩容同样成立）；
    :param window_start/window_end: 基准工作窗口（当日本地时；
    :param spread_min: 窗口起点错峰幅度（± 分钟域）；
    :param slot_minutes/slot_quota: 槽位粒度与同槽在线配额。
    """

    def __init__(
        self,
        account_ids: list[int],
        *,
        window_start: time = DEFAULT_WINDOW_START,
        window_end: time = DEFAULT_WINDOW_END,
        spread_min: float = DEFAULT_SPREAD_MIN,
        slot_minutes: int = DEFAULT_SLOT_MINUTES,
        slot_quota: int = DEFAULT_SLOT_QUOTA,
    ) -> None:
        if spread_min <= 0 or slot_minutes <= 0 or slot_quota <= 0:
            raise ValueError("spread_min/slot_minutes/slot_quota 必须为正")
        self._accounts = list(account_ids)
        self._index: dict[int, int] = {a: i for i, a in enumerate(self._accounts)}
        self._window_start = window_start
        self._base_duration = (datetime.combine(date(2000, 1, 1), window_end)
                               - datetime.combine(date(2000, 1, 1), window_start))
        self._spread_min = spread_min
        self._slot_minutes = slot_minutes
        self._slot_quota = slot_quota
        self._lock = threading.Lock()  # plan/allocate 为纯函数，锁仅为语义完备性

    # ---- 窗口错峰（§6.2 ①） ----

    def window_for(self, account_id: int, *, day: date) -> tuple[datetime, datetime]:
        """账号当日工作窗口：(起点黄金比例偏移, 基准窗口时长保持)。

        :raises KeyError: 未登记账号拒绝（fail fast 防错配）。
        """
        if account_id not in self._index:
            raise KeyError(f"账号未参与错峰调度：{account_id}")
        idx = self._index[account_id]
        # 黄金比例低差异序列：frac((k+1)·φ) ∈ [0,1) 两两互异 → 偏移两两互异（无同窗口）
        frac = ((idx + 1) * GOLDEN_RATIO) % 1.0
        offset_min = -self._spread_min + 2.0 * self._spread_min * frac
        start = (datetime.combine(day, self._window_start)
                 + timedelta(minutes=offset_min))
        return start, start + self._base_duration  # 平移不错切时长

    def plan(self, *, day: date) -> list[tuple[int, datetime, datetime]]:
        """当日错峰计划：按登记顺序输出 (account_id, start, end)（确定性可复现）。"""
        with self._lock:
            return [(a, *self.window_for(a, day=day)) for a in self._accounts]

    # ---- 槽位配额（§6.2 ②） ----

    def allocate_slots(self, *, day: date) -> dict[int, list[int]]:
        """当日在线槽位分配：同槽在线 ≤ slot_quota、每账号至少 1 槽（确定性幂等）。

        算法：槽位数 = ceil(N / quota)（配额下界的最少槽切分）；账号按窗口起点升序
        轮转落入槽位——错峰起点序即公平轮转序，任意槽负载 ≤ ceil(N/槽位) ≤ quota。
        """
        with self._lock:
            n = len(self._accounts)
            slot_count = max(1, math.ceil(n / self._slot_quota))
            ordered = sorted(self._accounts, key=lambda a: self.window_for(a, day=day)[0])
            allocation: dict[int, list[int]] = {a: [] for a in self._accounts}
            for i, account_id in enumerate(ordered):
                allocation[account_id].append(i % slot_count)
            return allocation
