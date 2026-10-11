# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""浏览器驱动层（FR-AG-002，SR-AG-002 §5.1/§6.2/§6.6；迭代 2.1 demo 口径）。

阿里国际站仅经 OpenClaw 浏览器自动化访问，无直连 API（04-接口设计 §4）。
本模块定义可插拔驱动协议与两实现：

- FakeBrowserDriver：确定性模拟阿里国际站页面行为（demo 验收口径）——登录态
  （有效/失效/验证码）、询盘列表、消息读取、拟人化输入记录、发送回执、
  页面改版注入（PageDriftError）、平台侧发送查证（恢复协议依据）；
- OpenClawDriver：真实客户端接口（httpx）。KB_OPENCLAW_ENDPOINT 未配置即
  中文报错拒绝构造（fail-fast）；HTTP 非 2xx 归一为 OpenClawError（中文上下文）。

发送物理阻断：send_reply 仅接受 AISendGate 签发的 SendToken 实例（state 模块），
任何伪造形态（字符串/None/数字）一律 TypeError 且零外发——「仅 ai_managed 可发」
是结构保证而非调用约定。
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from services.agent.core.state import SendToken

#: OpenClaw 服务端点环境变量（demo 未配置时拒绝构造真实驱动）
OPENCLAW_ENDPOINT_ENV = "KB_OPENCLAW_ENDPOINT"

#: OpenClaw 内部 API 前缀（04-接口设计 §4 agent-runner ↔ OpenClaw 形态）
_CLAW_API = "/api/v1/claw"


# ─────────────────────────── 异常 ───────────────────────────


class DriverError(RuntimeError):
    """驱动层基础异常：中文上下文，上层捕获后降级/告警/冻结。"""


class NotLoggedInError(DriverError):
    """登录态失效：Cookie 过期或未登录（不暴力重登，冻结等人工受控重登）。"""


class CaptchaRequiredError(DriverError):
    """平台触发验证码/滑块：需人工在接管视口完成验证后恢复自动化。"""


class PageDriftError(DriverError):
    """页面改版：关键定位键双通道连续失败，自动化冻结待人工更新选择器注册表。"""


class OpenClawConfigError(DriverError):
    """OpenClaw 配置缺失：KB_OPENCLAW_ENDPOINT 未配置（demo 指引改用 Fake）。"""


class OpenClawError(DriverError):
    """OpenClaw 调用失败：HTTP 非 2xx 或响应不可解析（归一中文上下文）。"""


# ─────────────────────────── 数据契约 ───────────────────────────


@dataclass(frozen=True)
class InquiryItem:
    """询盘列表条目（列表页视觉读取产物）。"""

    external_conv_id: str    # 平台会话外部 ID（会话定位键）
    buyer_name: str          # 买家名（新建会话提示）
    last_msg_time: int       # 水位①：最后消息平台时间（epoch 秒）
    unread: int = 1          # 未读数


@dataclass(frozen=True)
class PlatformMessage:
    """平台消息（消息气泡读取产物，字段完整入回调/落库）。"""

    sender: str              # 发件人
    sent_at: int             # 平台时间（epoch 秒）
    content: str             # 内容
    direction: str           # inbound / outbound


@dataclass(frozen=True)
class TypingPlan:
    """拟人化输入计划（engine.HumanTyper 产物，NFR-SEC-11）。

    char_delays_ms：逐字延迟（3-5 字符/秒换算区间 [200, 333.4]ms）；
    pauses：(字符下标, 停顿时长 ms)，时长落 [150, 800]ms（随机停顿/思考感）。
    """

    text: str
    char_delays_ms: tuple[float, ...]
    pauses: tuple[tuple[int, float], ...]

    @property
    def total_ms(self) -> float:
        """计划总时长：逐字延迟与停顿之和（执行段预算依据）。"""
        return sum(self.char_delays_ms) + sum(dur for _idx, dur in self.pauses)


