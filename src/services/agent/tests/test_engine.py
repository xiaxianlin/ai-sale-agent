# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""任务主循环（引擎）单元测试（FR-AG-002，SR-AG-002 §6.1/§6.3）。

覆盖：
- step() 单步可测：poll → read → process 回调 → 闸门 → send 全链；
- 水位防重复：同一询盘两轮 step 只处理一次；游标持久化 + 重启恢复（新引擎挂同存储）；
- 幂等去重：列表重排（同条目二次进入）被去重键拦截；水位元组比较过滤旧条目；
- 发送链：ai_managed 态经闸门发送并审计；人工接管态 AI 静默零发送（物理阻断兜底）；
- 生成中接管竞态：process 回调内发生接管 → 放弃发送（人工优先）；
- 页面改版 → 降级告警 + 冻结；验证码 → 人工接管通道；冻结后拒绝 step 直至 resume。

实现方案：docs/07-实现方案/AG/SR-AG-002-浏览器自动化.md §6.1、§6.3、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import random
from collections.abc import Callable

import pytest

from services.agent.core.driver import FakeBrowserDriver, PlatformMessage
from services.agent.core.engine import (
    AgentRunnerEngine,
    ConvCtx,
    EngineFrozenError,
    HumanTyper,
    InMemoryAlerts,
    InMemoryPollCursorStore,
    conv_uid,
    external_of,
)
from services.agent.core.state import (
    ConversationStateMachine,
    ConvState,
    InMemorySessionStore,
    AISendGate,
)

REPLY = "您好，感谢询盘，我们的报价随后附上。"


def build_engine(
    driver: FakeBrowserDriver,
    store: InMemorySessionStore | None = None,
    cursor_store: InMemoryPollCursorStore | None = None,
    process: Callable[[ConvCtx, list[PlatformMessage]], str | None] | None = None,
) -> tuple[
    AgentRunnerEngine,
    ConversationStateMachine,
    InMemorySessionStore,
    InMemoryAlerts,
    list[dict],
]:
    """标准装配：Fake 驱动 + 内存存储 + 记录型回调（返回固定草稿）。"""
    store = store or InMemorySessionStore()
    cursor_store = cursor_store or InMemoryPollCursorStore()
    sm = ConversationStateMachine(store)
    gate = AISendGate(store)
    alerts = InMemoryAlerts()
    audit: list[dict] = []

    def default_process(ctx: ConvCtx, msgs: list[PlatformMessage]) -> str | None:
        return REPLY

    engine = AgentRunnerEngine(
        account_id=1,
        driver=driver,
        state_machine=sm,
        gate=gate,
        cursor_store=cursor_store,
        typer=HumanTyper(rng=random.Random(99)),
        process_reply=process or default_process,
        alerts=alerts,
        audit=audit.append,
    )
    return engine, sm, store, alerts, audit


# ─────────────────────────── 主循环与水位 ───────────────────────────


def test_step_processes_new_inquiry() -> None:
    """step() 全链：新询盘 → 会话定位（未命中新建 ai_managed）→ 读取（字段完整入
    回调）→ 回复 → 经闸门发送恰一封 + 审计一条（含回执 ID）。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    driver.append_inbound("C001", "买家甲", "Hello", sent_at=100)
    seen: list[tuple[ConvCtx, list[PlatformMessage]]] = []
    engine, sm, store, _, audit = build_engine(
        driver, process=lambda ctx, msgs: (seen.append((ctx, msgs)), REPLY)[1]
    )
    report = engine.step()
    assert report.status == "ok"
    assert report.polled == 1 and report.new_items == 1 and report.sent == 1
    assert len(seen) == 1
    ctx, msgs = seen[0]
    assert ctx.uid == conv_uid("C001")            # 会话 uid 形态 channel:external
    assert external_of(ctx.uid) == "C001"
    rec = store.get(ctx.uid)
    assert rec is not None
    assert rec.state is ConvState.AI_MANAGED      # 未命中会话新建即 ai_managed（§4 规则）
    assert msgs[0].sender == "买家甲"
    assert len(driver.sent_receipts) == 1
    assert len(audit) == 1
    assert audit[0]["event"] == "send_reply"
    assert audit[0]["conv_uid"] == "alibaba:C001"
    assert audit[0]["receipt_id"] == driver.sent_receipts[0].external_msg_id


def test_watermark_prevents_reprocessing() -> None:
    """水位不重复处理：同一询盘两轮 step → 第二轮零新条目零发送。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    driver.append_inbound("C001", "买家甲", "Hello", sent_at=100)
    engine, _, _, _, _ = build_engine(driver)
    assert engine.step().sent == 1
    report2 = engine.step()
    assert report2.new_items == 0
    assert report2.sent == 0
    assert len(driver.sent_receipts) == 1


