# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""告警通知单元测试（FR-AG-007，SR-AG-007 §6/§9；迭代 2.3 TDD 阶段①用例）。

覆盖（告警引擎 = 全部「需要人立即知道」事件的唯一汇聚与分发点）：
- 分级路由矩阵：high→钉钉机器人+微信即时；mid→钉钉群；low→后台待办；
- 同源去重聚合：同 dedup_key 5min 窗口内不重复推送、首条 agg_count 累加、窗口外新告警；
- 处置闭环状态机：firing→acknowledged→resolved→archived，非法迁移拒绝；
- 未确认 15min 升级主管（幂等，确认后不再升级）；
- 渠道失败重试（3 次）+ 备用通道兜底（仅 high）；
- 大客户（A 级）复核：grade=A 强制 high，复核不过降级 low 并留痕。

实现方案：docs/07-实现方案/AG/SR-AG-007-告警通知.md §6.1-§6.5、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.agent.core.alert.router import (
    Alert,
    AlertRouter,
    AlwaysFailingChannel,
    FakeChannel,
    FlakyChannel,
    InvalidTransitionError,
)

NOW = datetime(2026, 10, 8, 10, 0, 0)


def make_router(**overrides):
    """标准装配：Fake 渠道三件套 + 备用短信通道。"""
    ding = overrides.pop("dingtalk_robot", None) or FakeChannel("dingtalk_robot")
    wechat = overrides.pop("wechat_app", None) or FakeChannel("wechat_app")
    group = overrides.pop("dingtalk_group", None) or FakeChannel("dingtalk_group")
    todo = overrides.pop("backend_todo", None) or FakeChannel("backend_todo")
    supervisor = overrides.pop("supervisor", None) or FakeChannel("supervisor")
    fallback = overrides.pop("fallback_channel", None) or FakeChannel("sms")
    audit: list[dict] = []
    router = AlertRouter(
        channels={"dingtalk_robot": ding, "wechat_app": wechat,
                  "dingtalk_group": group, "backend_todo": todo, "supervisor": supervisor},
        fallback_channel=fallback,
        audit=audit.append,
        clock=lambda: NOW,
        **overrides,
    )
    state = {"dingtalk_robot": ding, "wechat_app": wechat, "dingtalk_group": group,
             "backend_todo": todo, "supervisor": supervisor, "sms": fallback, "audit": audit}
    return router, state


def submit(router: AlertRouter, **overrides) -> Alert:
    """标准上报（风控触发事件）。"""
    kwargs = dict(type="risk_hit", title="红线拦截", body="命中 RED_OFFSITE_INDUCE",
                  dedup_key="risk_hit:acc-1", now=NOW)
    kwargs.update(overrides)
    result = router.submit(**kwargs)
    return router.get(result.uid)


class TestLevelRouting:
    """分级路由矩阵（04 §3.5 路由原文）。"""

    def test_high_routes_dual_channel(self) -> None:
        """high → 钉钉机器人 + 微信即时双通道，各送达一次。"""
        router, state = make_router()
        alert = submit(router, level="high")
        assert len(state["dingtalk_robot"].sent) == 1
        assert len(state["wechat_app"].sent) == 1
        assert [d.channel for d in alert.deliveries] == ["dingtalk_robot", "wechat_app"]

    def test_mid_routes_group_only(self) -> None:
        """mid → 仅钉钉群。"""
        router, state = make_router()
        alert = submit(router, level="mid")
        assert len(state["dingtalk_group"].sent) == 1
        assert state["dingtalk_robot"].sent == []
        assert [d.channel for d in alert.deliveries] == ["dingtalk_group"]

    def test_low_routes_backend_todo(self) -> None:
        """low → 仅后台待办（无外呼）。"""
        router, state = make_router()
        alert = submit(router, level="low")
        assert len(state["backend_todo"].sent) == 1
        assert state["dingtalk_robot"].sent == []

    def test_type_default_level(self) -> None:
        """type→默认 level：risk_hit/big_customer→high，system_error→mid。"""
        router, _ = make_router()
        assert router.submit(type="risk_hit", title="t", body="b",
                             dedup_key="k1", now=NOW).level == "high"
        assert router.submit(type="big_customer", title="t", body="b",
                             dedup_key="k2", now=NOW).level == "high"
        assert router.submit(type="system_error", title="t", body="b",
                             dedup_key="k3", now=NOW).level == "mid"


class TestDedupAggregation:
    """同源 5min 去重聚合（风暴防护第一层）。"""

    def test_dedup_within_window(self) -> None:
        """同 dedup_key 5min 内重复上报：不推送新告警、首条 agg_count 累加。"""
        router, state = make_router()
        first = router.submit(type="risk_hit", title="t", body="b",
                              dedup_key="k", now=NOW)
        second = router.submit(type="risk_hit", title="t", body="b",
                               dedup_key="k", now=NOW + timedelta(minutes=3))
        assert first.aggregated is False
        assert second.aggregated is True
        assert len(state["dingtalk_robot"].sent) == 1        # 仅首条推送
        assert router.get(first.uid).agg_count == 2

    def test_dedup_window_expiry(self) -> None:
        """窗口（300s）外再次上报 → 视为新告警正常推送。"""
        router, state = make_router()
        first = router.submit(type="risk_hit", title="t", body="b",
                              dedup_key="k", now=NOW)
        second = router.submit(type="risk_hit", title="t", body="b",
                               dedup_key="k", now=NOW + timedelta(seconds=301))
        assert second.aggregated is False
        assert second.uid != first.uid
        assert len(state["dingtalk_robot"].sent) == 2