@dataclass(frozen=True)
class SendReceipt:
    """发送回执（平台受理凭证，审计与幂等依据）。"""

    external_msg_id: str     # 平台消息 ID（回执唯一）
    conv_external_id: str    # 会话外部 ID
    sent_at: int             # 受理时间（epoch 秒）
    content_len: int         # 内容长度（审计只记长度不记内容，NFR-SEC-01）


@runtime_checkable
class BrowserDriver(Protocol):
    """浏览器驱动统一协议：Fake 与 OpenClaw 双实现同构（可插拔，demo 口径）。"""

    def ensure_login(self) -> str:
        """登录保持探测：返回 alive；失效抛 NotLoggedInError / CaptchaRequiredError。"""
        ...

    def poll_inquiries(self) -> list[InquiryItem]:
        """询盘轮询：列表页未读条目（按 (last_msg_time, external_conv_id) 稳定排序）。"""
        ...

    def read_messages(self, external_conv_id: str) -> list[PlatformMessage]:
        """消息读取：指定会话全部气泡（sender/sent_at/content/direction）。"""
        ...

    def send_reply(self, token: SendToken, plan: TypingPlan) -> SendReceipt:
        """拟人化输入并提交发送：仅接受闸门签发的 SendToken（物理阻断）。"""
        ...

    def verify_sent(self, external_conv_id: str, content: str) -> bool:
        """平台侧发送查证（NFR-REL-03 恢复第 3 步依据）：会话内已有同内容外发 → True。"""
        ...


def _require_send_token(token: object) -> SendToken:
    """发送闸门物理阻断的实现端：非 SendToken 实例一律 TypeError。"""
    if not isinstance(token, SendToken):
        raise TypeError(
            "send_reply 需要有效 SendToken（发送闸门物理阻断：非 AISendGate 签发不得发送）"
        )
    return token


# ─────────────────────────── FakeBrowserDriver ───────────────────────────


