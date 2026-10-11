# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""风控 V1 单元测试（FR-AG-005，SR-AG-005 §6/§9；迭代 2.3 TDD 阶段①用例）。

覆盖（验收口径 → 用例）：
- 滑动窗口频率门：小时内/日内上限边界（恰等 PASS / 超 1 排队）与排队语义
  （QUEUE_HOUR=下一窗口、QUEUE_NEXT_DAY=次日工作窗口起点+噪声）；
- 间隔随机化（NFR-SEC-11）：next_allowed_at 采样落在夹取域、确定性种子可复现、
  夜间样本顺延次日工作窗口；
- 敏感操作白名单拦截：改价/订单/支付/收货一律 INTERCEPT→草稿→pending_confirm
  （状态机迁移回调注入）、确认后放行、未知动作默认拦截（fail-safe 白名单制）；
- 红线规则引擎：规则集注册/热替换、内容命中（批量群发/诱导站外/联系方式）→
  BLOCK+高级告警+审计；
- fail-safe：风控组件内部抛错 → 一律 BLOCK（宁可停发），无任何 Permit 签发；
- 无旁路架构断言：SendPermit 仅风控可构造、一次性消费、发送器仅接受 Permit；
- 判定全量审计（NFR-SEC-03）与 op_uid 幂等。

实现方案：docs/07-实现方案/AG/SR-AG-005-风控V1.md §6.1-§6.6、§9
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from services.agent.core.engine import InMemoryAlerts
from services.agent.core.risk.guard import (
    PermitAlreadyUsedError,
    PermitOnlySender,
    RiskAction,
    RiskConfig,
    RiskGuard,
    RiskRule,
    RedlineEngine,
    SendPermit,
    SensitiveDraftStore,
    Verdict,
    classify_action,
)

NOW = datetime(2026, 10, 8, 10, 0, 0)  # 周四 10:00（工作窗口内）

DEFAULT_RULES = (
    RiskRule("RED_BULK_SPAM", "批量群发", "keyword", "批量群发|群发全部好友"),
    RiskRule("RED_OFFSITE_INDUCE", "诱导站外交易", "keyword", "绕开平台|加我微信|whatsapp me"),
    RiskRule("RED_CONTACT_BOMB", "联系方式轰炸", "regex", r"1[3-9]\d{9}"),
)


def make_guard(**overrides):
    """标准装配：默认红线规则 + 内存告警 + 审计记录 + 迁移回调记录。"""
    config = overrides.pop("config", None) or RiskConfig()
    rules = overrides.pop("rules", DEFAULT_RULES)
    alerts = overrides.pop("alerts", None) or InMemoryAlerts()
    audit: list[dict] = []
    transitions: list[tuple[str, str]] = []
    state_map: dict[str, str] = overrides.pop("state_map", {})
    guard = RiskGuard(
        config=config,
        rules=rules,
        alerts=alerts,
        audit=audit.append,
        state_lookup=lambda uid: state_map.get(uid, "ai_managed"),
        on_intercept=lambda uid, draft: transitions.append((uid, draft.status)),
        rng=random.Random(7),
        **overrides,
    )
    return guard, alerts, audit, transitions


def act(op: str = "op-1", action_type: str = "send_reply", content: str = "Hello!",
        conv: str = "c-1", account: int = 1) -> RiskAction:
    """构造判定入参。"""
    return RiskAction(op_uid=op, account_id=account, conversation_uid=conv,
                      action_type=action_type, content=content)


# ───────────────────────── 滑动窗口频率门（需求①） ─────────────────────────


