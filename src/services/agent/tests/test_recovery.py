# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""故障恢复单元测试（NFR-REL-03，SR-NFR-REL-03 §6.3/§6.4/§6.6）。

覆盖：
- 进程无状态化：重启后从存储重建会话状态（权威在存储，实例可随时丢弃）；
- 中断任务重放幂等：sending 半途任务以平台侧查证收敛——已发补账 sent 不重发、
  未发回置 queued 重放恰一次（幂等键 = 会话 + 消息内容 hash，CAS 防重发）；
- kill 混沌演练脚本化：构造半途状态 → 模拟 kill 丢弃进程实例 → recover →
  断言状态一致 + 零重复发送 + 异常重启告警；
- 崩溃期间人工接管 → 重放被发送闸门物理阻断转 handover（绝不越权代发）；
- mark_sent CAS 恰一胜（双路径并发防线）；idem_key 形状与唯一性；
- RTO 计时断言（rto_sec 实测入报告，目标 <2min）；recover 幂等可重入。

实现方案：docs/07-实现方案/NFR/SR-NFR-REL-03-故障恢复.md §6、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import random

from services.agent.core.driver import FakeBrowserDriver, TypingPlan
from services.agent.core.engine import (
    HumanTyper,
    InMemoryAlerts,
    conv_uid,
)
from services.agent.core.recovery import (
    JOB_GENERATING,
    JOB_HANDOVER,
    JOB_QUEUED,
    JOB_SENDING,
    JOB_SENT,
    InMemoryReplyJobStore,
    RecoveryManager,
    RecoverReport,
    ReplyJob,
    idem_key,
)
from services.agent.core.state import (
    AISendGate,
    ConversationStateMachine,
    ConvState,
    InMemorySessionStore,
)


def rec(store: InMemorySessionStore, uid: str):
    """取会话记录（断言存在，收窄 Optional）。"""
    found = store.get(uid)
    assert found is not None
    return found


def build_world() -> tuple[
    InMemorySessionStore,
    ConversationStateMachine,
    AISendGate,
    FakeBrowserDriver,
    InMemoryReplyJobStore,
    InMemoryAlerts,
    RecoveryManager,
]:
    """标准装配：存储 + 状态机 + 闸门 + Fake 驱动 + 任务库 + 告警 + 恢复器。"""
    store = InMemorySessionStore()
    sm = ConversationStateMachine(store)
    gate = AISendGate(store)
    driver = FakeBrowserDriver()
    jobs = InMemoryReplyJobStore()
    alerts = InMemoryAlerts()
    rm = RecoveryManager(
        store=store,
        state_machine=sm,
        gate=gate,
        driver=driver,
        job_store=jobs,
        alerts=alerts,
        typer=HumanTyper(rng=random.Random(11)),
    )
    return store, sm, gate, driver, jobs, alerts, rm


def make_job(
    jobs: InMemoryReplyJobStore,
    conv_ext: str,
    content: str,
    status: str,
    account_id: int = 1,
) -> ReplyJob:
    """构造指定状态的任务（模拟崩溃前半途状态）。"""
    job = ReplyJob.new(
        account_id=account_id, conversation_uid=conv_uid(conv_ext), content=content
    )
    job.status = status
    jobs.upsert(job)
    return job


# ─────────────────────────── 会话状态重建 ───────────────────────────


def test_recover_rebuilds_all_sessions() -> None:
    """状态重建：三个不同状态会话 → recover 全部从存储重建，状态零丢失。"""
    store, sm, _, _, jobs, _, rm = build_world()
    sm.ensure_conversation(conv_uid("C001"))
    sm.ensure_conversation(conv_uid("C002"))
    sm.ensure_conversation(conv_uid("C003"))
    sm.transition(conv_uid("C002"), ConvState.HUMAN_MANAGED, actor="user:a", reason="接管")
    sm.transition(conv_uid("C003"), ConvState.PENDING_CONFIRM, actor="ai:brain", reason="挂起")
    make_job(jobs, "C001", "待重放回复", JOB_QUEUED)
    report = rm.recover(account_id=1)
    assert report.rebuilt_sessions == 3
    assert store.get(conv_uid("C002")).state is ConvState.HUMAN_MANAGED  # type: ignore[union-attr]
    assert len(store.get(conv_uid("C002")).state_hist) == 1  # type: ignore[union-attr]
    assert store.get(conv_uid("C003")).state is ConvState.PENDING_CONFIRM  # type: ignore[union-attr]


# ─────────────────────────── 中断任务重放幂等 ───────────────────────────


