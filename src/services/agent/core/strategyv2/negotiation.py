# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 ③议价引擎：底线校验 + 让步阶梯 + 赠品组合（FR-SC-003③，SR-SC-003 §6.3）。

议价状态机：``l0 挂牌 → l1 首次让幅 → l2 次幅 → floor 底线价（最终报价）``。

红线（SRS §7.4 / SR-SC-003 §8，代码级保证而非约定）：
- ``price_floor`` 底线只在引擎内比较，QuoteHint 仅下发现阶段可报区间——
  AI 永不可见全量底线/挂牌/阶梯字段（``to_ai_payload`` 白名单出参）；
- 破底线在代码上不可达：``max(target, floor, customer_target)`` 硬约束；
- 议价成交后的平台改价/下单仍走敏感操作「起草 → 人工确认」。

降级：SKU 无底线行 → 不自动议价转人工（宁可不谈，不越权报价）；floor 阶段无
赠品可选 → 转人工；离谱压价（缺口 > human_gap_pct）→ 直接转人工（FR-AG-009）。

demo 形态：内存价格库/会话表（生产 = PG price_floor/bargain_session，§4）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

#: 让步阶段机（stage CHECK 口径；rejected/human 为终态分支）
STAGES: tuple[str, ...] = ("l0", "l1", "l2", "floor", "rejected", "deal", "human")
#: 阶梯推进映射（l2 之后为 floor 终点）
_NEXT_STAGE: dict[str, str] = {"l0": "l1", "l1": "l2", "l2": "floor"}
#: AI 出参白名单（结构性防泄漏：floor_price/list_price/阶梯字段永不出现）
AI_PAYLOAD_FIELDS: tuple[str, ...] = (
    "action", "stage", "allowed_min_price", "gift_candidates", "talk_track_tag",
)


