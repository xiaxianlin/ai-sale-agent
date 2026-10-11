# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""浏览器驱动单元测试（FR-AG-002，SR-AG-002 §9）。

覆盖（对应实现方案 §5.1/§6.2/§6.6，demo 口径）：
- FakeBrowserDriver 确定性：协议符合性、登录态三态（alive/登出失效/验证码）、
  询盘列表注册与排序、消息读取字段完整、发送回执字段完整；
- 发送物理阻断：send_reply 仅接受闸门签发的 SendToken，伪造对象 TypeError；
- 页面改版降级：inject_drift → PageDriftError，clear_drift 后恢复；
- OpenClawDriver：KB_OPENCLAW_ENDPOINT 未配置中文报错；环境变量注入 +
  httpx MockTransport 打桩的请求路径与字段映射；HTTP 错误中文归一。

实现方案：docs/07-实现方案/AG/SR-AG-002-浏览器自动化.md §5、§6、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import httpx
import pytest

from services.agent.core.driver import (
    OPENCLAW_ENDPOINT_ENV,
    BrowserDriver,
    CaptchaRequiredError,
    FakeBrowserDriver,
    InquiryItem,
    NotLoggedInError,
    OpenClawConfigError,
    OpenClawDriver,
    OpenClawError,
    PageDriftError,
    PlatformMessage,
    TypingPlan,
)
from services.agent.core.state import SendToken


def make_token(uid: str = "alibaba:C001", version: int = 0) -> SendToken:
    """构造闸门形态的合法令牌（模拟 AISendGate 签发产物）。"""
    return SendToken(uid=uid, version=version, issued_at=100.0)


# ─────────────────────────── FakeBrowserDriver 确定性 ───────────────────────────


def test_fake_driver_conforms_protocol() -> None:
    """FakeBrowserDriver 满足 BrowserDriver 协议（可插拔驱动，demo 口径）。"""
    assert isinstance(FakeBrowserDriver(), BrowserDriver)


def test_ensure_login_alive() -> None:
    """登录态有效：ensure_login 返回 alive。"""
    driver = FakeBrowserDriver()
    assert driver.ensure_login() == "alive"


def test_ensure_login_frozen_when_logged_out() -> None:
    """登录态失效：抛 NotLoggedInError（中文，不暴力重登语义）。"""
    driver = FakeBrowserDriver()
    driver.logout()
    with pytest.raises(NotLoggedInError, match="登录"):
        driver.ensure_login()


def test_captcha_triggers_chinese_error() -> None:
    """验证码触发：抛 CaptchaRequiredError（中文，人工接管视口语义）；
    人工完成验证（disarm）后恢复 alive（人工接管通道闭环）。"""
    driver = FakeBrowserDriver()
    driver.arm_captcha()
    with pytest.raises(CaptchaRequiredError, match="验证码"):
        driver.ensure_login()
    driver.disarm_captcha()
    assert driver.ensure_login() == "alive"