class TestSlidingWindow:
    """小时内/日内上限边界与排队语义（SR-AG-005 §6.1/§9）。"""

    def test_hour_cap_boundary_equal_passes(self) -> None:
        """恰等边界：max_hour=3 时第 3 笔仍 PASS（上限内含等号；间隔置零隔离频率变量）。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=3, min_gap_s=0.0,
                                                      max_gap_s=0.0))
        for i in range(1, 4):
            decision = guard.check(act(op=f"op-{i}"), now=NOW + timedelta(minutes=i))
        assert decision.verdict is Verdict.PASS

    def test_hour_cap_exceed_one_queues(self) -> None:
        """超 1 边界：第 4 笔（max_hour=3）→ QUEUE_HOUR，不签发 Permit。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=3, min_gap_s=0.0,
                                                      max_gap_s=0.0))
        for i in range(1, 4):
            guard.check(act(op=f"op-{i}"), now=NOW + timedelta(minutes=i))
        decision = guard.check(act(op="op-4"), now=NOW + timedelta(minutes=4))
        assert decision.verdict is Verdict.QUEUE_HOUR
        assert decision.permit is None

    def test_hour_queue_semantics_next_allowed(self) -> None:
        """小时超限排队语义：next_allowed_at = now + hold_minutes。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=1, hold_minutes=30,
                                                      min_gap_s=0.0, max_gap_s=0.0))
        guard.check(act(op="a"), now=NOW)
        decision = guard.check(act(op="b"), now=NOW + timedelta(seconds=1))
        assert decision.verdict is Verdict.QUEUE_HOUR
        assert decision.next_allowed_at == NOW.replace(minute=30) + timedelta(seconds=1)

    def test_day_cap_boundary_equal_passes(self) -> None:
        """恰等边界：max_day=2 时第 2 笔 PASS。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=50, max_ops_per_day=2,
                                                      min_gap_s=0.0, max_gap_s=0.0))
        assert guard.check(act(op="d1"), now=NOW).verdict is Verdict.PASS
        assert guard.check(act(op="d2"), now=NOW + timedelta(minutes=5)).verdict is Verdict.PASS

    def test_day_cap_exceed_queues_next_day(self) -> None:
        """日超限排队语义：QUEUE_NEXT_DAY，next_allowed_at = 次日工作窗口起点+噪声。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=50, max_ops_per_day=1,
                                                      min_gap_s=0.0, max_gap_s=0.0))
        guard.check(act(op="d1"), now=NOW)
        decision = guard.check(act(op="d2"), now=NOW + timedelta(minutes=5))
        assert decision.verdict is Verdict.QUEUE_NEXT_DAY
        assert decision.next_allowed_at is not None
        nxt = decision.next_allowed_at
        assert nxt.date() == (NOW + timedelta(days=1)).date()      # 次日
        assert nxt.hour == 8 and nxt.minute >= 30                  # 窗口起点 08:30 + 噪声 ≤15min
        assert (nxt - nxt.replace(minute=30, second=0, microsecond=0)) <= timedelta(seconds=900)

    def test_intercept_does_not_consume_quota(self) -> None:
        """INTERCEPT/BLOCK 不消耗额度：草稿并未发送（SR-AG-005 §6.1 admit 注释）。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=1))
        guard.check(act(op="s1", action_type="price_change"), now=NOW)      # INTERCEPT
        assert guard.check(act(op="s2"), now=NOW + timedelta(seconds=30)).verdict is Verdict.PASS

    def test_block_does_not_consume_quota(self) -> None:
        """红线 BLOCK 不消耗额度。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=1))
        guard.check(act(op="r1", content="批量群发促销"), now=NOW)           # BLOCK
        assert guard.check(act(op="r2"), now=NOW + timedelta(seconds=30)).verdict is Verdict.PASS

    def test_interval_wait_queues_until_next_allowed(self) -> None:
        """间隔未到：签发过的 next_allowed_at 未至 → QUEUE_HOUR(interval_wait)。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=60, max_gap_s=60))
        first = guard.check(act(op="i1"), now=NOW)
        assert first.verdict is Verdict.PASS
        second = guard.check(act(op="i2"), now=NOW + timedelta(seconds=10))
        assert second.verdict is Verdict.QUEUE_HOUR
        assert second.reason == "interval_wait"
        assert second.next_allowed_at == first.next_allowed_at

    def test_pass_after_interval_elapsed(self) -> None:
        """间隔已到 + 窗口未超 → 恢复 PASS。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=60, max_gap_s=60))
        guard.check(act(op="i1"), now=NOW)
        decision = guard.check(act(op="i2"), now=NOW + timedelta(seconds=60))
        assert decision.verdict is Verdict.PASS


# ───────────────────────── 间隔随机化（NFR-SEC-11） ─────────────────────────


class TestIntervalRandomization:
    """next_allowed_at 对数正态采样：夹取域/确定性/夜间顺延（SR-AG-005 §6.1）。"""

    def test_samples_within_clamp_bounds(self) -> None:
        """连续采样全部落在 [min_gap, max_gap] 夹取域。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=20.0, max_gap_s=600.0))
        samples = [guard.next_allowed_at(1, base=NOW) for _ in range(2000)]
        gaps = [(s - NOW).total_seconds() for s in samples]
        assert all(20.0 <= g <= 600.0 for g in gaps)

    def test_deterministic_seed_reproducible(self) -> None:
        """同一种子 → 采样序列完全一致（测试/演练可复现）。"""
        guard_a, _, _, _ = make_guard()   # rng 种子 7
        guard_b, _, _, _ = make_guard()
        seq_a = [guard_a.next_allowed_at(1, base=NOW) for _ in range(50)]
        seq_b = [guard_b.next_allowed_at(1, base=NOW) for _ in range(50)]
        assert seq_a == seq_b

    def test_distribution_not_stuck_at_floor(self) -> None:
        """分布形状：中位间隔显著高于下限（非贴地板的固定间隔）。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=20.0, max_gap_s=600.0))
        gaps = sorted((guard.next_allowed_at(1, base=NOW) - NOW).total_seconds()
                      for _ in range(2000))
        assert gaps[len(gaps) // 2] > 30.0

    def test_night_sample_defers_to_next_window(self) -> None:
        """夜间采样顺延次日工作窗口起点+入场噪声（活跃时段模型）。"""
        guard, _, _, _ = make_guard()
        night = datetime(2026, 10, 8, 23, 0, 0)
        for _ in range(20):
            nxt = guard.next_allowed_at(1, base=night)
            assert nxt.date() == datetime(2026, 10, 9).date()
            assert nxt.hour == 8 and nxt.minute >= 30


# ───────────────────────── 敏感操作拦截（需求②/§7.4 红线） ─────────────────────────


class TestSensitiveIntercept:
    """敏感白名单拦截：INTERCEPT→草稿→pending_confirm 联动→确认放行。"""

    @pytest.mark.parametrize("action_type", ["price_change", "order_op", "payment", "shipping_addr"])
    def test_all_sensitive_types_intercepted(self, action_type: str) -> None:
        """§7.4 四类敏感操作一律 INTERCEPT，零 Permit 签发。"""
        guard, _, _, _ = make_guard()
        decision = guard.check(act(op=f"s-{action_type}", action_type=action_type), now=NOW)
        assert decision.verdict is Verdict.INTERCEPT
        assert decision.permit is None
        assert decision.sensitive_uid is not None

    def test_unknown_action_defaults_to_intercept(self) -> None:
        """白名单制 fail-safe：未登记的新动作默认拦截，不可绕过风控。"""
        guard, _, _, _ = make_guard()
        decision = guard.check(act(op="u1", action_type="export_order"), now=NOW)
        assert decision.verdict is Verdict.INTERCEPT

    @pytest.mark.parametrize("whitelisted", ["poll_inquiry", "read_message", "mark_read", "send_reply"])
    def test_whitelist_auto_executable(self, whitelisted: str) -> None:
        """可自动执行白名单动作正常 PASS 并签发 Permit。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0))
        decision = guard.check(act(op=f"w-{whitelisted}", action_type=whitelisted), now=NOW)
        assert decision.verdict is Verdict.PASS
        assert decision.permit is not None

    def test_intercept_creates_pending_draft(self) -> None:
        """INTERCEPT 生成敏感草稿（pending_confirm，含确认时限）。"""
        guard, _, _, _ = make_guard()
        decision = guard.check(act(op="d1", action_type="price_change"), now=NOW)
        draft = guard.drafts.get(decision.sensitive_uid)
        assert draft is not None and draft.status == "pending_confirm"
        assert draft.expires_at > NOW

    def test_intercept_transitions_conversation_via_callback(self) -> None:
        """状态联动：迁移回调被注入调用（会话迁至 pending_confirm，FR-AG-008）。"""
        guard, _, _, transitions = make_guard()
        guard.check(act(op="t1", action_type="order_op"), now=NOW)
        assert transitions == [("c-1", "pending_confirm")]

    def test_confirm_then_release_passes(self) -> None:
        """人工确认后放行：execute_confirmed 复跑红线+频率门 → PASS+Permit。"""
        guard, _, _, _ = make_guard()
        decision = guard.check(act(op="cf1", action_type="price_change"), now=NOW)
        guard.confirm_sensitive(decision.sensitive_uid, operator="supervisor", now=NOW)
        released = guard.execute_confirmed(
            decision.sensitive_uid,
            act(op="cf1-exec", action_type="price_change"),
            now=NOW + timedelta(minutes=5),
        )
        assert released.verdict is Verdict.PASS
        assert released.permit is not None

    def test_reject_blocks_release(self) -> None:
        """驳回后不可放行：草稿 rejected，execute_confirmed 拒绝。"""
        guard, _, _, _ = make_guard()
        decision = guard.check(act(op="rj1", action_type="payment"), now=NOW)
        guard.reject_sensitive(decision.sensitive_uid, operator="supervisor",
                               reason="价格不合理", now=NOW)
        with pytest.raises(ValueError, match="rejected"):
            guard.execute_confirmed(decision.sensitive_uid, act(op="rj1-x"), now=NOW)