class FakeBrowserDriver:
    """确定性模拟阿里国际站页面行为（demo 验收口径，不发起任何网络请求）。

    可注入面：登录态（logout/login）、验证码（arm/disarm）、页面改版
    （inject/clear drift）、询盘列表（add_inquiry）、消息（append_inbound/
    append_outbound）；记录面：sent_receipts / typed_plans（逐字延迟可断言）。
    确定性：时钟为固定步进计数器，列表稳定排序，回执 ID 顺序生成——
    同输入序列两次构造结果完全一致。
    """

    def __init__(self, *, start_time: int = 1_700_000_000, tick: int = 60) -> None:
        self._clock: Iterator[int] = itertools.count(start_time, tick)
        self.logged_in: bool = True
        self._captcha_armed: bool = False
        self._drift: bool = False
        self._inquiries: dict[str, InquiryItem] = {}
        self._messages: dict[str, list[PlatformMessage]] = {}
        self.sent_receipts: list[SendReceipt] = []
        self.typed_plans: list[TypingPlan] = []

    # ---- 注入面（测试编排用） ----

    def next_time(self) -> int:
        """推进内部时钟（注册询盘/消息缺省时间戳来源）。"""
        return next(self._clock)

    def add_inquiry(
        self, external_conv_id: str, buyer_name: str, *, last_msg_time: int | None = None
    ) -> InquiryItem:
        """注册/更新询盘条目（同会话二次注册模拟平台列表重排）。"""
        item = InquiryItem(
            external_conv_id=external_conv_id,
            buyer_name=buyer_name,
            last_msg_time=self.next_time() if last_msg_time is None else last_msg_time,
        )
        self._inquiries[external_conv_id] = item
        return item

    def append_inbound(
        self, external_conv_id: str, sender: str, content: str, *, sent_at: int | None = None
    ) -> PlatformMessage:
        """追加买家消息。"""
        return self._append(external_conv_id, sender, content, "inbound", sent_at)

    def append_outbound(
        self, external_conv_id: str, content: str, *, sent_at: int | None = None
    ) -> PlatformMessage:
        """追加座席外发消息（模拟平台事实，供 verify_sent 查证）。"""
        return self._append(external_conv_id, "agent", content, "outbound", sent_at)

    def _append(
        self, external_conv_id: str, sender: str, content: str, direction: str,
        sent_at: int | None,
    ) -> PlatformMessage:
        message = PlatformMessage(
            sender=sender,
            sent_at=self.next_time() if sent_at is None else sent_at,
            content=content,
            direction=direction,
        )
        self._messages.setdefault(external_conv_id, []).append(message)
        return message

    def logout(self) -> None:
        """置登录态失效（Cookie 过期演练）。"""
        self.logged_in = False

    def login(self) -> None:
        """恢复登录态（人工受控重登演练）。"""
        self.logged_in = True

    def arm_captcha(self) -> None:
        """下一次登录探测触发验证码（需人工接管视口完成验证）。"""
        self._captcha_armed = True

    def disarm_captcha(self) -> None:
        """人工完成验证后解除。"""
        self._captcha_armed = False

    def inject_drift(self) -> None:
        """注入页面改版：关键定位键双通道失败（降级演练）。"""
        self._drift = True

    def clear_drift(self) -> None:
        """人工更新选择器注册表后恢复。"""
        self._drift = False

    # ---- BrowserDriver 协议实现 ----

    def ensure_login(self) -> str:
        """探测顺序：验证码优先（人工接管通道）→ 登录态失效 → alive。"""
        if self._captcha_armed:
            raise CaptchaRequiredError("平台触发登录验证码：需人工在接管视口完成验证后恢复自动化")
        if not self.logged_in:
            raise NotLoggedInError("登录态失效：Cookie 过期或未登录（不暴力重登，等待人工受控重登）")
        return "alive"

    def poll_inquiries(self) -> list[InquiryItem]:
        """轮询列表页：未登录/页面改版抛对应异常；条目按水位元组稳定排序。"""
        self._ensure_readable()
        return sorted(
            self._inquiries.values(),
            key=lambda item: (item.last_msg_time, item.external_conv_id),
        )

    def read_messages(self, external_conv_id: str) -> list[PlatformMessage]:
        """读取会话气泡：未注册会话返回空列表（非异常，轮询语义）。"""
        self._ensure_readable()
        return list(self._messages.get(external_conv_id, []))

    def send_reply(self, token: SendToken, plan: TypingPlan) -> SendReceipt:
        """拟人化输入并提交：闸门令牌校验（物理阻断）→ 记录输入 → 产出回执。"""
        _require_send_token(token)
        if not self.logged_in:
            raise NotLoggedInError("登录态失效：发送被拒绝（不暴力重登）")
        self.typed_plans.append(plan)
        receipt = SendReceipt(
            external_msg_id=f"sent-{len(self.sent_receipts) + 1:04d}",
            conv_external_id=self._conv_of(token.uid),
            sent_at=self.next_time(),
            content_len=len(plan.text),
        )
        self.sent_receipts.append(receipt)
        return receipt

    def verify_sent(self, external_conv_id: str, content: str) -> bool:
        """平台侧查证：会话内存在同内容外发消息即视为已发（不猜测不盲发）。"""
        return any(
            m.direction == "outbound" and m.content == content
            for m in self._messages.get(external_conv_id, [])
        )

    # ---- 内部 ----

    def _ensure_readable(self) -> None:
        """读取类动作前置：登录态与页面指纹检查。"""
        if not self.logged_in:
            raise NotLoggedInError("登录态失效：无法读取页面（冻结等人工恢复）")
        if self._drift:
            raise PageDriftError(
                "疑似页面改版：关键定位键双通道连续失败，自动化已冻结"
                "（请更新选择器注册表后恢复实例）"
            )

    @staticmethod
    def _conv_of(uid: str) -> str:
        """从会话 uid（channel:external 形态）提取外部 ID。"""
        return uid.split(":", 1)[1] if ":" in uid else uid


# ─────────────────────────── OpenClawDriver（真实客户端接口） ───────────────────────────


