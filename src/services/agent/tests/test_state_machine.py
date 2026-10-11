# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""会话状态机单元测试（FR-AG-008，SR-AG-008 §9 T1-T8）。

覆盖（对应实现方案 §6.1/§6.2/§6.4/§6.5）：
- 合法迁移全矩阵（§6.1 LEGAL_TRANSITIONS 逐条成功、版本 +1、state_hist 追加）；
- 非法迁移拒绝（closed→pending_confirm、human_managed→pending_confirm，无写库）；
- 幂等迁移（目标态已达：不写痕、无审计）；
- 乐观并发（expect_version 过期冲突；锁内二次校验使双写竞态恰一胜）；
- 迁移留痕（state_hist 条目 from/to/at/by/reason 齐全；条数 == 成功迁移数）；
- 审计钩子（每次成功迁移恰一条，含 uid/from/to/by/reason/version）；
- 发送闸门（物理阻断：仅 ai_managed 签发 SendToken；版本失配/TTL 过期 verify 失败）；
- pending_confirm 超时（N 小时提醒恰一次；M 小时 system:timeout 转人工，绝不回 AI）；
- 崩溃恢复前置（rebuild：新状态机实例从存储重建 state/version/hist，进程无状态化）。

实现方案：docs/07-实现方案/AG/SR-AG-008-会话状态机.md §6、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import pytest

from services.agent.core.state import (
    LEGAL_TRANSITIONS,
    AISendBlockedError,
    AISendGate,
    ConversationNotFoundError,
    ConversationRecord,
    ConvState,
    IllegalTransitionError,
    InMemorySessionStore,
    SendToken,
    VersionConflictError,
    ConversationStateMachine,
    scan_pending_timeout,
)

AI = ConvState.AI_MANAGED
HUMAN = ConvState.HUMAN_MANAGED
PENDING = ConvState.PENDING_CONFIRM
CLOSED = ConvState.CLOSED

#: 设计文档 §6.1 迁移表（9 条）+ §6.4 挂起超时 fail-safe（pending→human，偏差显式标注）
EXPECTED_LEGAL: set[tuple[ConvState, ConvState]] = {
    (AI, HUMAN), (HUMAN, AI), (AI, PENDING), (PENDING, AI),
    (AI, CLOSED), (HUMAN, CLOSED), (PENDING, CLOSED),
    (CLOSED, AI), (CLOSED, HUMAN),
    (PENDING, HUMAN),  # SR-AG-008 §6.4：挂起 24h 超时自动转人工（红线：绝不回 AI）
}


def make_sm() -> tuple[ConversationStateMachine, InMemorySessionStore, list[dict]]:
    """标准装配：内存存储 + 状态机 + 审计记录列表。"""
    store = InMemorySessionStore()
    audit: list[dict] = []
    sm = ConversationStateMachine(store, audit=audit.append)
    return sm, store, audit


def seed(sm: ConversationStateMachine, uid: str, state: ConvState) -> None:
    """构造指定状态的会话（沿合法路径推进，确保留痕真实）。"""
    sm.ensure_conversation(uid)
    path: dict[ConvState, list[ConvState]] = {
        HUMAN: [HUMAN],
        PENDING: [PENDING],
        CLOSED: [CLOSED],
    }
    for target in path.get(state, []):
        sm.transition(uid, target, actor="test:seed", reason="测试预置")


# ─────────────────────────── 迁移表与合法迁移 ───────────────────────────


def test_legal_transition_table_matches_design() -> None:
    """合法迁移表与 SR-AG-008 §6.1/§6.4 一致（10 条：9 条设计表 + 1 条超时 fail-safe）。"""
    assert set(LEGAL_TRANSITIONS) == EXPECTED_LEGAL
    assert len(LEGAL_TRANSITIONS) == len(EXPECTED_LEGAL)