def test_cursor_persisted_and_restored() -> None:
    """游标持久化 + 重启恢复：新引擎挂同一游标存储 → 旧消息不重复处理（状态外置）。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    driver.append_inbound("C001", "买家甲", "Hello", sent_at=100)
    cursor_store = InMemoryPollCursorStore()
    engine1, _, _, _, _ = build_engine(driver, cursor_store=cursor_store)
    engine1.step()
    assert cursor_store.load(1).last_seen_time == 100  # 游标已落存储
    engine2, _, _, _, _ = build_engine(driver, cursor_store=cursor_store)  # "重启"
    report = engine2.step()
    assert report.new_items == 0 and report.sent == 0
    assert len(driver.sent_receipts) == 1


def test_dedup_blocks_relisted_item() -> None:
    """幂等去重（水位回拨/列表重排）：同 (会话, 时间) 二次进入被去重键拦截。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=200)
    driver.append_inbound("C001", "买家甲", "Hello", sent_at=200)
    engine, _, _, _, _ = build_engine(driver)
    engine.step()
    driver.add_inquiry("C001", "买家甲", last_msg_time=150)  # 平台列表重排出旧时间戳
    report = engine.step()
    assert report.skipped_seen == 1
    assert report.sent == 0
    assert len(driver.sent_receipts) == 1


def test_older_items_filtered_by_cursor_tuple() -> None:
    """水位元组比较：时间早于游标的条目直接过滤（防回拨重复处理）。"""
    from services.agent.core.engine import PollCursor

    driver = FakeBrowserDriver()
    driver.add_inquiry("C-new", "新买家", last_msg_time=300)
    driver.add_inquiry("C-old", "老买家", last_msg_time=50)
    cursor_store = InMemoryPollCursorStore()
    cursor_store.save(1, PollCursor(last_seen_time=200, last_external_id="C-x"))  # 预置水位
    engine, _, _, _, _ = build_engine(driver, cursor_store=cursor_store)
    report = engine.step()
    assert report.new_items == 1  # 仅 C-new（300 > 200）进入处理
    assert report.skipped_seen == 1  # C-old（50 < 200）被元组比较过滤


def test_ensure_conversation_creates_ai_managed() -> None:
    """会话定位规则：未命中既有会话 → 新建且默认 ai_managed（SR-AG-002 §4）。"""
    driver = FakeBrowserDriver()
    engine, sm, store, _, _ = build_engine(driver)
    ctx = engine.ensure_conversation("C777", buyer_hint="买家丁")
    assert ctx.uid == "alibaba:C777"
    rec = store.get(ctx.uid)
    assert rec is not None
    assert rec.state is ConvState.AI_MANAGED
    assert external_of(ctx.uid) == "C777"


# ─────────────────────────── 发送闸门与人工优先 ───────────────────────────


def test_silent_when_human_managed() -> None:
    """人工接管态 AI 静默：草稿生成但不发送（发送闸门物理阻断兜底）。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    engine, sm, _, _, _ = build_engine(driver)
    sm.ensure_conversation("alibaba:C001")
    sm.transition("alibaba:C001", ConvState.HUMAN_MANAGED, actor="user:boss", reason="人工接管")
    report = engine.step()
    assert report.sent == 0
    assert not driver.sent_receipts


def test_takeover_during_process_no_send() -> None:
    """生成中人工接管：process 回调内发生接管 → 发送前状态校验放弃（人工优先原则）。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    store = InMemorySessionStore()
    sm = ConversationStateMachine(store)
    sm.ensure_conversation("alibaba:C001")

    def racing_process(ctx: ConvCtx, msgs: list[PlatformMessage]) -> str | None:
        # 模拟 AI 生成期间人工在后台点击接管
        sm.transition(ctx.uid, ConvState.HUMAN_MANAGED,
                      actor="user:boss", reason="生成中接管")
        return REPLY

    engine, _, _, _, _ = build_engine(driver, store=store, process=racing_process)
    report = engine.step()
    assert report.sent == 0
    assert not driver.sent_receipts


# ─────────────────────────── 降级与人工接管通道 ───────────────────────────


def test_page_drift_freezes_and_alerts() -> None:
    """页面改版：双通道失败 → high 告警 + 冻结 frozen:page_drift + 恢复后继续。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    driver.inject_drift()
    engine, _, _, alerts, _ = build_engine(driver)
    report = engine.step()
    assert report.status == "frozen:page_drift"
    assert engine.login_state == "frozen:page_drift"
    assert alerts.last(level="high", type_="page_drift") is not None
    with pytest.raises(EngineFrozenError, match="冻结"):
        engine.step()                                   # 冻结态拒绝任何平台动作（宁停勿撞）
    driver.clear_drift()
    engine.resume()  # 人工更新选择器注册表后恢复
    assert engine.step().sent == 1  # 冻结发生在 poll 阶段，询盘未消费；恢复后正常处理
    assert len(driver.sent_receipts) == 1


def test_captcha_freezes_and_alerts_takeover_channel() -> None:
    """验证码触发：high 告警 + frozen:captcha（人工接管视口）；解除后恢复轮询。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    driver.arm_captcha()
    engine, _, _, alerts, _ = build_engine(driver)
    report = engine.step()
    assert report.status == "frozen:captcha"
    assert alerts.last(level="high", type_="captcha") is not None
    driver.disarm_captcha()  # 人工在接管视口完成验证
    engine.resume()
    assert engine.step().sent == 1


def test_login_expired_freezes_with_alert() -> None:
    """Cookie 失效：不暴力重登 → frozen:cookie_expired + high 告警（等人工受控重登）。"""
    driver = FakeBrowserDriver()
    driver.logout()
    engine, _, _, alerts, _ = build_engine(driver)
    report = engine.step()
    assert report.status == "frozen:cookie_expired"
    assert alerts.last(level="high", type_="cookie_expired") is not None
    driver.login()
    engine.resume()
    assert engine.step().status == "ok"
