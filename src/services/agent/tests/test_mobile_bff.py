# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""移动端 BFF 单元测试（FR-SC-006，SR-SC-006 §6.1-§6.4/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（双端免登矩阵、一屏聚合、审批确认/驳回）/
边界（校验码第 5 次锁定、令牌 120s 过期、订阅余量恰好为 0）/ 异常（未绑定 40301
不泄露存在性、学员角色越权 40301、无令牌确认一律拒绝）/ 幂等（verify_token 单次
消费防重放、Idempotency-Key 透传去重、聚合短缓存）/ 降级（下游单路失败该卡片置
空其余可用；微信订阅额度耗尽与 43101 走降级链 mp_template→sms）。
验收口径映射：敏感操作红线（SRS §7.4/§16.5）——移动端 confirm 强制二次校验 +
令牌单次消费（无令牌绝不透传下游）。

实现方案：docs/07-实现方案/SC/SR-SC-006-移动端.md §6.2、§6.3、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.agent.core.mobile.bff import (
    Binding,
    BindingStore,
    DingTalkWorkFake,
    FakeApprovalSink,
    FakeConversationSink,
    FakeHomeProvider,
    IdentityStub,
    MobileBff,
    MobileError,
    MpTemplateFake,
    PushRouter,
    SmsFake,
    VerifyGate,
    WxSubscribeFake,
)

NOW = datetime(2026, 10, 11, 10, 0, 0)
ROLES = {"supervisor:1": ("supervisor",), "sales:1": ("sales",)}


class Harness:
    """标准装配：双端绑定 + 记录型下游 + 推送路由（测试直接读各 Fake 观测面）。"""

    def __init__(self, *, clock: object | None = None) -> None:
        self.now = NOW
        self.clock = clock or (lambda: self.now)
        self.codes: list[tuple[str, str]] = []                 # (user_id, 校验码) 演练观测面
        self.bindings = BindingStore()
        self.bindings.add(Binding("supervisor:1", "dingtalk", "dd-001", "王主管"))
        self.bindings.add(Binding("supervisor:1", "wechat", "wx-open-001", "王主管"))
        self.bindings.add(Binding("sales:1", "dingtalk", "dd-002", "李销售"))
        self.identity = IdentityStub(
            dingtalk_users={"auth-ok": "dd-001", "auth-sales": "dd-002"},
            wechat_users={"code-ok": "wx-open-001"})
        self.home = FakeHomeProvider()
        self.approvals = FakeApprovalSink()
        self.conversations = FakeConversationSink()
        self.dingtalk = DingTalkWorkFake()
        self.wx = WxSubscribeFake()
        self.mp = MpTemplateFake()
        self.sms = SmsFake()
        self.push = PushRouter(self.dingtalk, self.wx, self.mp, self.sms, clock=self.clock)
        self.verify = VerifyGate(
            code_sender=lambda uid, code: self.codes.append((uid, code)), clock=self.clock)
        self.audit: list[dict] = []
        self.bff = MobileBff(
            bindings=self.bindings, identity=self.identity, roles_of=ROLES.get,
            home=self.home, approvals=self.approvals, conversations=self.conversations,
            push=self.push, verify=self.verify, audit=self.audit, clock=self.clock)

    def login_supervisor(self, channel: str = "dingtalk") -> str:
        """主管登录取 token（钉钉免登 / 微信 code）。"""
        credential = "auth-ok" if channel == "dingtalk" else "code-ok"
        return str(self.bff.login(channel, credential)["token"])

    def obtain_verify_token(self, token: str) -> str:
        """挑战 → 正确校验码 → 换取一次性确认令牌。"""
        self.bff.request_challenge(token)
        code = self.codes[-1][1]
        return str(self.bff.issue_verify_token(token, code)["verify_token"])


def make_harness() -> Harness:
    """标准用例夹具。"""
    return Harness()


