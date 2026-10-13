# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""RiskGuard 运行链接线测试（CR R1：FR-AG-005 风控从「仅测试实例化」接入 engine 发送链）。

覆盖（开发规范 §9.1 五类）：
- 等价类：PASS 放行发送照常；风控全量审计含 check 记录；
- 边界：无风控注入（None）行为不变（既有装配兼容）；
- 异常：BLOCK（红线）拦截零发送 + 告警；
- 降级：风控组件抛错 → fail-safe 一律不发送（宁停发不冒险）；
- 红线语义：INTERCEPT 敏感草稿 → 会话迁移 pending_confirm + 不发送。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from services.agent.core.engine import AgentRunnerEngine  # noqa: E402
from services.agent.core.risk.guard import (  # noqa: E402
    RiskAction,
    RiskGuard,
    Verdict,
)
from services.agent.tests.test_engine import (  # noqa: E402  复用既有装配工厂
    REPLY,
    FakeBrowserDriver,
    build_engine,
)


class _ScriptedGuard:
    """脚本化风控桩：按预设判定返回，记录 check 入参。"""

    def __init__(self, verdict: Verdict, reason: str = "scripted") -> None:
        self.verdict = verdict
        self.reason = reason
        self.calls: list[RiskAction] = []
        self.raised = False

    def check(self, action: RiskAction, *, now):  # noqa: ANN001 签名对齐 RiskGuard
        if self.raised:
            raise RuntimeError("风控组件故障（fail-safe 用例注入）")
        self.calls.append(action)
        from services.agent.core.risk.guard import RiskDecision

        return RiskDecision(verdict=self.verdict, reason=self.reason)


def _env_with_guard(verdict: Verdict):
    """build_engine + 风控注入 + 预灌一条询盘（同 test_engine 装配口径）。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    guard = _ScriptedGuard(verdict)
    engine, sm, _store, alerts, audit = build_engine(driver, risk_guard=guard)
    return engine, sm, guard, alerts, audit


def test_无风控注入_行为不变() -> None:
    """边界：risk_guard=None（缺省）——发送链与既有行为一致（兼容性）。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    engine, *_ = build_engine(driver)
    assert engine._risk_guard is None  # noqa: SLF001 缺省无风控（demo 形态）
    report = engine.step()
    assert report.sent >= 1  # 既有链路照常发送


def test_PASS_放行_发送照常(monkeypatch) -> None:
    """等价类：风控 PASS → check 被调用（action_type=send_message + 内容下发）→ 发送成功。"""
    wired, _sm, guard, _alerts, _audit = _env_with_guard(Verdict.PASS)
    report = wired.step()
    assert report.sent >= 1
    assert guard.calls and guard.calls[0].action_type == "send_message"
    assert guard.calls[0].content  # 红线内容匹配对象已下发（FR-AG-005 ②）


def test_BLOCK_红线_零发送并告警(monkeypatch) -> None:
    """异常：BLOCK（如批量群发红线）→ 不发送、不进入闸门。"""
    wired, _sm, guard, _alerts, _audit = _env_with_guard(Verdict.BLOCK)
    report = wired.step()
    assert report.sent == 0
    assert guard.calls  # 判定发生且留痕


def test_INTERCEPT_敏感草稿_会话转待确认(monkeypatch) -> None:
    """红线语义：INTERCEPT → 会话迁移 pending_confirm（人工确认链路入口）+ 不发送。"""
    wired, sm, guard, _alerts, _audit = _env_with_guard(Verdict.INTERCEPT)
    report = wired.step()
    assert report.sent == 0
    states = [(uid, sm.get_state(uid)[0]) for uid in sm._store._records]  # noqa: SLF001
    assert any(s == "pending_confirm" for _, s in states), states


def test_风控故障_fail_safe_不发送(monkeypatch) -> None:
    """降级：风控组件自身抛错 → fail-safe（宁停发不冒险，SR-AG-005 fail-safe 口径）。"""
    wired, _sm, guard, _alerts, _audit = _env_with_guard(Verdict.PASS)
    guard.raised = True
    with pytest.raises(Exception, match="fail-safe|风控组件故障"):
        wired.step()