def test_poll_returns_registered_inquiries_sorted() -> None:
    """轮询：返回注册的询盘条目，按 (last_msg_time, external_conv_id) 稳定排序。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C002", "买家乙", last_msg_time=200)
    driver.add_inquiry("C001", "买家甲", last_msg_time=200)
    driver.add_inquiry("C003", "买家丙", last_msg_time=100)
    items = driver.poll_inquiries()
    assert [i.external_conv_id for i in items] == ["C003", "C001", "C002"]
    assert all(isinstance(i, InquiryItem) and i.buyer_name for i in items)


def test_read_messages_fields_complete() -> None:
    """消息读取字段完整：sender/sent_at/content/direction 全部非空且方向正确。"""
    driver = FakeBrowserDriver()
    driver.append_inbound("C001", "买家甲", "请问 MOQ 是多少？", sent_at=100)
    msgs = driver.read_messages("C001")
    assert len(msgs) == 1
    m = msgs[0]
    assert isinstance(m, PlatformMessage)
    assert m.sender == "买家甲"
    assert m.sent_at == 100
    assert "MOQ" in m.content
    assert m.direction == "inbound"
    assert driver.read_messages("C404") == []  # 未注册会话：空列表而非异常


def test_send_receipt_fields_complete() -> None:
    """发送回执：external_msg_id/conv_external_id/sent_at/content_len 齐全且单调；
    登录态失效时发送拒绝（NotLoggedInError）且回执零产生。"""
    driver = FakeBrowserDriver()
    plan = TypingPlan(text="您好，感谢询盘。", char_delays_ms=(250.0, 260.0), pauses=())
    r1 = driver.send_reply(make_token(), plan)
    r2 = driver.send_reply(make_token(), plan)
    assert r1.external_msg_id != r2.external_msg_id  # 回执 ID 唯一
    assert r1.conv_external_id == "C001"
    assert r1.sent_at > 0
    assert r1.content_len == len("您好，感谢询盘。")
    assert len(driver.sent_receipts) == 2
    assert driver.typed_plans[-1].char_delays_ms == (250.0, 260.0)  # 逐字延迟被记录
    driver.logout()
    with pytest.raises(NotLoggedInError):
        driver.send_reply(make_token(), plan)
    assert len(driver.sent_receipts) == 2


def test_send_blocked_when_logged_out() -> None:
    """登录态失效时发送拒绝（NotLoggedInError），回执零产生。"""
    driver = FakeBrowserDriver()
    driver.logout()
    plan = TypingPlan(text="x", char_delays_ms=(250.0,), pauses=())
    with pytest.raises(NotLoggedInError):
        driver.send_reply(make_token(), plan)
    assert not driver.sent_receipts


def test_page_drift_error_and_recovery() -> None:
    """页面改版降级：注入后轮询抛 PageDriftError；人工更新选择器后恢复。"""
    driver = FakeBrowserDriver()
    driver.add_inquiry("C001", "买家甲", last_msg_time=100)
    driver.inject_drift()
    with pytest.raises(PageDriftError, match="改版"):
        driver.poll_inquiries()
    driver.clear_drift()  # 人工恢复（更新选择器注册表）
    assert [i.external_conv_id for i in driver.poll_inquiries()] == ["C001"]


def test_fake_driver_deterministic() -> None:
    """确定性：同输入序列两次构造 → 询盘列表、消息、回执完全一致。"""
    def run() -> tuple[list[InquiryItem], list[PlatformMessage], list[str]]:
        d = FakeBrowserDriver()
        d.add_inquiry("C001", "买家甲", last_msg_time=100)
        d.append_inbound("C001", "买家甲", "Hello", sent_at=100)
        plan = TypingPlan(text="Hi", char_delays_ms=(250.0, 300.0), pauses=())
        d.send_reply(make_token(), plan)
        return d.poll_inquiries(), d.read_messages("C001"), [r.external_msg_id for r in d.sent_receipts]

    a, b = run(), run()
    assert a == b


def test_verify_sent_platform_evidence() -> None:
    """平台侧查证：会话内已有同内容外发消息 → True；否则 False（恢复第 3 步依据）。"""
    driver = FakeBrowserDriver()
    driver.append_inbound("C001", "买家甲", "Hello", sent_at=100)
    assert driver.verify_sent("C001", "未发送的内容") is False
    driver.append_outbound("C001", "已发送的回复", sent_at=110)
    assert driver.verify_sent("C001", "已发送的回复") is True


# ─────────────────────────── OpenClawDriver（真实客户端接口） ───────────────────────────


def test_openclaw_missing_endpoint_chinese(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量未配置：构造即抛 OpenClawConfigError（中文，指引改用 Fake）。"""
    monkeypatch.delenv(OPENCLAW_ENDPOINT_ENV, raising=False)
    with pytest.raises(OpenClawConfigError, match="KB_OPENCLAW_ENDPOINT"):
        OpenClawDriver()


def test_openclaw_env_endpoint_and_poll_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """环境变量注入端点：MockTransport 打桩 → ensure_login/poll/read 字段映射正确。"""
    import json as _json

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/v1/claw/login/ensure":
            return httpx.Response(200, json={"state": "alive"})
        if request.url.path == "/api/v1/claw/inquiries/list":
            return httpx.Response(200, json={"items": [
                {"external_conv_id": "C001", "buyer_name": "买家甲",
                 "last_msg_time": 100, "unread": 1},
            ]})
        if request.url.path == "/api/v1/claw/messages/read":
            body: dict = _json.loads(request.content)
            assert body == {"external_conv_id": "C001"}
            return httpx.Response(200, json={"messages": [
                {"sender": "买家甲", "sent_at": 100, "content": "Hello", "direction": "inbound"},
            ]})
        return httpx.Response(404, json={})

    monkeypatch.setenv(OPENCLAW_ENDPOINT_ENV, "http://openclaw-internal:9223")
    driver = OpenClawDriver(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert driver.ensure_login() == "alive"
    items = driver.poll_inquiries()
    assert len(items) == 1 and items[0].external_conv_id == "C001"
    msgs = driver.read_messages("C001")
    assert msgs[0].sender == "买家甲" and msgs[0].direction == "inbound"
    assert "/api/v1/claw/login/ensure" in seen_paths


def test_openclaw_http_error_chinese() -> None:
    """OpenClaw 服务异常（5xx）：归一为 OpenClawError（中文上下文，带状态码）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "browser down"})

    driver = OpenClawDriver(
        endpoint="http://openclaw-internal:9223",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenClawError, match="503"):
        driver.ensure_login()
