# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 ①客户分级：RFM 变体加权评分 + 人工改写留痕（FR-SC-003①，SR-SC-003 §6.1）。

- 四因子（各归一化 0-100）：订单额 / 成单频次 / 最近交互 / 渠道来源加权；
- 权重与阈值全配置注入（app_config 免发版调参，NFR-MAI-02；OI-06 调研后校准）；
- 分级桶：≥80 A / ≥60 B / ≥40 C / 其余 D（``thresholds`` 两侧边界显式断言）；
- 自动重算（source=auto）每日批量；主管人工改写（source=manual）必填理由留痕，
  并进入保护期——期内自动重算不得覆盖（尊重主管判断，并发安全语义）；
- ``score_breakdown`` 逐因子留痕 = 分级准确性抽检（SRS §16.5 建议一致率 ≥80%）
  的可解释依据。

demo 形态：内存客户源（生产 = PG customer/customer_grade_log，FR-KB-002 档案）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

#: 分级全集（customer_grade_log CHECK 口径）
GRADES: tuple[str, ...] = ("A", "B", "C", "D")
#: 四因子默认权重（SR-SC-003 §7 strategy.grade.weights）
DEFAULT_WEIGHTS: dict[str, float] = {"amount": 0.35, "freq": 0.25, "recency": 0.25, "channel": 0.15}
#: 默认分级切分线（SR-SC-003 §7 strategy.grade.thresholds；低于 C 线为 D）
DEFAULT_THRESHOLDS: dict[str, float] = {"A": 80.0, "B": 60.0, "C": 40.0}
#: 渠道来源加权（老客户返单/独立站留资 > 新询盘；未知渠道取最低默认）
CHANNEL_SCORES: dict[str, float] = {
    "return_order": 100.0, "site_leads": 80.0, "exhibition": 60.0, "new_inquiry": 50.0,
}
UNKNOWN_CHANNEL_SCORE: float = 40.0