def test_all_legal_transitions_succeed() -> None:
    """T1 合法迁移全遍历：逐条成功，state_hist 追加对应条目，version +1。"""
    sm, store, _ = make_sm()
    for from_state, to_state in sorted(EXPECTED_LEGAL):
        uid = f"conv-legal-{from_state.value}-{to_state.value}"
        sm.ensure_conversation(uid)
        seed(sm, uid, from_state)
        before = store.get(uid)
        assert before is not None
        v0 = before.state_version
        # pending→human 为 §6.4 挂起超时专用路径：仅 system:timeout actor 可触发
        actor = "system:timeout" if (from_state, to_state) == (PENDING, HUMAN) else "test:case"
        result = sm.transition(uid, to_state, actor=actor, reason="合法迁移遍历")
        assert result.to_state is to_state
        assert result.version == v0 + 1
        after = store.get(uid)
        assert after is not None
        assert after.state is to_state
        assert after.state_hist[-1]["to"] == to_state.value
        assert after.state_hist[-1]["from"] == from_state.value


def test_illegal_transitions_rejected() -> None:
    """T2 非法迁移拒绝：抛 IllegalTransitionError（中文），状态与留痕零变化。

    pending→human_managed 为 §6.4 挂起超时专用路径（actor=system:timeout），
    人工侧 actor 触发同判非法（防绕过确认链路）。
    """
    for from_state, to_state, actor in (
        (HUMAN, PENDING, "user:a"),
        (CLOSED, PENDING, "user:a"),
        (PENDING, HUMAN, "user:boss"),
    ):
        sm, store, _ = make_sm()
        uid = "conv-illegal"
        seed(sm, uid, from_state)
        before = store.get(uid)
        assert before is not None
        with pytest.raises(IllegalTransitionError, match="非法"):
            sm.transition(uid, to_state, actor=actor, reason="非法尝试")
        after = store.get(uid)
        assert after is not None
        assert after.state is from_state
        assert after.state_version == before.state_version
        assert len(after.state_hist) == len(before.state_hist)


def test_idempotent_transition_no_hist_write() -> None:
    """T3 幂等迁移：目标态已达 → 幂等成功返回，state_hist 不新增、审计不触发。"""
    sm, store, audit = make_sm()
    uid = "conv-idem"
    seed(sm, uid, HUMAN)
    audit.clear()  # 种子路径的审计不计入本用例断言
    before = store.get(uid)
    assert before is not None
    result = sm.transition(uid, HUMAN, actor="test:case", reason="重复接管")
    assert result.idempotent is True
    assert result.version == before.state_version
    after = store.get(uid)
    assert after is not None
    assert len(after.state_hist) == len(before.state_hist)
    assert not audit  # 幂等无迁移不写痕，亦不投递审计


def test_transition_unknown_uid_chinese_error() -> None:
    """未知会话迁移：抛 ConversationNotFoundError（中文信息，对应 40401）。"""
    sm, _, _ = make_sm()
    with pytest.raises(ConversationNotFoundError, match="不存在"):
        sm.transition("ghost", AI, actor="test:case", reason="幽灵会话")


# ─────────────────────────── 乐观并发 ───────────────────────────


def test_expect_version_conflict() -> None:
    """乐观并发（显式 CAS）：expect_version 已过期 → VersionConflictError（40901）。"""
    sm, _, _ = make_sm()
    uid = "conv-cas"
    sm.ensure_conversation(uid)
    sm.transition(uid, HUMAN, actor="user:a", reason="先到者")
    with pytest.raises(VersionConflictError, match="冲突"):
        sm.transition(uid, AI, actor="ai:brain", reason="后到者", expect_version=0)


def test_concurrent_double_write_exactly_one_wins() -> None:
    """T4 双写竞态恰一胜：写者 A 在读态之后、落库之前被写者 B 抢先推进版本
    （经存储 on_locked 钩子注入锁内交错）→ A 的 CAS 必然失败，恰一成功。"""
    sm, store, _ = make_sm()
    uid = "conv-race"
    sm.ensure_conversation(uid)
    v0 = store.get(uid).state_version  # type: ignore[union-attr]

    def interleave_with_winner() -> None:
        """在 A 持锁校验前模拟 B 完成迁移（同线程重入，版本先推进到 v0+1）。"""
        sm.transition(uid, HUMAN, actor="user:b", reason="并发写者 B（人工接管）")

    # A（AI 侧挂起请求）与 B（人工接管）同刻争行锁：人工优先，A 必败
    with pytest.raises(VersionConflictError, match="并发"):
        sm.transition(
            uid, PENDING, actor="ai:brain", reason="并发写者 A", expect_version=v0,
            _test_on_locked=interleave_with_winner,
        )
    after = store.get(uid)
    assert after is not None
    assert after.state is HUMAN            # 恰 B 胜出（人工优先）
    assert after.state_version == v0 + 1
    assert len(after.state_hist) == 1      # 恰一次迁移留痕