def test_recover_replay_not_sent_once() -> None:
    """未发送才重发：sending 且平台无证据 → 回置重放恰一次，任务落 sent。"""
    _, _, _, driver, jobs, _, rm = build_world()
    job = make_job(jobs, "C001", "半途回复", JOB_SENDING)
    report = rm.recover(account_id=1)
    assert report.replayed == 1
    assert report.sent_now == 1
    assert len(driver.sent_receipts) == 1  # 恰重发一次
    assert jobs.get(job.uid).status == JOB_SENT  # type: ignore[union-attr]


def test_recover_sending_confirmed_sent_no_resend() -> None:
    """已发补账：sending 且平台侧已有同内容外发 → CAS 置 sent，零重发。"""
    _, _, _, driver, jobs, _, rm = build_world()
    job = make_job(jobs, "C001", "已经发出去的回复", JOB_SENDING)
    driver.append_outbound("C001", "已经发出去的回复", sent_at=120)  # 平台事实
    report = rm.recover(account_id=1)
    assert report.marked_sent == 1
    assert report.sent_now == 0
    assert not driver.sent_receipts  # 无二次外发
    assert jobs.get(job.uid).status == JOB_SENT  # type: ignore[union-attr]


def test_recover_mixed_jobs_zero_duplicate_send() -> None:
    """混合半途状态：3 已发 + 2 未发 + 1 queued → 重放恰好 3 封，零重复。"""
    _, _, _, driver, jobs, _, rm = build_world()
    for i in range(3):
        make_job(jobs, f"Csent{i}", f"已发内容{i}", JOB_SENDING)
        driver.append_outbound(f"Csent{i}", f"已发内容{i}", sent_at=100 + i)
    make_job(jobs, "Cmid1", "未发内容一", JOB_SENDING)
    make_job(jobs, "Cmid2", "未发内容二", JOB_SENDING)
    make_job(jobs, "Cqueued", "排队内容", JOB_QUEUED)
    report = rm.recover(account_id=1)
    assert report.sent_now == 3 and report.marked_sent == 3
    assert len(driver.sent_receipts) == 3  # 终极断言：零重复发送
    for i in range(3):
        assert jobs.get_by_idem(idem_key(conv_uid(f"Csent{i}"), f"已发内容{i}")) is not None
        assert rec_for(jobs, i).status == JOB_SENT


def rec_for(jobs: InMemoryReplyJobStore, i: int) -> ReplyJob:
    """按会话反查已补账任务（测试辅助，断言存在性）。"""
    found = jobs.get_by_idem(idem_key(conv_uid(f"Csent{i}"), f"已发内容{i}"))
    assert found is not None
    return found


def test_recover_idempotent_rerun() -> None:
    """恢复协议幂等可重入：二次 recover 结果一致，无重复重放与发送。"""
    _, _, _, driver, jobs, _, rm = build_world()
    make_job(jobs, "C001", "半途回复", JOB_SENDING)
    first = rm.recover(account_id=1)
    second = rm.recover(account_id=1)
    assert first.replayed == 1 and second.replayed == 0
    assert len(driver.sent_receipts) == 1  # 不因二次恢复而多发


def test_generating_job_replayed() -> None:
    """generating 无外发副作用：回置 queued → 重放补发（任务不丢）。"""
    _, _, _, driver, jobs, _, rm = build_world()
    job = make_job(jobs, "C001", "生成中断回复", JOB_GENERATING)
    report = rm.recover(account_id=1)
    assert report.replayed == 1
    assert jobs.get(job.uid).status == JOB_SENT  # type: ignore[union-attr]
    assert len(driver.sent_receipts) == 1


def test_replay_handover_when_human_gate() -> None:
    """崩溃期间人工接管：重放被发送闸门物理阻断 → 任务转 handover，零发送。"""
    store, sm, _, driver, jobs, _, rm = build_world()
    sm.ensure_conversation(conv_uid("C001"))
    sm.transition(conv_uid("C001"), ConvState.HUMAN_MANAGED, actor="user:boss", reason="崩溃期间接管")
    job = make_job(jobs, "C001", "被接管的回复", JOB_SENDING)
    report = rm.recover(account_id=1)
    assert report.handed_over == 1
    assert report.sent_now == 0
    assert not driver.sent_receipts
    assert jobs.get(job.uid).status == JOB_HANDOVER  # type: ignore[union-attr]


# ─────────────────────────── kill 混沌演练（脚本化） ───────────────────────────