class TestLoginAndAuth:
    """等价类/异常：双端免登换发与鉴权。"""

    def test_login_both_channels_and_audit(self) -> None:
        """钉钉免登 + 微信 code2session → 换发含角色 token；审计 mobile_login×2。"""
        h = make_harness()
        dd = h.bff.login("dingtalk", "auth-ok")
        wx = h.bff.login("wechat", "code-ok")
        assert dd["user"]["roles"] == ("supervisor",) and wx["user"]["name"] == "王主管"
        assert {e["action_type"] for e in h.audit} == {"mobile_login"}
        assert len(h.audit) == 2

    def test_login_unbound_no_leak_and_unknown_channel(self) -> None:
        """未绑定 external_id → 40301 且文案不泄露账号存在性；非法渠道 → 40001。"""
        h = make_harness()
        with pytest.raises(MobileError) as unbound:
            h.bff.login("dingtalk", "auth-unknown")
        assert unbound.value.code == 40301
        assert "不存在" not in unbound.value.message
        with pytest.raises(MobileError) as channel:
            h.bff.login("sms", "whatever")
        assert channel.value.code == 40001

    def test_auth_unknown_or_expired_token(self) -> None:
        """降级：伪造 token → 40101；过期（jwt_ttl 之后）→ 40101。"""
        h = make_harness()
        token = h.login_supervisor()
        assert h.bff.auth(token).user_id == "supervisor:1"
        with pytest.raises(MobileError) as bogus:
            h.bff.auth("not-a-token")
        assert bogus.value.code == 40101
        h.now = NOW + timedelta(seconds=7201)
        with pytest.raises(MobileError):
            h.bff.auth(token)


class TestHomeAggregation:
    """等价类/降级/幂等：一屏一请求聚合。"""

    def test_home_summary_one_screen_payload(self) -> None:
        """一屏一请求：告警/审批/接管/今日 KPI 四卡片一次返回（§6.4 出参形态）。"""
        h = make_harness()
        token = h.login_supervisor()
        payload = h.bff.home_summary(token)
        assert set(payload) == {"todo_alerts", "todo_approvals", "taking_over", "today"}
        assert payload["todo_alerts"] == 3 and payload["today"]["inquiries"] == 137

    def test_home_cache_one_request_per_window(self) -> None:
        """幂等：缓存窗口内重复进入零下游调用；过期后重新聚合（15s 短缓存）。"""
        h = make_harness()
        token = h.login_supervisor()
        h.bff.home_summary(token)
        assert len(h.home.calls) == 4
        h.bff.home_summary(token)
        assert len(h.home.calls) == 4
        h.now = NOW + timedelta(seconds=16)
        h.bff.home_summary(token)
        assert len(h.home.calls) == 8

    def test_home_degraded_route_none(self) -> None:
        """降级：单路下游超时 → 该卡片 None（端上显示—），其余卡片正常。"""
        h = make_harness()
        h.home.fail_routes.add("alerts")
        payload = h.bff.home_summary(h.login_supervisor())
        assert payload["todo_alerts"] is None
        assert payload["todo_approvals"] == 1 and payload["taking_over"] == 2


class TestVerifyGate:
    """边界/异常：二次校验（挑战 → 令牌 → 单次消费）。"""

    def test_verify_flow_issues_single_use_token(self) -> None:
        """正确校验码 → 发令牌；GETDEL 单次消费：第二次 False；120s 过期后 False。"""
        h = make_harness()
        token = h.login_supervisor()
        h.bff.request_challenge(token)
        code = h.codes[-1][1]
        vt = h.bff.issue_verify_token(token, code)["verify_token"]
        assert h.verify.consume(vt, "supervisor:1") is True
        assert h.verify.consume(vt, "supervisor:1") is False           # 已消费
        h.bff.request_challenge(token)
        vt2 = h.bff.issue_verify_token(token, h.codes[-1][1])["verify_token"]
        h.now = NOW + timedelta(seconds=121)
        assert h.verify.consume(vt2, "supervisor:1") is False          # 过期

    def test_verify_locks_after_five_failures(self) -> None:
        """边界：连续错 4 次仍可再试；第 5 次 → 40102 且锁定（正确码也拒绝）。"""
        h = make_harness()
        token = h.login_supervisor()
        h.bff.request_challenge(token)
        code = h.codes[-1][1]
        for _ in range(4):
            with pytest.raises(MobileError):
                h.bff.issue_verify_token(token, "000000")
        with pytest.raises(MobileError) as fifth:
            h.bff.issue_verify_token(token, code)
        assert fifth.value.code == 40102
        with pytest.raises(MobileError):                               # 锁定期内正确码也拒绝
            h.bff.issue_verify_token(token, code)