class TestLifecycle:
    """处置闭环状态机：firing→acknowledged→resolved→archived。"""

    def test_full_lifecycle(self) -> None:
        """全链迁移：确认/处置/归档各步回填操作人与时刻。"""
        router, _ = make_router()
        alert = submit(router)
        assert alert.status == "firing"
        acked = router.ack(alert.uid, operator="sale-07", now=NOW + timedelta(minutes=2))
        assert acked.status == "acknowledged" and acked.acked_by == "sale-07"
        resolved = router.resolve(alert.uid, operator="sale-07", now=NOW + timedelta(minutes=10))
        assert resolved.status == "resolved" and resolved.resolved_by == "sale-07"
        archived = router.archive(alert.uid, now=NOW + timedelta(days=7))
        assert archived.status == "archived"

    def test_illegal_transitions_rejected(self) -> None:
        """非法迁移拒绝：firing→resolved、resolved→acknowledged 均拒绝。"""
        router, _ = make_router()
        alert = submit(router)
        with pytest.raises(InvalidTransitionError):
            router.resolve(alert.uid, operator="x", now=NOW)
        router.ack(alert.uid, operator="x", now=NOW)
        router.resolve(alert.uid, operator="x", now=NOW)
        with pytest.raises(InvalidTransitionError):
            router.ack(alert.uid, operator="x", now=NOW)


class TestEscalation:
    """未确认 15min 升级主管（05 §10 升级原文）。"""

    def test_escalate_overdue(self) -> None:
        """high firing 超 15min 未确认 → 升级：@主管 + escalated 留痕 + 审计。"""
        router, state = make_router()
        alert = submit(router, level="high")
        escalated = router.escalate_overdue(now=NOW + timedelta(minutes=16))
        assert [a.uid for a in escalated] == [alert.uid]
        assert router.get(alert.uid).escalated is True
        assert len(state["supervisor"].sent) == 1
        assert any(entry["event"] == "alert_escalated" for entry in state["audit"])

    def test_no_escalation_within_timeout(self) -> None:
        """15min 内不升级。"""
        router, state = make_router()
        submit(router, level="high")
        assert router.escalate_overdue(now=NOW + timedelta(minutes=14)) == []
        assert state["supervisor"].sent == []

    def test_escalation_idempotent_and_stops_after_ack(self) -> None:
        """升级幂等（不重复 @主管）；确认后不再升级。"""
        router, state = make_router()
        alert = submit(router, level="high")
        late = NOW + timedelta(minutes=16)
        router.escalate_overdue(now=late)
        router.escalate_overdue(now=late + timedelta(minutes=1))
        assert len(state["supervisor"].sent) == 1
        router.ack(alert.uid, operator="sale", now=late + timedelta(minutes=2))
        assert router.escalate_overdue(now=late + timedelta(minutes=30)) == []


class TestRetryAndFallback:
    """渠道失败重试 + 备用通道兜底（04 §4 外部接口韧性）。"""

    def test_retry_then_success(self) -> None:
        """前 2 次失败第 3 次成功：送达状态 sent、attempts=3。"""
        router, state = make_router(dingtalk_robot=FlakyChannel("dingtalk_robot", fail_times=2))
        alert = submit(router, level="high")
        delivery = alert.deliveries[0]
        assert delivery.channel == "dingtalk_robot"
        assert delivery.status == "sent" and delivery.attempts == 3

    def test_all_fail_fallback_sms_high_only(self) -> None:
        """high 双通道 3 次重试全失败 → 备用短信兜底 + 审计留痕。"""
        router, state = make_router(dingtalk_robot=AlwaysFailingChannel("dingtalk_robot"),
                                    wechat_app=AlwaysFailingChannel("wechat_app"))
        alert = submit(router, level="high")
        assert len(state["sms"].sent) == 1                     # 备用通道兜底
        assert all(d.status in ("failed", "fallback") for d in alert.deliveries)
        assert any(entry["event"] == "alert_fallback_sms" for entry in state["audit"])

    def test_mid_fail_no_sms_fallback(self) -> None:
        """mid 渠道失败不走短信兜底（仅 high），delivery=failed 待巡检。"""
        router, state = make_router(dingtalk_group=AlwaysFailingChannel("dingtalk_group"))
        alert = submit(router, level="mid")
        assert state["sms"].sent == []
        assert alert.deliveries[0].status == "failed"


class TestBigCustomer:
    """大客户判定复核（customer.grade == 'A'）。"""

    def test_grade_a_forced_high(self) -> None:
        """A 级 → 强制 high 双通道（不可被调用方降级）。"""
        router, state = make_router()
        alert = submit(router, type="big_customer", customer_grade="A",
                       dedup_key="big-1", title="A 级客户新询盘")
        assert alert.level == "high"
        assert len(state["dingtalk_robot"].sent) == 1

    def test_grade_b_downgraded_to_low(self) -> None:
        """复核不过（B 级）→ 降级 low 后台待办 + alert_downgraded 审计留痕。"""
        router, state = make_router()
        alert = submit(router, type="big_customer", customer_grade="B",
                       dedup_key="big-2", title="B 级误报")
        assert alert.level == "low"
        assert state["dingtalk_robot"].sent == []
        assert any(entry["event"] == "alert_downgraded" for entry in state["audit"])

    def test_audit_received_written(self) -> None:
        """受理即落审计（全量留审计，NFR-SEC-03）。"""
        router, state = make_router()
        submit(router)
        assert any(entry["event"] == "alert_received" for entry in state["audit"])