def test_kill_drill_chaos_sending_window() -> None:
    """KillDrill（SR-NFR-REL-03 §6.6 脚本化版）：sending 窗口定向打击。

    时序：正常发送一封（落账）→ 构造第二封半途 sending → kill（丢弃进程实例，
    保留外部存储与驱动）→ 新进程 recover → 断言：异常重启告警、状态一致、
    零重复发送、RTO 达标。
    """
    store, sm, gate, driver, jobs, alerts, _rm = build_world()
    conv_ext = "C-drill"
    sm.ensure_conversation(conv_uid(conv_ext))
    # ① 第一封：完整走完发送（模拟 kill 前的正常路径）
    sent_job = make_job(jobs, conv_ext, "kill 前已完成的回复", JOB_SENDING)
    plan = TypingPlan(text="kill 前已完成的回复", char_delays_ms=(250.0,), pauses=())
    driver.send_reply(gate.acquire(conv_uid(conv_ext)), plan)
    jobs.cas_status(sent_job.uid, JOB_SENDING, JOB_SENT)
    # ② 第二封：构造半途状态（发送中崩溃的不确定区，平台无证据）
    make_job(jobs, conv_ext, "kill 时半途的回复", JOB_SENDING)
    before = rec(store, conv_uid(conv_ext))
    snapshot = (before.state, before.state_version, len(before.state_hist))
    # ③ kill：进程实例全部丢弃（_rm / sm / gate 皆不可再引用），仅外部存储存活
    del _rm, sm, gate
    # ④ 新进程恢复：外部权威存储 + 全新状态机/闸门/恢复器
    new_sm = ConversationStateMachine(store)
    new_gate = AISendGate(store)
    new_rm = RecoveryManager(
        store=store, state_machine=new_sm, gate=new_gate, driver=driver,
        job_store=jobs, alerts=alerts, typer=HumanTyper(rng=random.Random(11)),
    )
    report = new_rm.recover(account_id=1)
    # ⑤ 断言：告警触达 + 状态零丢失 + 零重复发送 + RTO 达标
    assert alerts.last(level="mid", type_="restart") is not None  # 异常重启告警
    after = rec(store, conv_uid(conv_ext))
    assert snapshot == (after.state, after.state_version, len(after.state_hist))
    assert len(driver.sent_receipts) == 2  # 第一封不重发 + 第二封恰重放一次 = 零重复
    assert report.ready is True
    assert report.rto_sec < 120.0  # RTO 目标 <2min（demo 进程内实测为毫秒级）


# ─────────────────────────── 幂等键与 CAS ───────────────────────────


def test_idem_key_shape_and_uniqueness() -> None:
    """幂等键：同会话同内容稳定一致；跨会话/跨内容互异；32 位十六进制；
    任务库同 idem_key 重复 upsert 不产生重复任务（PG UNIQUE 语义）。"""
    k1a = idem_key("alibaba:C001", "同样的内容")
    k1b = idem_key("alibaba:C001", "同样的内容")
    k2 = idem_key("alibaba:C002", "同样的内容")
    k3 = idem_key("alibaba:C001", "不同的内容")
    assert k1a == k1b and k1a != k2 and k1a != k3
    assert len(k1a) == 32
    assert all(c in "0123456789abcdef" for c in k1a)
    jobs = InMemoryReplyJobStore()
    a = ReplyJob.new(account_id=1, conversation_uid=conv_uid("C1"), content="内容")
    b = ReplyJob.new(account_id=1, conversation_uid=conv_uid("C1"), content="内容")
    assert a.idem_key == b.idem_key
    jobs.upsert(a)
    jobs.upsert(b)  # 幂等覆盖
    assert jobs.get_by_idem(a.idem_key).uid == a.uid  # type: ignore[union-attr]
    assert len(jobs.list_inflight(1)) == 1  # 仅一个任务实体


def test_mark_sent_cas_exactly_one() -> None:
    """CAS 防重发（幂等键③）：双路径并发推进 sending→sent 恰一成功。"""
    jobs = InMemoryReplyJobStore()
    job = make_job(jobs, "C1", "并发补账内容", JOB_SENDING)
    first = jobs.cas_status(job.uid, JOB_SENDING, JOB_SENT)
    second = jobs.cas_status(job.uid, JOB_SENDING, JOB_SENT)
    assert first is True
    assert second is False  # 败者放弃，绝不二次外发


def test_rto_within_target() -> None:
    """RTO 计时断言：recover 实测 rto_sec > 0 且低于目标 120s（<2min 验收口径）；
    RecoverReport 契约字段完整（ready/rto_sec/rebuilt/replayed/sent_now/marked/handover）。"""
    _, _, _, _, jobs, _, rm = build_world()
    make_job(jobs, "C1", "rto 计时内容", JOB_SENDING)
    report = rm.recover(account_id=1)
    assert 0.0 < report.rto_sec < 120.0
    assert report.ready is True
    assert isinstance(report, RecoverReport)
    assert set(report.__dataclass_fields__) == {
        "ready", "rto_sec", "rebuilt_sessions", "replayed",
        "sent_now", "marked_sent", "handed_over",
    }