class GradingError(Exception):
    """分级域业务异常（code 对齐 04-接口设计 §1.3：40001 参数/校验类）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CustomerProfile:
    """客户档案（customer 表 + 客户知识库投影；FR-KB-002）。"""

    customer_id: int
    deal_amounts: tuple[float, ...] = ()          # 近 12 月订单额（USD）
    last_inbound_at: datetime | None = None       # 最近买家入站时刻
    channels: tuple[str, ...] = ()                # 渠道来源（return_order/site_leads/…）
    grade: str = "D"                              # 当前分级（回写目标列）


@dataclass(frozen=True)
class GradeChange:
    """一次分级变更（customer_grade_log 行语义；auto/manual 均追加留痕）。"""

    customer_id: int
    old_grade: str
    new_grade: str
    source: str                                   # auto / manual
    score: float | None = None                    # auto 加权总分（0-100）
    score_breakdown: dict[str, object] = field(default_factory=dict)
    reason: str = ""                              # manual 必填理由；auto 记批次号
    effective_until: datetime | None = None       # manual 保护期截止
    note: str = ""                                # unchanged / manual_protected / ""


class CustomerSource:
    """客户档案源协议（鸭子类型注入接缝；生产 = PG 查询形态）。"""

    def profile(self, customer_id: int) -> CustomerProfile:
        """读取客户档案；不存在抛 KeyError。"""
        raise NotImplementedError

    def update_grade(self, customer_id: int, grade: str) -> None:
        """回写 customer.grade 当前值。"""
        raise NotImplementedError


class InMemoryCustomerSource(CustomerSource):
    """内存客户源（demo Fake；线程安全）。"""

    def __init__(self, profiles: Mapping[int, CustomerProfile] | None = None) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[int, CustomerProfile] = dict(profiles or {})

    def upsert(self, profile: CustomerProfile) -> None:
        """登记/覆盖客户档案（测试夹具入口）。"""
        with self._lock:
            self._profiles[profile.customer_id] = profile

    def profile(self, customer_id: int) -> CustomerProfile:
        with self._lock:
            if customer_id not in self._profiles:
                raise GradingError(40001, f"客户 {customer_id} 不存在")
            return self._profiles[customer_id]

    def update_grade(self, customer_id: int, grade: str) -> None:
        with self._lock:
            current = self.profile(customer_id)
            self._profiles[customer_id] = replace(current, grade=grade)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """归一化截断到 [0, 100]。"""
    return max(low, min(high, value))


class CustomerGrader:
    """客户分级器：RFM 变体评分 → 落桶 → 回写留痕（SR-SC-003 §6.1 工程级形态）。

    :param source: 客户档案源（注入）；
    :param weights: 四因子权重（和为 1；app_config 即时生效语义）；
    :param thresholds: 分级切分线（A/B/C，低于 C 线为 D）；
    :param amount_ceiling: 订单额满分天花板（USD，归一化用）；
    :param freq_ceiling: 成单频次满分天花板（笔数）；
    :param recency_window_days: 最近交互衰减窗口（天，越近越高）；
    :param override_protect_days: 人工改写保护期（默认 90 天）；
    :param clock: 时钟注入（测试拨钟/保护期推演）。
    """

    def __init__(
        self,
        source: CustomerSource,
        *,
        weights: Mapping[str, float] | None = None,
        thresholds: Mapping[str, float] | None = None,
        amount_ceiling: float = 50000.0,
        freq_ceiling: int = 12,
        recency_window_days: float = 30.0,
        override_protect_days: int = 90,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self.weights: dict[str, float] = dict(weights) if weights else dict(DEFAULT_WEIGHTS)
        self.thresholds: dict[str, float] = dict(thresholds) if thresholds else dict(DEFAULT_THRESHOLDS)
        self._amount_ceiling = amount_ceiling
        self._freq_ceiling = freq_ceiling
        self._recency_window = recency_window_days
        self._protect_days = override_protect_days
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self._grade_log: list[GradeChange] = []       # customer_grade_log 内存形态
        self._protected_until: dict[int, datetime] = {}  # customer_id → 保护期截止

    # ---------------------------------------------------------------- 评分与落桶

    def score_breakdown(self, profile: CustomerProfile, *, now: datetime | None = None) -> dict[str, object]:
        """RFM 变体四因子评分（明细可解释，抽检证据）。"""
        moment = now if now is not None else self._clock()
        amount = sum(profile.deal_amounts)
        f_amount = _clamp(amount / self._amount_ceiling * 100.0) if amount > 0 else 0.0
        f_freq = _clamp(len(profile.deal_amounts) / self._freq_ceiling * 100.0)
        if profile.last_inbound_at is None:
            f_recency = 0.0                            # 降级：缺失交互按 0 分，不抛异常
        else:
            days = max((moment - profile.last_inbound_at).total_seconds() / 86400.0, 0.0)
            f_recency = _clamp(100.0 * (1.0 - days / self._recency_window))
        if profile.channels:
            f_channel = max(CHANNEL_SCORES.get(c, UNKNOWN_CHANNEL_SCORE) for c in profile.channels)
        else:
            f_channel = 0.0                            # 无渠道数据按 0 分（与未知渠道名区分）
        score = round(
            self.weights["amount"] * f_amount + self.weights["freq"] * f_freq
            + self.weights["recency"] * f_recency + self.weights["channel"] * f_channel, 2)
        return {
            "factors": {"amount": round(f_amount, 2), "freq": round(f_freq, 2),
                        "recency": round(f_recency, 2), "channel": round(f_channel, 2)},
            "weights": dict(self.weights),
            "score": score,
        }

    def bucket(self, score: float) -> str:
        """落桶：≥A 线 A；≥B 线 B；≥C 线 C；其余 D（阈值两侧边界由测试断言）。"""
        if score >= self.thresholds["A"]:
            return "A"
        if score >= self.thresholds["B"]:
            return "B"
        if score >= self.thresholds["C"]:
            return "C"
        return "D"

    # ---------------------------------------------------------------- 自动重算

    def is_protected(self, customer_id: int, *, now: datetime | None = None) -> bool:
        """人工改写保护期内判定（期内 auto 不得覆盖）。"""
        moment = now if now is not None else self._clock()
        until = self._protected_until.get(customer_id)
        return until is not None and moment < until

    def regrade(self, customer_id: int, *, batch_no: str) -> GradeChange:
        """自动重算（source=auto）：评分 → 保护期检查 → 回写留痕（同值不写）。"""
        with self._lock:
            profile = self.source.profile(customer_id)
            breakdown = self.score_breakdown(profile)
            score = float(breakdown["score"])  # type: ignore[arg-type]
            grade = self.bucket(score)
            if self.is_protected(customer_id):
                return GradeChange(customer_id, profile.grade, profile.grade, "auto",
                                   note="manual_protected")
            if grade == profile.grade:
                return GradeChange(customer_id, grade, grade, "auto", score=score,
                                   score_breakdown=breakdown, reason=batch_no, note="unchanged")
            self.source.update_grade(customer_id, grade)
            change = GradeChange(customer_id, profile.grade, grade, "auto", score=score,
                                 score_breakdown=breakdown, reason=batch_no)
            self._grade_log.append(change)
            return change

    # ---------------------------------------------------------------- 人工改写

    def manual_override(self, customer_id: int, new_grade: str, reason: str, *,
                        actor: str) -> GradeChange:
        """主管人工改写（source=manual）：reason 必填留痕 + 保护期 + 触发跟进重排由调用方承载。"""
        if not reason.strip():
            raise GradingError(40001, "改写必须填写理由（审计留痕必填）")
        if new_grade not in GRADES:
            raise GradingError(40001, f"非法分级键 {new_grade}（仅 {'/'.join(GRADES)}）")
        with self._lock:
            profile = self.source.profile(customer_id)
            until = self._clock() + timedelta(days=self._protect_days)
            self.source.update_grade(customer_id, new_grade)
            self._protected_until[customer_id] = until
            change = GradeChange(customer_id, profile.grade, new_grade, "manual",
                                 reason=reason, effective_until=until)
            self._grade_log.append(change)
            return change

    # ---------------------------------------------------------------- 观测面

    @property
    def grade_log(self) -> tuple[GradeChange, ...]:
        """留痕流水（customer_grade_log 只读快照）。"""
        with self._lock:
            return tuple(self._grade_log)


__all__ = [
    "GRADES", "DEFAULT_WEIGHTS", "DEFAULT_THRESHOLDS", "CHANNEL_SCORES",
    "GradingError", "CustomerProfile", "GradeChange", "CustomerSource",
    "InMemoryCustomerSource", "CustomerGrader",
]