def test_race_loser_raises_conflict_without_hook_skip() -> None:
    """竞态败者路径：CAS 失败必须抛 VersionConflictError 而非静默跳过。"""
    sm, store, _ = make_sm()
    uid = "conv-race2"
    sm.ensure_conversation(uid)

    def bump_version_silently() -> None:
        """模拟并发方推进版本（存储内真实生效，而非改读出的克隆）。"""
        cur = store.get(uid)
        assert cur is not None
        cur.state_version += 1
        store.put(cur)

    with pytest.raises(VersionConflictError, match="并发"):
        sm.transition(uid, HUMAN, actor="user:a", reason="竞态",
                      _test_on_locked=bump_version_silently)


# ─────────────────────────── 留痕与审计 ───────────────────────────


def test_hist_entry_fields_complete() -> None:
    """留痕条目字段齐全：from/to/at/by/reason；at 为 ISO8601 UTC 时间戳。"""
    sm, store, _ = make_sm()
    uid = "conv-hist"
    sm.ensure_conversation(uid)
    sm.transition(uid, HUMAN, actor="user:zhangsan", reason="客户议价超出授权")
    rec = store.get(uid)
    assert rec is not None
    entry = rec.state_hist[-1]
    assert set(entry) == {"from", "to", "at", "by", "reason"}
    assert entry["from"] == AI.value
    assert entry["to"] == HUMAN.value
    assert entry["by"] == "user:zhangsan"
    assert entry["reason"] == "客户议价超出授权"
    assert entry["at"].endswith("+00:00")  # ISO8601 且 UTC


def test_hist_count_equals_success_count() -> None:
    """T8 留痕对账 + 版本单调：任意成功迁移序列后 state_hist 条数 == 成功迁移数，
    版本号随每次迁移严格 +1（乐观并发的单调性前提）。"""
    sm, store, _ = make_sm()
    uid = "conv-loop"
    sm.ensure_conversation(uid)
    seq = [(AI, HUMAN), (HUMAN, AI), (AI, PENDING), (PENDING, AI),
           (AI, HUMAN), (HUMAN, CLOSED), (CLOSED, AI), (AI, CLOSED)]
    versions = [store.get(uid).state_version]  # type: ignore[union-attr]
    ok = 0
    for _from, to in seq:
        result = sm.transition(uid, to, actor="test:loop", reason="留痕对账")
        versions.append(result.version)
        ok += 1
    rec = store.get(uid)
    assert rec is not None
    assert len(rec.state_hist) == ok
    assert versions == sorted(set(versions))          # 严格单调递增
    assert rec.state_version == ok


def test_audit_hook_payload_complete() -> None:
    """审计钩子：每次成功迁移恰一条，payload 含 conv_uid/from/to/by/reason/version。"""
    sm, _, audit = make_sm()
    uid = "conv-audit"
    sm.ensure_conversation(uid)
    sm.transition(uid, HUMAN, actor="user:zhangsan", reason="人工接管")
    assert len(audit) == 1
    payload = audit[0]
    assert payload["conv_uid"] == uid
    assert payload["from"] == AI.value
    assert payload["to"] == HUMAN.value
    assert payload["by"] == "user:zhangsan"
    assert payload["reason"] == "人工接管"
    assert payload["version"] == 1