class TestApprovalFlow:
    """红线/幂等：会话审批移动流（§6.2）。"""

    def test_confirm_without_token_blocked_no_downstream(self) -> None:
        """红线兜底：无令牌/伪造令牌 confirm → 40102，且绝不透传下游（零调用）。"""
        h = make_harness()
        token = h.login_supervisor()
        with pytest.raises(MobileError) as none_case:
            h.bff.confirm_approval(token, "sa-1", "", idem_key="k1")
        with pytest.raises(MobileError) as bogus:
            h.bff.confirm_approval(token, "sa-1", "faked-token", idem_key="k1")
        assert none_case.value.code == bogus.value.code == 40102
        assert h.approvals.executed == 0

    def test_confirm_with_token_downstream_once_and_replay_blocked(self) -> None:
        """正常流：二次校验后确认（下游执行 1 次 + 审计 client=mobile）；令牌重放 → 40102。"""
        h = make_harness()
        token = h.login_supervisor()
        vt = h.obtain_verify_token(token)
        result = h.bff.confirm_approval(token, "sa-1", vt, note="同意改价", idem_key="k2")
        assert result["ok"] is True and h.approvals.executed == 1
        assert any(e["action_type"] == "sensitive_confirm"
                   and e["extra"]["client"] == "mobile" for e in h.audit)
        with pytest.raises(MobileError) as replay:
            h.bff.confirm_approval(token, "sa-1", vt, idem_key="k3")   # 令牌已消费
        assert replay.value.code == 40102
        assert h.approvals.executed == 1

    def test_reject_idempotent_and_role_guard(self) -> None:
        """驳回免二次校验仍幂等（同 key 仅执行一次）；学员角色审批/接管 → 40301。"""
        h = make_harness()
        token = h.login_supervisor()
        first = h.bff.reject_approval(token, "sa-1", "金额存疑", idem_key="k4")
        second = h.bff.reject_approval(token, "sa-1", "金额存疑", idem_key="k4")
        assert first == second and h.approvals.executed == 0           # 驳回走 reject 通道
        assert h.approvals.reject_executed == 1
        sales = h.bff.login("dingtalk", "auth-sales")["token"]
        with pytest.raises(MobileError) as guard:
            h.bff.confirm_approval(sales, "sa-1", "x", idem_key="k5")
        assert guard.value.code == 40301

    def test_takeover_and_push_register(self) -> None:
        """人工介入接管透传状态机；push_register 累加微信订阅额度（引导授权口径）。"""
        h = make_harness()
        token = h.login_supervisor()
        assert h.bff.takeover(token, "conv-9", reason="客户投诉升级")["state"] == "human_managed"
        assert h.conversations.records == [("conv-9", "supervisor:1")]
        wx_token = h.bff.login("wechat", "code-ok")["token"]
        before = h.push.quota_of("wx-open-001")
        h.bff.push_register(wx_token, openid="wx-open-001", add_quota=2)
        assert h.push.quota_of("wx-open-001") == before + 2


class TestPushRouting:
    """降级：推送通道适配 Protocol + Fake（钉钉主力 vs 订阅额度制 + 降级链）。"""

    def test_dingtalk_work_direct_with_deep_link(self) -> None:
        """钉钉绑定 → 工作通知直发（主力通道，含深链），无降级。"""
        h = make_harness()
        b = h.bindings.get("dingtalk", "dd-001")
        assert b is not None
        deliveries = h.push.push(user_id="supervisor:1", bindings=[b],
                                 title="A 级客户新询盘", body="请关注", deep_link="/pages/alert/center")
        assert deliveries == [{"channel": "dingtalk_work", "status": "sent"}]
        assert h.dingtalk.outbox[0]["deep_link"] == "/pages/alert/center"

    def test_wechat_quota_fallback_chain_and_43101(self) -> None:
        """微信端：有额度 → 订阅消息（额度扣减）；零额度 → 不外呼 + mp_template 降级；
        平台 43101 → 余量清零留痕并降级；全链耗尽 → sms 兜底。"""
        h = make_harness()
        b = h.bindings.get("wechat", "wx-open-001")
        assert b is not None
        h.push.register_quota("wx-open-001", add=1)
        d1 = h.push.push(user_id="supervisor:1", bindings=[b], title="t", body="b")
        assert d1[0] == {"channel": "wx_subscribe", "status": "sent"}
        assert h.push.quota_of("wx-open-001") == 0
        d2 = h.push.push(user_id="supervisor:1", bindings=[b], title="t", body="b")   # 额度尽
        assert d2[0]["channel"] == "mp_template" and d2[0]["status"] == "fallback"
        assert h.wx.calls == 1 and "wx-open-001" in h.push.resubscribe_flags
        h.push.register_quota("wx-open-001", add=1)
        h.wx.next_errcode = 43101                                      # 平台判未订阅
        d3 = h.push.push(user_id="supervisor:1", bindings=[b], title="t", body="b")
        assert h.push.quota_of("wx-open-001") == 0
        assert d3[0]["channel"] in {"mp_template", "sms"}
        h.wx.next_errcode = None
        h.mp.fail_next = True                                          # 服务号模板也失败 → 短信兜底
        d4 = h.push.push(user_id="supervisor:1", bindings=[b], title="t", body="b")
        assert d4[-1]["channel"] == "sms" and d4[-1]["status"] in {"fallback", "failed"}
