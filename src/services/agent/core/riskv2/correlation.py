# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""账号关联风险监控（FR-SC-004-②/§6.4，SR-SC-004 §6.4；多账号关联度量，demo 进程内形态）。

职责（§17 R-06 关联风险的两个监控维）：
- **操作时间相关系数**（§6.4 pair_corr_max）：按 10 分钟槽聚合各账号操作计数，
  两两计算 Pearson 相关（纯 Python，无 numpy 本地依赖——NFR-SEC-06 口径）；
  同槽同步操作 → ρ→1 超阈（> 0.6 严格大于）高级告警（含账号对明细）；错峰后形态
  互补 → ρ 低不告警（错峰机制有效性的可观测证明）；单账号无量测对 → 0.0 不误报；
- **跨账号内容相似率**（§6.4 content_similar_rate）：发送流中命中防同质守卫的占比，
  阈值 5%（≤ 5% 验收线内含不告警、> 5% 告警含分子分母明细）；零流量不除零；
- **评估与留痕**：evaluate() 汇总两项指标（结构稳定——仪表盘契约），逐项写审计行
  （仪表盘数据 + 审计双写，NFR-SEC-03）。

demo 形态边界：计数账本为内存字典（生产 = Redis 槽位计数 + 流水聚合）；告警经
alerts 通道（生产 = FR-AG-007），审计经注入 callable（生产 = audit_log 哈希链）。
"""

from __future__ import annotations

import math
import threading
from datetime import datetime
from itertools import combinations

#: 操作时间相关系数告警阈值（SR-SC-004 §6.4：超阈为严格大于）
PAIR_CORR_THRESHOLD = 0.6

#: 内容相似率告警阈值（≤ 5% 验收线内含）
CONTENT_RATE_THRESHOLD = 0.05

#: 操作槽位粒度（秒）：10 分钟槽对齐（SR-SC-004 §6.4 同槽判定口径）
OP_SLOT_SECONDS = 600


def pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson 相关系数（纯 Python）；零方差/长度不齐/空序列守卫返回 0.0（不产 NaN）。"""
    n = len(xs)
    if n == 0 or n != len(ys):
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0.0 or var_y == 0.0:  # 零方差序列：相关无定义 → 0.0 守卫
        return 0.0
    return cov / math.sqrt(var_x * var_y)