def test_audit_skipped_on_idempotent_and_illegal() -> None:
    """审计只在真实迁移时投递：幂等与非法迁移均不产生审计事件。"""
    sm, _, audit = make_sm()
    uid = "conv-audit2"
    seed(sm, uid, HUMAN)
    audit.clear()
    sm.transition(uid, HUMAN, actor="user:a", reason="幂等重复")  # 幂等
    with pytest.raises(IllegalTransitionError):
        sm.transition(uid, PENDING, actor="user:a", reason="非法")  # 非法
    assert not audit


# ─────────────────────────── pending_confirm 超时 ───────────────────────────


def test_pending_since_set_on_enter_and_clear_on_exit() -> None:
    """pending_since：进入 pending_confirm 记录时刻，离开该态即清空。"""
    sm, store, _ = make_sm()
    uid = "conv-since"
    sm.ensure_conversation(uid)
    sm.transition(uid, PENDING, actor="ai:brain", reason="敏感操作挂起")
    rec = store.get(uid)
    assert rec is not None
    assert rec.pending_since is not None
    sm.transition(uid, AI, actor="user:boss", reason="确认执行")
    rec = store.get(uid)
    assert rec is not None
    assert rec.pending_since is None


def test_pending_timeout_reminds_once_only() -> None:
    """挂起 N 小时（默认 2h）未确认：提醒恰一次；重复巡检不重复提醒。"""
    from datetime import datetime, timedelta

    sm, store, _ = make_sm()
    uid = "conv-remind"
    sm.ensure_conversation(uid)
    sm.transition(uid, PENDING, actor="ai:brain", reason="敏感操作挂起")
    rec = store.get(uid)
    assert rec is not None and rec.pending_since is not None
    base = datetime.fromisoformat(rec.pending_since)
    reminded: list[str] = []
    now = base + timedelta(hours=2.5)
    scan_pending_timeout(store, sm, now=now, remind=lambda u, h: reminded.append(u))
    assert reminded == [uid]
    # 再次巡检（尚未超 24h）：不重复提醒
    scan_pending_timeout(store, sm, now=now + timedelta(hours=1),
                         remind=lambda u, h: reminded.append(u))
    assert reminded == [uid]


def test_pending_timeout_24h_moves_to_human() -> None:
    """T7 挂起 24h 未确认：system:timeout 迁移 human_managed，reason=pending_timeout_24h。"""
    from datetime import datetime, timedelta

    sm, store, _ = make_sm()
    uid = "conv-timeout"
    sm.ensure_conversation(uid)
    sm.transition(uid, PENDING, actor="ai:brain", reason="敏感操作挂起")
    rec = store.get(uid)
    assert rec is not None and rec.pending_since is not None
    since = datetime.fromisoformat(rec.pending_since)
    scan_pending_timeout(store, sm, now=since + timedelta(hours=25))
    rec = store.get(uid)
    assert rec is not None
    assert rec.state is HUMAN
    assert rec.state_hist[-1]["by"] == "system:timeout"
    assert rec.state_hist[-1]["reason"] == "pending_timeout_24h"


def test_pending_timeout_never_back_to_ai() -> None:
    """红线（SRS §7.4）：挂起超时只转人工、绝不回 ai_managed 自动执行。"""
    from datetime import datetime, timedelta

    sm, store, _ = make_sm()
    uid = "conv-never-ai"
    sm.ensure_conversation(uid)
    sm.transition(uid, PENDING, actor="ai:brain", reason="挂起")
    rec = store.get(uid)
    assert rec is not None and rec.pending_since is not None
    since = datetime.fromisoformat(rec.pending_since)
    results = scan_pending_timeout(store, sm, now=since + timedelta(hours=30))
    rec = store.get(uid)
    assert rec is not None
    assert rec.state is HUMAN  # 绝非 AI_MANAGED
    assert all(r.to_state is not AI for r in results)


# ─────────────────────────── 发送闸门（物理阻断） ───────────────────────────


def test_send_allowed_only_ai_managed() -> None:
    """send_allowed：仅 ai_managed 为 True（四态逐一断言）。"""
    sm, _, _ = make_sm()
    assert sm.send_allowed(AI) is True
    assert sm.send_allowed(HUMAN) is False
    assert sm.send_allowed(PENDING) is False
    assert sm.send_allowed(CLOSED) is False