class OpenClawDriver:
    """OpenClaw 真实客户端接口（httpx；demo 以桩 transport 打样）。

    构造即要求 KB_OPENCLAW_ENDPOINT（或显式 endpoint）配置，未配置中文报错
    拒绝启动——避免静默把生产流量打到默认地址；HTTP 非 2xx 归一 OpenClawError。
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._endpoint = endpoint or os.environ.get(OPENCLAW_ENDPOINT_ENV)
        if not self._endpoint:
            raise OpenClawConfigError(
                f"环境变量 {OPENCLAW_ENDPOINT_ENV} 未配置：无法连接 OpenClaw 浏览器自动化服务"
                "（demo 环境请改用 FakeBrowserDriver，生产经环境变量/KMS 注入端点）"
            )
        self._client = client or httpx.Client(base_url=self._endpoint, timeout=timeout_seconds)

    # ---- BrowserDriver 协议实现 ----

    def ensure_login(self) -> str:
        """POST /login/ensure：OpenClaw 探测登录态并返回 alive。"""
        body = self._post(f"{_CLAW_API}/login/ensure", {})
        state = str(body.get("state", ""))
        if not state:
            raise OpenClawError("OpenClaw 登录探测响应缺少 state 字段")
        return state

    def poll_inquiries(self) -> list[InquiryItem]:
        """POST /inquiries/list：列表页未读条目映射。"""
        body = self._post(f"{_CLAW_API}/inquiries/list", {})
        return [
            InquiryItem(
                external_conv_id=str(item["external_conv_id"]),
                buyer_name=str(item.get("buyer_name", "")),
                last_msg_time=int(item.get("last_msg_time", 0)),
                unread=int(item.get("unread", 1)),
            )
            for item in body.get("items", [])
        ]

    def read_messages(self, external_conv_id: str) -> list[PlatformMessage]:
        """POST /messages/read：会话气泡映射（字段完整）。"""
        body = self._post(f"{_CLAW_API}/messages/read", {"external_conv_id": external_conv_id})
        return [
            PlatformMessage(
                sender=str(m["sender"]),
                sent_at=int(m["sent_at"]),
                content=str(m["content"]),
                direction=str(m.get("direction", "inbound")),
            )
            for m in body.get("messages", [])
        ]

    def send_reply(self, token: SendToken, plan: TypingPlan) -> SendReceipt:
        """POST /reply/send：闸门令牌物理校验 → 提交拟人化计划 → 回执映射。"""
        _require_send_token(token)
        body = self._post(f"{_CLAW_API}/reply/send", {
            "uid": token.uid,
            "version": token.version,
            "text": plan.text,
            "char_delays_ms": list(plan.char_delays_ms),
            "pauses": [[idx, dur] for idx, dur in plan.pauses],
        })
        return SendReceipt(
            external_msg_id=str(body["external_msg_id"]),
            conv_external_id=str(body.get("conv_external_id", "")),
            sent_at=int(body.get("sent_at", 0)),
            content_len=int(body.get("content_len", len(plan.text))),
        )

    def verify_sent(self, external_conv_id: str, content: str) -> bool:
        """POST /reply/verify：平台侧发送查证（读会话末条比对，NFR-REL-03）。"""
        body = self._post(f"{_CLAW_API}/reply/verify", {
            "external_conv_id": external_conv_id, "content": content,
        })
        return bool(body.get("sent", False))

    # ---- 内部 ----

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        """统一 POST：非 2xx 归一 OpenClawError（中文上下文，带状态码）。

        URL 以端点拼接为绝对地址——注入的桩 client（MockTransport，无 base_url）
        与生产 client（base_url=端点）两种形态均可用。
        """
        url = path if path.startswith("http") else f"{self._endpoint}{path}"
        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise OpenClawError(f"OpenClaw 调用失败：{path} 网络异常（{exc.__class__.__name__}）") from exc
        if response.status_code >= 400:
            raise OpenClawError(
                f"OpenClaw 调用失败：{path} 返回 HTTP {response.status_code}"
                "（服务不可用或页面行为异常，请检查 OpenClaw 实例）"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise OpenClawError(f"OpenClaw 调用失败：{path} 响应不是 JSON 对象")
        return data