# ───────────────────────── 红线规则引擎（§7.4） ─────────────────────────


class TestRedlineEngine:
    """红线内容匹配：注册/命中/BLOCK+告警+审计/热替换。"""

    @pytest.mark.parametrize("content", [
        "双 11 批量群发促销信息",
        "加我微信，绕开平台下单更便宜",
        "contact me at 13812345678",
    ])
    def test_redline_hits_block(self, content: str) -> None:
        """批量群发/诱导站外/联系方式轰炸 → BLOCK + rule_code。"""
        guard, _, _, _ = make_guard()
        decision = guard.check(act(op="red-1", content=content), now=NOW)
        assert decision.verdict is Verdict.BLOCK
        assert decision.rule_code is not None
        assert decision.permit is None

    def test_redline_alerts_high_and_audited(self) -> None:
        """命中即高级告警（risk_hit）+ 判定留痕（NFR-SEC-03）。"""
        guard, alerts, audit, _ = make_guard()
        guard.check(act(op="red-2", content="加我微信私下打款"), now=NOW)
        assert alerts.last(level="high", type_="risk_hit") is not None
        assert any(entry["verdict"] == "BLOCK" and entry["op_uid"] == "red-2" for entry in audit)

    def test_clean_content_passes(self) -> None:
        """未命中红线内容正常放行。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0))
        assert guard.check(act(op="ok-1", content="Thanks for your inquiry."), now=NOW).verdict \
            is Verdict.PASS

    def test_redline_hot_reload(self) -> None:
        """规则热替换：原子替换后旧规则失效、新规则生效。"""
        engine = RedlineEngine(DEFAULT_RULES)
        assert engine.match("批量群发") is not None
        engine.reload((RiskRule("RED_NEW", "新规", "keyword", "违禁词"),))
        assert engine.match("批量群发") is None
        assert engine.match("违禁词") is not None
        assert engine.match("违禁词").rule_code == "RED_NEW"


# ───────────────────────── fail-safe（宁停不险） ─────────────────────────


class TestFailSafe:
    """风控组件内部故障 → 一律 BLOCK（宁可停发），绝不放行。"""

    def test_internal_error_blocks(self) -> None:
        """组件抛错（迁移回调崩溃）→ FAILSAFE，无 Permit。"""
        guard, _, _, _ = make_guard()
        guard._on_intercept = lambda uid, draft: (_ for _ in ()).throw(RuntimeError("boom"))
        decision = guard.check(act(op="fs-1", action_type="price_change"), now=NOW)
        assert decision.verdict is Verdict.FAILSAFE
        assert decision.permit is None
        assert "fail" in decision.reason.lower() or "fail" in decision.reason

    def test_failsafe_alerts(self) -> None:
        """fail-safe 触发高级告警（system_error），可运维感知。"""
        guard, alerts, _, _ = make_guard()
        guard._state_lookup = lambda uid: (_ for _ in ()).throw(RuntimeError("store down"))
        guard.check(act(op="fs-2"), now=NOW)
        assert alerts.last(level="high", type_="system_error") is not None

    def test_failsafe_even_for_whitelisted_action(self) -> None:
        """白名单动作遇组件故障同样停发（无任何旁路）。"""
        guard, _, _, _ = make_guard()
        guard._redline = None  # type: ignore[assignment] —— 拆掉内部组件制造故障
        decision = guard.check(act(op="fs-3"), now=NOW)
        assert decision.verdict is Verdict.FAILSAFE


# ───────────────────────── 无旁路：SendPermit 唯一发送入口 ─────────────────────────


class TestNoBypass:
    """发送器仅接受风控签发的 SendPermit（强制路径无旁路的结构保证）。"""

    def test_permit_not_constructable_externally(self) -> None:
        """外部无法伪造 SendPermit（私有 bearer 签名）。"""
        with pytest.raises(TypeError):
            SendPermit(  # type: ignore[call-arg]
                permit_uid="fake", account_id=1, conversation_uid="c", action_type="send_reply",
                op_uid="x", issued_at=NOW, next_allowed_at=NOW,
            )

    def test_sender_rejects_non_permit(self) -> None:
        """发送器对非 Permit 入参 TypeError（对齐 driver.send_reply 物理阻断模式）。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0))
        sender = PermitOnlySender(guard)
        with pytest.raises(TypeError):
            sender.send("permit-looking-string", "text")  # type: ignore[arg-type]

    def test_permit_one_time_consumption(self) -> None:
        """Permit 一次性：首次消费成功，重复消费抛 PermitAlreadyUsedError。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0))
        decision = guard.check(act(op="ot-1"), now=NOW)
        sender = PermitOnlySender(guard)
        assert sender.send(decision.permit, "hi") == "hi"
        with pytest.raises(PermitAlreadyUsedError):
            sender.send(decision.permit, "hi again")

    def test_forged_uid_rejected(self) -> None:
        """伪造 permit_uid 的异类对象被拒（isinstance + 归属校验双保险）。"""
        guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0))
        other_guard, _, _, _ = make_guard(config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0))
        decision = other_guard.check(act(op="fg-1"), now=NOW)
        sender = PermitOnlySender(guard)
        with pytest.raises(ValueError, match="签发"):
            sender.send(decision.permit, "hi")


# ───────────────────────── 接管静默 / 审计 / 幂等 ─────────────────────────


class TestTakeoverAuditIdempotency:
    """接管静默（需求③）+ 全量审计 + op_uid 幂等。"""

    @pytest.mark.parametrize("state", ["human_managed", "pending_confirm", "closed"])
    def test_non_ai_state_denies(self, state: str) -> None:
        """非 ai_managed 态一律 DENY（AI 静默，不告警属正常业务）。"""
        guard, _, _, _ = make_guard(state_map={"c-1": state})
        decision = guard.check(act(op="tk-1"), now=NOW)
        assert decision.verdict is Verdict.DENY
        assert decision.permit is None

    def test_every_decision_audited_including_pass(self) -> None:
        """全判定留痕（含 PASS 双写审计，风控触发事件可审计）。"""
        guard, _, audit, _ = make_guard(config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0))
        guard.check(act(op="au-1"), now=NOW)
        guard.check(act(op="au-2", content="批量群发"), now=NOW + timedelta(seconds=1))
        verdicts = {entry["verdict"] for entry in audit}
        assert {"PASS", "BLOCK"} <= verdicts
        assert all("op_uid" in entry and "at" in entry for entry in audit)

    def test_op_uid_idempotent(self) -> None:
        """同 op_uid 重复判定返回首次结果，窗口仅计 1 笔。"""
        guard, _, _, _ = make_guard(config=RiskConfig(max_ops_per_hour=1, min_gap_s=0.0,
                                                      max_gap_s=0.0))
        first = guard.check(act(op="same"), now=NOW)
        second = guard.check(act(op="same"), now=NOW + timedelta(minutes=1))
        assert first.verdict is Verdict.PASS
        assert second.verdict is Verdict.PASS          # 幂等回放，不再计数
        third = guard.check(act(op="other"), now=NOW + timedelta(minutes=2))
        assert third.verdict is Verdict.QUEUE_HOUR     # 窗口确只有 1 笔在册

    def test_classify_action_semantics(self) -> None:
        """白名单制分类语义：白名单→auto；敏感→intercept；未知→intercept。"""
        assert classify_action("send_reply") == "auto"
        assert classify_action("price_change") == "intercept"
        assert classify_action("never_seen_before") == "intercept"

    def test_draft_store_standalone(self) -> None:
        """SensitiveDraftStore 独立可用：创建/查询/确认时限。"""
        store = SensitiveDraftStore()
        draft = store.create(action_type="price_change", conversation_uid="c-9",
                             payload={"to": 9.9}, now=NOW, expire_hours=24)
        assert store.get(draft.uid).status == "pending_confirm"
        store.confirm(draft.uid, operator="op", now=NOW)
        assert store.get(draft.uid).status == "confirmed"
        assert store.get(draft.uid).decided_by == "op"