def test_gate_acquire_blocks_non_ai_states() -> None:
    """T5 闸门阻断：human/pending/closed 三态 acquire 全部抛 AISendBlockedError（中文）。"""
    sm, store, _ = make_sm()
    gate = AISendGate(store)
    for state in (HUMAN, PENDING, CLOSED):
        uid = f"conv-block-{state.value}"
        seed(sm, uid, state)
        with pytest.raises(AISendBlockedError, match="阻断"):
            gate.acquire(uid)


def test_gate_token_carries_current_version() -> None:
    """闸门快路径：ai_managed 态签发令牌，令牌携带当前 state_version。"""
    sm, store, _ = make_sm()
    gate = AISendGate(store)
    uid = "conv-token"
    sm.ensure_conversation(uid)
    sm.transition(uid, HUMAN, actor="user:a", reason="走一版")
    sm.transition(uid, AI, actor="user:a", reason="放回")
    rec = store.get(uid)
    assert rec is not None
    tok = gate.acquire(uid)
    assert isinstance(tok, SendToken)
    assert tok.uid == uid
    assert tok.version == rec.state_version


def test_gate_verify_fails_after_version_bump() -> None:
    """T6 令牌失配：acquire 后人工接管（版本 +1）再 verify → False，不得发送。"""
    sm, store, _ = make_sm()
    gate = AISendGate(store)
    uid = "conv-verify"
    sm.ensure_conversation(uid)
    tok = gate.acquire(uid)
    sm.transition(uid, HUMAN, actor="user:zhangsan", reason="生成中人工接管")
    assert gate.verify(uid, tok) is False


def test_gate_verify_true_when_untouched() -> None:
    """令牌未被竞态干扰：verify=True（发送前最后一瞬校验通过）。"""
    sm, store, _ = make_sm()
    gate = AISendGate(store)
    uid = "conv-verify-ok"
    sm.ensure_conversation(uid)
    tok = gate.acquire(uid)
    assert gate.verify(uid, tok) is True


def test_gate_token_ttl_expiry() -> None:
    """令牌 TTL 过期：verify=False（防拟人输入期间状态陈旧）。"""
    sm, store, _ = make_sm()
    now = [100.0]

    def fake_clock() -> float:
        return now[0]

    gate = AISendGate(store, token_ttl_sec=30.0, clock=fake_clock)
    uid = "conv-ttl"
    sm.ensure_conversation(uid)
    tok = gate.acquire(uid)
    now[0] += 31.0  # 推进时钟越过 TTL
    assert gate.verify(uid, tok) is False


def test_gate_verify_unknown_uid_false() -> None:
    """未知会话 verify 恒 False（宁拒勿发）。"""
    sm, store, _ = make_sm()
    gate = AISendGate(store)
    tok = SendToken(uid="ghost", version=0, issued_at=0.0)
    assert gate.verify("ghost", tok) is False


# ─────────────────────────── 崩溃恢复前置：rebuild ───────────────────────────


def test_rebuild_restores_state_version_hist() -> None:
    """T/§6.5 进程无状态化：新状态机实例从存储 rebuild → state/version/hist 完整一致；
    附会话记录默认值断言（新建即 ai_managed / version=0 / 空 hist，会话定位规则 §4）。"""
    empty = ConversationRecord(uid="conv-default")
    assert empty.state is AI and empty.state_version == 0
    assert empty.state_hist == [] and empty.pending_since is None
    sm, store, _ = make_sm()
    uid = "conv-rebuild"
    sm.ensure_conversation(uid)
    sm.transition(uid, PENDING, actor="ai:brain", reason="挂起")
    original = store.get(uid)
    assert original is not None
    # 模拟进程崩溃：全新状态机实例（零内存状态）挂同一存储
    sm2 = ConversationStateMachine(store)
    rec = sm2.rebuild(uid)
    assert rec.state is PENDING
    assert rec.state_version == original.state_version
    assert rec.state_hist == original.state_hist
    assert rec.pending_since == original.pending_since