class CorrelationMonitor:
    """账号关联风险监控器：时间相关 + 内容相似率（线程安全计数，评估写审计）。

    :param alerts: 告警通道（send(level, type_, title, body)；生产 = FR-AG-007）；
    :param audit: 审计接收 callable(dict)（生产 = audit_log 哈希链；demo = list.append）；
    :param pair_corr_threshold: 时间相关系数告警阈值；
    :param content_rate_threshold: 内容相似率告警阈值。
    """

    def __init__(
        self,
        *,
        alerts=None,
        audit=None,
        pair_corr_threshold: float = PAIR_CORR_THRESHOLD,
        content_rate_threshold: float = CONTENT_RATE_THRESHOLD,
    ) -> None:
        self._alerts = alerts
        self._audit = audit if audit is not None else (lambda _row: None)
        self._pair_threshold = pair_corr_threshold
        self._rate_threshold = content_rate_threshold
        self._lock = threading.Lock()
        self._ops: dict[int, dict[int, int]] = {}  # account → (10min 槽 epoch) → 计数
        self.total_sends: int = 0                  # 发送总笔数（内容相似率分母）
        self._similar_hits: int = 0                # 防同质命中笔数（分子）

    # ---- 记录 ----

    def record_op(self, account_id: int, ts: datetime) -> None:
        """记录一次账号操作（按 10 分钟槽聚合——相关系数的原始观测）。"""
        slot = int(ts.timestamp()) // OP_SLOT_SECONDS
        with self._lock:
            slots = self._ops.setdefault(account_id, {})
            slots[slot] = slots.get(slot, 0) + 1

    def record_send(self, *, similar_hit: bool) -> None:
        """记录一次发送及是否命中防同质守卫（内容相似率分子分母）。"""
        with self._lock:
            self.total_sends += 1
            if similar_hit:
                self._similar_hits += 1

    # ---- 度量 ----

    def content_similar_rate(self) -> float:
        """跨账号内容相似率 = 命中笔数 / 总笔数；零流量返回 0.0（不除零）。"""
        with self._lock:
            if self.total_sends == 0:
                return 0.0
            return self._similar_hits / self.total_sends

    def pair_corr_max(self) -> tuple[float, list[tuple[int, int, float]]]:
        """账号对操作时间相关系数最大值及 Top 明细（降序）。

        :returns: (最大 ρ, [(account_a, account_b, ρ), ...] 按 ρ 降序)；
            单账号/零观测无量测对 → (0.0, []) 不误报。
        """
        with self._lock:
            snapshot = {a: dict(slots) for a, slots in self._ops.items()}
        accounts = sorted(snapshot)
        if len(accounts) < 2:
            return 0.0, []
        pairs: list[tuple[int, int, float]] = []
        for a, b in combinations(accounts, 2):
            keys = set(snapshot[a]) | set(snapshot[b])
            xs = [float(snapshot[a].get(k, 0)) for k in keys]
            ys = [float(snapshot[b].get(k, 0)) for k in keys]
            pairs.append((a, b, pearson(xs, ys)))
        pairs.sort(key=lambda item: item[2], reverse=True)
        return pairs[0][2], pairs

    # ---- 评估（仪表盘契约 + 审计双写） ----

    def evaluate(self) -> dict:
        """汇总评估：两项指标状态判定 + 告警 + 逐项审计（出参结构稳定）。"""
        corr_value, top_pairs = self.pair_corr_max()
        corr_alert = corr_value > self._pair_threshold  # 超阈为严格大于
        rate = self.content_similar_rate()
        with self._lock:
            hits, total = self._similar_hits, self.total_sends
        rate_alert = rate > self._rate_threshold  # 恰等阈值不告警（验收线内含）
        report = {
            "pair_corr_max": {
                "status": "alert" if corr_alert else "ok",
                "value": round(corr_value, 4),
                "threshold": self._pair_threshold,
                "top_pairs": [(a, b, round(r, 4)) for a, b, r in top_pairs[:3]],
            },
            "content_similar_rate": {
                "status": "alert" if rate_alert else "ok",
                "rate": round(rate, 4),
                "hits": hits,
                "total": total,
                "threshold": self._rate_threshold,
            },
        }
        if corr_alert:  # 高级告警含账号对明细（处置定位用）
            a, b, r = top_pairs[0]
            self._alert("high", "risk_correlation",
                        f"账号操作时间强相关：{a}/{b} ρ={r:.2f}",
                        f"超过阈值 {self._pair_threshold}，存在关联风控暴露，"
                        "建议核查错峰配置与行为画像")
        if rate_alert:  # 内容相似率告警含分子分母明细
            self._alert("high", "risk_correlation",
                        f"跨账号内容相似率超阈：{rate:.1%}",
                        f"命中 {hits}/{total} 笔，超过阈值 {self._rate_threshold}，"
                        "请核查强制重写链路")
        self._audit({"event": "correlation_metric", "metric": "pair_corr_max",
                     "status": report["pair_corr_max"]["status"],
                     "value": report["pair_corr_max"]["value"]})
        self._audit({"event": "correlation_metric", "metric": "content_similar_rate",
                     "status": report["content_similar_rate"]["status"],
                     "rate": report["content_similar_rate"]["rate"]})
        return report

    # ---- 内部 ----

    def _alert(self, level: str, type_: str, title: str, body: str) -> None:
        """告警投递（best-effort：通道故障不影响监控主流程）。"""
        if self._alerts is None:
            return
        try:
            self._alerts.send(level, type_, title, body)
        except Exception:  # noqa: BLE001 —— 告警为旁路
            pass