class NegotiationError(Exception):
    """议价域业务异常（40001 参数/配置类）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GiftRule:
    """赠品档位（起订量门槛 → 赠品组合 + 预算）。"""

    min_qty: int
    gift_skus: tuple[str, ...]
    budget_usd: float = 0.0


@dataclass(frozen=True)
class PriceFloor:
    """底线价格行（price_floor 表语义；敏感表：引擎内比较，永不下发全量给 AI）。"""

    sku: str
    list_price: float
    floor_price: float
    currency: str = "USD"
    first_step_pct: float = 0.02                  # 首次让幅（相对挂牌）
    second_step_pct: float = 0.01                 # 次幅
    gift_rules: tuple[GiftRule, ...] = ()

    def __post_init__(self) -> None:
        if self.floor_price >= self.list_price:
            raise NegotiationError(40001, f"底线价 {self.floor_price} 须低于挂牌价 {self.list_price}")


class PriceFloorStore:
    """价格底线库协议（注入接缝；生产 = PG price_floor，RBAC 仅主管/管理员可读）。"""

    def floor_of(self, sku: str) -> PriceFloor | None:
        """取 SKU 现行有效底线；无记录返回 None（降级转人工）。"""
        raise NotImplementedError


class InMemoryFloorStore(PriceFloorStore):
    """内存价格库（demo Fake）。"""

    def __init__(self, floors: dict[str, PriceFloor] | None = None) -> None:
        self._lock = threading.RLock()
        self._floors: dict[str, PriceFloor] = dict(floors or {})

    def put(self, floor: PriceFloor) -> None:
        """登记/覆盖底线行（价格改版入口；在途会话走创建时快照不受影响）。"""
        with self._lock:
            self._floors[floor.sku] = floor

    def floor_of(self, sku: str) -> PriceFloor | None:
        with self._lock:
            return self._floors.get(sku)


@dataclass
class BargainSession:
    """议价会话（bargain_session 行语义；创建时快照底线，价格改版不影响在途）。"""

    conversation_id: str
    sku: str
    qty: int
    list_price: float                             # 创建时快照
    floor_price: float                            # 创建时快照（引擎内部持有）
    first_step_pct: float
    second_step_pct: float
    current_offer: float
    stage: str = "l0"
    concessions: list[dict[str, object]] = field(default_factory=list)  # 让步审计
    status: str = "open"                          # open / closed


@dataclass(frozen=True)
class QuoteHint:
    """报价建议（agent-core 可见的全部信息：无 floor_price、无阶梯全景，§5 示例）。"""

    action: str                                   # concede / hold_floor / human
    stage: str
    allowed_min_price: float | None               # 本阶段可报最低价
    gift_candidates: tuple[str, ...]
    talk_track_tag: str

    def to_ai_payload(self) -> dict[str, object]:
        """AI 出参（白名单字段结构性防泄漏，§8 红线）。"""
        return {
            "action": self.action,
            "stage": self.stage,
            "allowed_min_price": self.allowed_min_price,
            "gift_candidates": list(self.gift_candidates),
            "talk_track_tag": self.talk_track_tag,
        }


def match_gift(rules: tuple[GiftRule, ...], qty: int) -> tuple[str, ...]:
    """赠品组合规则：按起订量命中最高满足档位（无满足档返回空）。"""
    hit = max((r for r in rules if qty >= r.min_qty), key=lambda r: r.min_qty, default=None)
    return hit.gift_skus if hit is not None else ()


class NegotiationEngine:
    """议价引擎：底线校验 → 让步阶梯三段 → 赠品建议（SR-SC-003 §6.3 工程级形态）。

    :param store: 底线价格库（注入）；
    :param human_gap_pct: 压价低于底线该比例即转人工（§7 strategy.bargain.human_gap_pct）；
    :param audit: 审计事件承接回调（生产 = audit_log 哈希链）；
    :param clock: 时钟注入。
    """

    def __init__(
        self,
        store: PriceFloorStore,
        *,
        human_gap_pct: float = 0.10,
        audit: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self._human_gap_pct = human_gap_pct
        self._audit = audit or (lambda event: None)
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str], BargainSession] = {}
        self.audit: list[dict[str, object]] = []     # 进程内审计观测面

    # ---------------------------------------------------------------- 主链路

    def on_counter_offer(self, conversation_id: str, sku: str, qty: int,
                         customer_target: float, current_offer: float) -> QuoteHint:
        """客户压价入口：无底线行转人工；有会话复用（快照隔离），否则创建。"""
        pf = self.store.floor_of(sku)
        if pf is None:
            return QuoteHint("human", "l0", None, (), "议价_转人工")   # 宁可不谈，不越权报价
        with self._lock:
            session = self._sessions.get((conversation_id, sku))
            if session is None:
                session = BargainSession(
                    conversation_id=conversation_id, sku=sku, qty=qty,
                    list_price=pf.list_price, floor_price=pf.floor_price,
                    first_step_pct=pf.first_step_pct, second_step_pct=pf.second_step_pct,
                    current_offer=current_offer,
                )
                self._sessions[(conversation_id, sku)] = session
                self._record("bargain_created", session)
        return self._decide(session, customer_target)

    def on_deal(self, conversation_id: str, sku: str) -> None:
        """达成一致：关闭会话记 deal（平台改价/下单仍走「起草→人工确认」红线）。"""
        with self._lock:
            session = self._sessions.get((conversation_id, sku))
            if session is None:
                return
            session.stage = "deal"
            session.status = "closed"
            self._record("bargain_deal", session)

    def session_of(self, conversation_id: str, sku: str) -> BargainSession | None:
        """在途/已关会话查询（观测面）。"""
        with self._lock:
            return self._sessions.get((conversation_id, sku))

    # ---------------------------------------------------------------- 内部决策

    def _decide(self, session: BargainSession, customer_target: float) -> QuoteHint:
        """单轮决策：底线校验（hold/human）→ 阶梯推进（concede）→ floor 终点保持。"""
        gifts = self._gifts_of(session, session.qty)
        if customer_target < session.floor_price:
            gap_floor = session.floor_price * (1 - self._human_gap_pct)
            if customer_target < gap_floor:
                session.stage = "human"                # 离谱压价直接转人工（FR-AG-009 链路）
                self._record("bargain_human", session)
                return QuoteHint("human", "human", None, gifts, "议价_转人工")
            return QuoteHint("hold_floor", session.stage, round(session.current_offer, 4),
                             gifts, "议价_坚守底线")
        if session.stage in {"floor", "human", "deal"}:
            return QuoteHint("hold_floor", session.stage, round(session.floor_price, 4),
                             gifts, "议价_坚守底线")
        next_stage = _NEXT_STAGE[session.stage]
        if next_stage == "floor":
            target_price = session.floor_price
        else:
            cumulative = (session.first_step_pct if next_stage == "l1"
                          else session.first_step_pct + session.second_step_pct)
            target_price = session.list_price * (1 - cumulative)
        # 硬约束：永不破底线、不过度让利（不低于客户目标即可成交）
        target_price = max(target_price, session.floor_price, customer_target)
        session.concessions.append({
            "at": self._clock().isoformat(), "from": session.current_offer,
            "to": round(target_price, 4), "stage": next_stage,
        })
        session.current_offer = target_price
        session.stage = next_stage
        if next_stage == "floor" and not gifts:
            session.stage = "human"                    # floor 阶段无赠品可选 → 转人工
            self._record("bargain_human", session)
            return QuoteHint("human", "human", round(session.floor_price, 4), (), "议价_转人工")
        self._record("concession", session)
        return QuoteHint("concede", next_stage, round(target_price, 4), gifts,
                         f"议价_{next_stage}")

    def _gifts_of(self, session: BargainSession, qty: int) -> tuple[str, ...]:
        """按会话快照外的现行价格库匹配赠品（demo：读现行 gift_rules）。"""
        pf = self.store.floor_of(session.sku)
        return match_gift(pf.gift_rules, qty) if pf is not None else ()

    def _record(self, event: str, session: BargainSession) -> None:
        """审计事件（进程内观测面 + 回调承接）。"""
        record: dict[str, object] = {
            "event": event, "conversation_id": session.conversation_id,
            "sku": session.sku, "stage": session.stage, "at": self._clock().isoformat(),
        }
        self.audit.append(record)
        self._audit(record)


__all__ = [
    "STAGES", "AI_PAYLOAD_FIELDS", "NegotiationError", "GiftRule", "PriceFloor",
    "PriceFloorStore", "InMemoryFloorStore", "BargainSession", "QuoteHint",
    "match_gift", "NegotiationEngine",
]
