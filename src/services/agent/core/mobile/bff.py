# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""移动端 BFF：双端免登换发 + 一屏聚合 + 会话审批移动流 + 推送路由
（FR-SC-006，SR-SC-006 §6.1-§6.4）。

- 免登换发（§6.1）：钉钉免登凭据 / 微信 code2session → external_id → 绑定表换发
  自签会话令牌（HMAC demo 形态，生产 = JWT + RS256）；未绑定一律 40301 且文案
  不泄露账号存在性（枚举防护）；
- 一屏聚合（§6.4）：告警待办/审批待办/接管中/今日 KPI 四卡片一次请求；15s 短缓存
  （幂等：窗口内重复进入零下游调用）；单路下游失败该卡片置 None 其余可用（降级）；
- 审批移动流（§6.2，SRS §7.4 红线端上加严）：confirm 必须先 challenge → 校验码 →
  verify_token（120s、单次消费、绑定用户）→ 才透传下游；无令牌/伪造/重放一律
  40102 且下游零调用（绝不透传）；连续错 4 次后锁定（第 5 次起全拒）；reject 低风险
  免二次校验但幂等（同 Idempotency-Key 仅执行一次）；主管角色校验（40301）；
- 推送路由（§6.3）：钉钉绑定 → 工作通知直发（主力，含深链）；微信端订阅额度制：
  有额度扣减发订阅消息，零额度不外呼 + 留痕引导重订阅 + mp_template 降级，平台
  43101 判未订阅同样清额度留痕降级，模板也失败 → sms 兜底（Protocol+Fake 接缝）。

demo 形态：进程内存储与 HMAC 令牌（生产 = Redis GETDEL 令牌、PG binding、
钉钉/微信开放平台真实网关，接缝不变）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

#: 会话令牌有效期（秒；2 小时滑动，过期强制重登）
SESSION_TTL_S: int = 7200
#: 二次校验令牌有效期（秒；SRS §7.4 高危操作即时确认口径）
VERIFY_TTL_S: int = 120
#: 校验码连续失败锁定阈值（第 5 次尝试起全拒）
VERIFY_MAX_FAILURES: int = 4
#: 校验码长度（6 位数字）
VERIFY_CODE_LEN: int = 6
#: 一屏聚合短缓存窗口（秒；§6.4 幂等口径）
HOME_CACHE_S: float = 15.0
#: 支持的登录渠道（钉钉微应用 / 微信服务号）
CHANNELS: tuple[str, ...] = ("dingtalk", "wechat")
#: 敏感操作所需角色
SUPERVISOR_ROLES: tuple[str, ...] = ("supervisor", "admin")
#: demo 令牌签名密钥占位（NFR-SEC-04：生产从密钥管理注入，严禁入库硬编码真实凭证）
_DEMO_SIGNING_KEY = b"demo-only-signing-key-NFR-SEC-04-placeholder"


class MobileError(Exception):
    """移动端域业务异常（对齐 04-接口设计 §1.3：40001/40101/40102/40301/40401）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PushProviderError(Exception):
    """推送平台异常（携带平台 errcode，如微信 43101=用户未订阅）。"""

    def __init__(self, message: str, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode


# ---------------------------------------------------------------- 令牌与身份

@dataclass(frozen=True)
class Binding:
    """双端绑定关系（mobile_binding 行语义；user_id ↔ 渠道外部身份）。"""

    user_id: str                                  # 内部统一身份（如 supervisor:1）
    channel: str                                  # dingtalk / wechat
    external_id: str                              # 钉钉 userid / 微信 openid
    display_name: str = ""


@dataclass(frozen=True)
class MobileSession:
    """换发后的会话身份（auth 出参；角色随令牌下发，端上免二次查库）。"""

    user_id: str
    name: str
    roles: tuple[str, ...]
    expires_at: datetime


class BindingStore:
    """绑定表（demo 内存形态；生产 = PG mobile_binding，UNIQUE(channel, external_id)）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, str], Binding] = {}

    def add(self, binding: Binding) -> None:
        """登记绑定（重复覆盖）。"""
        with self._lock:
            self._rows[(binding.channel, binding.external_id)] = binding

    def get(self, channel: str, external_id: str) -> Binding | None:
        """按（渠道, 外部身份）查绑定；未绑定返回 None（调用方负责不泄露存在性）。"""
        with self._lock:
            return self._rows.get((channel, external_id))

    def by_user(self, user_id: str) -> list[Binding]:
        """某用户的全部绑定（推送寻址用）。"""
        with self._lock:
            return [b for b in self._rows.values() if b.user_id == user_id]


def _sign(payload: bytes) -> str:
    """HMAC-SHA256 签名截断（demo；生产 = JWT RS256，见 SR-SC-006 §7）。"""
    return hmac.new(_DEMO_SIGNING_KEY, payload, hashlib.sha256).hexdigest()[:32]


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_session_token(user_id: str, name: str, roles: tuple[str, ...],
                        issued_at: datetime, ttl_s: int = SESSION_TTL_S) -> str:
    """签发会话令牌：``v1.<b64(payload)>.<hmac>``（demo 形态，结构对齐 JWT 三段式）。"""
    payload = json.dumps(
        {"uid": user_id, "name": name, "roles": list(roles),
         "exp": (issued_at + timedelta(seconds=ttl_s)).isoformat()},
        ensure_ascii=False).encode("utf-8")
    body = _b64e(payload)
    return f"v1.{body}.{_sign(body.encode('ascii'))}"


def parse_session_token(token: str, now: datetime) -> dict[str, Any]:
    """校验并解析会话令牌（签名/格式/有效期三查）；任一失败抛 40101。"""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise MobileError(40101, "登录态无效或已过期，请重新登录")
    body = parts[1]
    if not hmac.compare_digest(_sign(body.encode("ascii")), parts[2]):
        raise MobileError(40101, "登录态无效或已过期，请重新登录")
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError) as exc:
        raise MobileError(40101, "登录态无效或已过期，请重新登录") from exc
    if datetime.fromisoformat(str(payload["exp"])) <= now:
        raise MobileError(40101, "登录态已过期，请重新登录")
    return payload


# ---------------------------------------------------------------- 外部身份接缝

class IdentityProvider(Protocol):
    """开放平台身份换发接缝（生产 = 钉钉/微信真实网关客户端）。"""

    def dingtalk_login(self, credential: str) -> str | None:
        """钉钉免登码 → userid；无效返回 None。"""
        ...

    def wechat_code2session(self, code: str) -> str | None:
        """微信 js_code → openid；无效返回 None。"""
        ...


class IdentityStub:
    """身份换发 Fake（demo：凭据 → external_id 静态映射表）。"""

    def __init__(self, *, dingtalk_users: dict[str, str] | None = None,
                 wechat_users: dict[str, str] | None = None) -> None:
        self.dingtalk_users = dict(dingtalk_users or {})
        self.wechat_users = dict(wechat_users or {})

    def dingtalk_login(self, credential: str) -> str | None:
        return self.dingtalk_users.get(credential)

    def wechat_code2session(self, code: str) -> str | None:
        return self.wechat_users.get(code)


# ---------------------------------------------------------------- 下游业务接缝

class HomeProvider(Protocol):
    """一屏聚合下游接缝（生产 = 告警/审批/接管/统计四路内部 API 聚合）。"""

    def fetch(self, route: str) -> Any:
        """按路由取数：alerts / approvals / taking_over / today。"""
        ...


class FakeHomeProvider:
    """一屏聚合 Fake（demo 固定值；fail_routes 注入单路失败观测降级）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_routes: set[str] = set()
        self._values: dict[str, Any] = {
            "alerts": 3,                                     # 告警待办数
            "approvals": 1,                                  # 审批待办数
            "taking_over": 2,                                # 接管中会话数
            "today": {"inquiries": 137, "replies": 129, "gmv": 5210.0},  # 今日 KPI
        }

    def fetch(self, route: str) -> Any:
        self.calls.append(route)
        if route in self.fail_routes:
            raise TimeoutError(f"下游 {route} 超时")
        return self._values[route]


class ApprovalSink(Protocol):
    """会话审批下游接缝（生产 = FR-AG-006 审批状态机）。"""

    def execute(self, approval_id: str, *, actor: str, note: str) -> dict[str, Any]:
        """执行审批通过（敏感操作；仅 BFF 二次校验通过后可达）。"""
        ...

    def reject(self, approval_id: str, *, reason: str, actor: str) -> dict[str, Any]:
        """执行审批驳回。"""
        ...


class FakeApprovalSink:
    """审批下游 Fake（观测面：executed / reject_executed 计数）。"""

    def __init__(self) -> None:
        self.executed = 0
        self.reject_executed = 0
        self.log: list[dict[str, Any]] = []

    def execute(self, approval_id: str, *, actor: str, note: str) -> dict[str, Any]:
        self.executed += 1
        row = {"ok": True, "approval_id": approval_id, "actor": actor, "note": note}
        self.log.append(row)
        return row

    def reject(self, approval_id: str, *, reason: str, actor: str) -> dict[str, Any]:
        self.reject_executed += 1
        row = {"ok": True, "approval_id": approval_id, "rejected": True,
               "reason": reason, "actor": actor}
        self.log.append(row)
        return row


class ConversationSink(Protocol):
    """会话接管下游接缝（生产 = FR-AG-010 会话状态机 ai_managed → human_managed）。"""

    def takeover(self, conversation_uid: str, user_id: str, reason: str) -> dict[str, Any]:
        ...


class FakeConversationSink:
    """接管下游 Fake（观测面：records = (conversation_uid, user_id) 流水）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def takeover(self, conversation_uid: str, user_id: str, reason: str) -> dict[str, Any]:
        self.records.append((conversation_uid, user_id))
        return {"state": "human_managed", "conversation_uid": conversation_uid,
                "taken_over_by": user_id, "reason": reason}


# ---------------------------------------------------------------- 推送通道接缝

class DingTalkWork(Protocol):
    """钉钉工作通知接缝（生产 = 钉钉开放平台 asyncsend_v2）。"""

    def send(self, to_external_id: str, *, title: str, body: str,
             deep_link: str | None = None) -> None:
        ...


class DingTalkWorkFake:
    """钉钉工作通知 Fake（outbox 观测面含 deep_link）。"""

    def __init__(self) -> None:
        self.outbox: list[dict[str, Any]] = []

    def send(self, to_external_id: str, *, title: str, body: str,
             deep_link: str | None = None) -> None:
        self.outbox.append({"to": to_external_id, "title": title, "body": body,
                            "deep_link": deep_link})


class WxSubscribe(Protocol):
    """微信订阅消息接缝（生产 = 微信 subscribeMessage.send，43101=用户未订阅）。"""

    def send(self, to_openid: str, *, title: str, body: str) -> None:
        ...


class WxSubscribeFake:
    """微信订阅消息 Fake（calls 计数；next_errcode 注入平台错误码）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.next_errcode: int | None = None

    def send(self, to_openid: str, *, title: str, body: str) -> None:
        self.calls += 1
        if self.next_errcode is not None:
            raise PushProviderError(f"微信平台错误 errcode={self.next_errcode}",
                                    errcode=self.next_errcode)


class MpTemplate(Protocol):
    """微信服务号模板消息接缝（订阅额度耗尽时的第一级降级）。"""

    def send(self, to_openid: str, *, title: str, body: str) -> None:
        ...


class MpTemplateFake:
    """服务号模板 Fake（fail_next 注入下一轮失败）。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_next = False

    def send(self, to_openid: str, *, title: str, body: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise PushProviderError("服务号模板下发失败")
        self.sent.append(to_openid)


class SmsSender(Protocol):
    """短信兜底接缝（生产 = 阿里云短信；SMTP 等外部依赖同口径 Protocol+Fake）。"""

    def send(self, to_phone: str, *, title: str, body: str) -> None:
        ...


class SmsFake:
    """短信 Fake（demo 以 openid 占位手机号槽位；outbox 观测面）。"""

    def __init__(self) -> None:
        self.outbox: list[dict[str, Any]] = []

    def send(self, to_phone: str, *, title: str, body: str) -> None:
        self.outbox.append({"to": to_phone, "title": title, "body": body})


class PushRouter:
    """推送路由（§6.3）：钉钉主力直发；微信订阅额度制 + mp_template→sms 降级链。

    - 钉钉绑定：工作通知直发（含深链），无额度概念；
    - 微信绑定：额度 >0 → 扣减发订阅消息；额度 =0 → **不外呼**订阅接口（省无用
      调用）+ 留痕引导重订阅 + 模板降级；平台 43101 判未订阅 → 清零额度留痕降级；
      模板也失败 → 短信兜底；
    - 配额与留痕（resubscribe_flags）为 demo 内存形态（生产 = Redis quota + 引导
      订阅任务表）。
    """

    def __init__(
        self,
        dingtalk: DingTalkWork,
        wx: WxSubscribe,
        mp: MpTemplate,
        sms: SmsSender,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.dingtalk = dingtalk
        self.wx = wx
        self.mp = mp
        self.sms = sms
        self._clock = clock or (lambda: datetime.now())
        self._quota: dict[str, int] = {}              # openid → 订阅额度
        self._resubscribe: set[str] = set()           # 需引导重订阅留痕

    # ---------------------------------------------------------------- 配额面

    def quota_of(self, openid: str) -> int:
        """查询订阅余量。"""
        return self._quota.get(openid, 0)

    def register_quota(self, openid: str, *, add: int) -> int:
        """累加订阅额度（用户点击授权订阅一次 +1 的引导口径），返回新余量。"""
        self._quota[openid] = self._quota.get(openid, 0) + max(0, add)
        return self._quota[openid]

    @property
    def resubscribe_flags(self) -> set[str]:
        """需引导重订阅的 openid 集合（观测面）。"""
        return self._resubscribe

    # ---------------------------------------------------------------- 路由

    def push(self, user_id: str, bindings: list[Binding], *, title: str, body: str,
             deep_link: str | None = None) -> list[dict[str, str]]:
        """按绑定关系路由推送，返回逐通道投递结果（channel/status）。"""
        deliveries: list[dict[str, str]] = []
        for binding in bindings:
            if binding.channel == "dingtalk":
                self.dingtalk.send(binding.external_id, title=title, body=body,
                                   deep_link=deep_link)
                deliveries.append({"channel": "dingtalk_work", "status": "sent"})
            elif binding.channel == "wechat":
                deliveries.extend(self._via_wechat(binding.external_id, title=title, body=body))
        return deliveries

    def _via_wechat(self, openid: str, *, title: str, body: str) -> list[dict[str, str]]:
        """微信端投递：额度扣减 → 43101 清零 → 零额度不外呼 → 模板/短信降级链。"""
        quota = self._quota.get(openid, 0)
        if quota > 0:
            try:
                self.wx.send(openid, title=title, body=body)
                self._quota[openid] = quota - 1
                return [{"channel": "wx_subscribe", "status": "sent"}]
            except PushProviderError as exc:
                if exc.errcode == 43101:              # 平台判用户未订阅：额度清零留痕
                    self._quota[openid] = 0
                    self._resubscribe.add(openid)
                return self._via_mp_then_sms(openid, title=title, body=body)
        # 零额度：不外呼订阅接口（额度制省调用），留痕引导重订阅后降级
        self._resubscribe.add(openid)
        return self._via_mp_then_sms(openid, title=title, body=body)

    def _via_mp_then_sms(self, openid: str, *, title: str, body: str) -> list[dict[str, str]]:
        """降级链：服务号模板 → 短信兜底（全链耗尽由调用方告警，FR-AG-007）。"""
        try:
            self.mp.send(openid, title=title, body=body)
            return [{"channel": "mp_template", "status": "fallback"}]
        except PushProviderError:
            self.sms.send(openid, title=title, body=body)
            return [{"channel": "sms", "status": "fallback"}]


# ---------------------------------------------------------------- 二次校验闸门

class VerifyGate:
    """二次校验闸门（§6.2）：challenge 发码 → 校验换 verify_token → GETDEL 单次消费。

    - 令牌三性：120s 有效期、绑定用户（换人无效）、单次消费（重放 False）；
    - 防爆破：连续错 VERIFY_MAX_FAILURES 次后锁定，此后正确码也拒绝（40102）；
    - 校验码经 code_sender 外呼下发（生产 = 短信/钉钉；demo 测试注入捕获）；
    - demo 存储为进程内字典（生产 = Redis：SETEX 码 + SET NX EX 令牌 + GETDEL 消费）。
    """

    def __init__(self, *, code_sender: Callable[[str, str], None],
                 ttl_s: int = VERIFY_TTL_S, max_failures: int = VERIFY_MAX_FAILURES,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._send_code = code_sender
        self._ttl = ttl_s
        self._max_failures = max_failures
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self._challenges: dict[str, tuple[str, datetime]] = {}   # user → (code, expires)
        self._failures: dict[str, int] = {}                       # user → 连续失败数
        self._locked: set[str] = set()                            # 锁定用户
        self._tokens: dict[str, tuple[str, datetime]] = {}        # token → (user, expires)

    # ---------------------------------------------------------------- 挑战与签发

    def challenge(self, user_id: str) -> dict[str, Any]:
        """下发校验码（6 位数字；demo 随机，生产叠加频控）。"""
        now = self._clock()
        code = "".join(str(secrets.randbelow(10)) for _ in range(VERIFY_CODE_LEN))
        with self._lock:
            self._challenges[user_id] = (code, now + timedelta(seconds=self._ttl))
        self._send_code(user_id, code)
        return {"expires_in": self._ttl}

    def issue(self, user_id: str, answer: str) -> str:
        """校验答案换一次性令牌；锁定/超限/答案错误均 40102。"""
        now = self._clock()
        with self._lock:
            if user_id in self._locked or self._failures.get(user_id, 0) >= self._max_failures:
                self._locked.add(user_id)
                raise MobileError(40102, "校验失败次数过多已锁定，请稍后重试或联系管理员")
            entry = self._challenges.get(user_id)
            if (entry is None or now >= entry[1] or not hmac.compare_digest(entry[0], answer)):
                self._failures[user_id] = self._failures.get(user_id, 0) + 1
                raise MobileError(40102, "校验码错误或已过期，请重试")
            self._failures[user_id] = 0                            # 成功清零连击
            token = f"vt-{secrets.token_hex(16)}"
            self._tokens[token] = (user_id, now + timedelta(seconds=self._ttl))
            return token

    def consume(self, token: str, user_id: str) -> bool:
        """单次消费（GETDEL 语义）：令牌有效且绑定该用户 → True 并销毁；否则 False。"""
        now = self._clock()
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return False
            bound_user, expires = entry
            del self._tokens[token]                                # 取出即销毁（防重放）
            return bound_user == user_id and now < expires


# ---------------------------------------------------------------- BFF 主体

class MobileBff:
    """移动端 BFF：鉴权换发 / 一屏聚合 / 审批移动流 / 接管 / 推送登记（§6.1-§6.4）。

    :param roles_of: user_id → 角色元组（None 视为无角色）；敏感操作要求主管角色；
    :param audit: 审计流水（列表注入；生产 = audit_log 哈希链）。
    """

    def __init__(
        self,
        *,
        bindings: BindingStore,
        identity: IdentityProvider,
        roles_of: Callable[[str], tuple[str, ...] | None],
        home: HomeProvider,
        approvals: ApprovalSink,
        conversations: ConversationSink,
        push: PushRouter,
        verify: VerifyGate,
        audit: list[dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bindings = bindings
        self.identity = identity
        self._roles_of = roles_of
        self.home = home
        self.approvals = approvals
        self.conversations = conversations
        self.push = push
        self.verify = verify
        self.audit = audit if audit is not None else []
        self._clock = clock or (lambda: datetime.now())
        self._home_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}  # user → (at, payload)
        self._idem_done: dict[str, dict[str, Any]] = {}                    # idem_key → 结果

    # ---------------------------------------------------------------- 登录与鉴权

    def login(self, channel: str, credential: str) -> dict[str, Any]:
        """双端免登换发：渠道凭据 → external_id → 绑定表 → 会话令牌（§6.1）。"""
        if channel not in CHANNELS:
            raise MobileError(40001, f"非法登录渠道 {channel}（仅 {'/'.join(CHANNELS)}）")
        if channel == "dingtalk":
            external_id = self.identity.dingtalk_login(credential)
        else:
            external_id = self.identity.wechat_code2session(credential)
        binding = self.bindings.get(channel, external_id) if external_id else None
        if binding is None:
            # 枚举防护：未绑定与凭证无效同文案，不泄露账号存在性（NFR-SEC-02）
            raise MobileError(40301, "账号未绑定或凭证无效，请联系管理员确认绑定关系")
        now = self._clock()
        roles = self._roles_of(binding.user_id) or ()
        token = issue_session_token(binding.user_id, binding.display_name, roles, now)
        self._write_audit("mobile_login", binding.user_id,
                          extra={"channel": channel, "external_id": external_id})
        return {"token": token,
                "user": {"user_id": binding.user_id, "name": binding.display_name,
                         "roles": roles, "channel": channel}}

    def auth(self, token: str) -> MobileSession:
        """会话鉴权：验签/有效期 → 会话身份（bogus/过期 40101）。"""
        now = self._clock()
        payload = parse_session_token(token, now)
        return MobileSession(
            user_id=str(payload["uid"]), name=str(payload.get("name", "")),
            roles=tuple(payload.get("roles", ())),
            expires_at=datetime.fromisoformat(str(payload["exp"])))

    # ---------------------------------------------------------------- 一屏聚合

    def home_summary(self, token: str) -> dict[str, Any]:
        """一屏一请求：四卡片聚合 + 15s 短缓存 + 单路降级置 None（§6.4）。"""
        session = self.auth(token)
        now = self._clock()
        cached = self._home_cache.get(session.user_id)
        if cached is not None and (now - cached[0]).total_seconds() < HOME_CACHE_S:
            return cached[1]
        payload: dict[str, Any] = {}
        for key, route in (("todo_alerts", "alerts"), ("todo_approvals", "approvals"),
                           ("taking_over", "taking_over"), ("today", "today")):
            try:
                payload[key] = self.home.fetch(route)
            except Exception:                                 # noqa: BLE001——单路失败不拖垮整屏
                payload[key] = None                           # 端上展示「—」
        self._home_cache[session.user_id] = (now, payload)
        return payload

    # ---------------------------------------------------------------- 二次校验

    def request_challenge(self, token: str) -> dict[str, Any]:
        """发起二次校验挑战（下发校验码）。"""
        session = self.auth(token)
        return self.verify.challenge(session.user_id)

    def issue_verify_token(self, token: str, answer: str) -> dict[str, Any]:
        """校验码换一次性确认令牌（120s、单次消费、绑定用户）。"""
        session = self.auth(token)
        vt = self.verify.issue(session.user_id, answer)
        return {"verify_token": vt, "expires_in": VERIFY_TTL_S}

    # ---------------------------------------------------------------- 审批移动流

    def confirm_approval(self, token: str, approval_id: str, verify_token: str, *,
                         note: str = "", idem_key: str | None = None) -> dict[str, Any]:
        """审批确认（敏感红线）：主管角色 → 令牌单次消费 → 幂等 → 透传下游。

        令牌缺失/伪造/重放一律 40102 且**下游零调用**（绝不透传，SRS §7.4）。
        """
        session = self.auth(token)
        self._require_supervisor(session)
        if not verify_token or not self.verify.consume(verify_token, session.user_id):
            raise MobileError(40102, "二次校验令牌无效或已使用，请重新校验后再确认")
        cached = self._idem_get(idem_key)
        if cached is not None:
            return cached
        result = self.approvals.execute(approval_id, actor=session.user_id, note=note)
        self._write_audit("sensitive_confirm", session.user_id, target=approval_id,
                          extra={"client": "mobile", "note": note, "idem_key": idem_key})
        return self._idem_put(idem_key, result)

    def reject_approval(self, token: str, approval_id: str, reason: str, *,
                        idem_key: str | None = None) -> dict[str, Any]:
        """审批驳回（低风险免二次校验，但幂等：同 Idempotency-Key 仅执行一次）。"""
        session = self.auth(token)
        self._require_supervisor(session)
        cached = self._idem_get(idem_key)
        if cached is not None:
            return cached
        result = self.approvals.reject(approval_id, reason=reason, actor=session.user_id)
        self._write_audit("sensitive_reject", session.user_id, target=approval_id,
                          extra={"client": "mobile", "reason": reason, "idem_key": idem_key})
        return self._idem_put(idem_key, result)

    # ---------------------------------------------------------------- 接管与推送

    def takeover(self, token: str, conversation_uid: str, *, reason: str = "") -> dict[str, Any]:
        """人工介入接管：透传会话状态机（ai_managed → human_managed）。"""
        session = self.auth(token)
        self._require_supervisor(session)
        result = self.conversations.takeover(conversation_uid, session.user_id, reason)
        self._write_audit("mobile_takeover", session.user_id, target=conversation_uid,
                          extra={"reason": reason})
        return result

    def push_register(self, token: str, openid: str, *, add_quota: int = 1) -> dict[str, Any]:
        """订阅额度登记（用户授权一次 +1；引导授权口径）。"""
        session = self.auth(token)
        quota = self.push.register_quota(openid, add=add_quota)
        self._write_audit("push_register", session.user_id,
                          extra={"openid": openid, "add_quota": add_quota})
        return {"openid": openid, "quota": quota}

    # ---------------------------------------------------------------- 内部

    def _require_supervisor(self, session: MobileSession) -> None:
        """敏感操作角色闸门：非主管/管理员一律 40301（学员越权防护）。"""
        if not any(role in SUPERVISOR_ROLES for role in session.roles):
            raise MobileError(40301, "当前角色无权执行该操作，请联系主管处理")

    def _idem_get(self, idem_key: str | None) -> dict[str, Any] | None:
        with threading.Lock():
            return self._idem_done.get(idem_key) if idem_key else None

    def _idem_put(self, idem_key: str | None, result: dict[str, Any]) -> dict[str, Any]:
        if idem_key:
            with threading.Lock():
                self._idem_done[idem_key] = result
        return result

    def _write_audit(self, action_type: str, actor: str, *,
                     target: str = "", extra: dict[str, Any] | None = None) -> None:
        self.audit.append({
            "action_type": action_type, "actor": actor, "target": target,
            "at": self._clock().isoformat(), "extra": dict(extra or {}),
        })


__all__ = [
    "SESSION_TTL_S", "VERIFY_TTL_S", "VERIFY_MAX_FAILURES", "HOME_CACHE_S", "CHANNELS",
    "SUPERVISOR_ROLES", "MobileError", "PushProviderError",
    "Binding", "MobileSession", "BindingStore",
    "issue_session_token", "parse_session_token",
    "IdentityProvider", "IdentityStub",
    "HomeProvider", "FakeHomeProvider", "ApprovalSink", "FakeApprovalSink",
    "ConversationSink", "FakeConversationSink",
    "DingTalkWork", "DingTalkWorkFake", "WxSubscribe", "WxSubscribeFake",
    "MpTemplate", "MpTemplateFake", "SmsSender", "SmsFake",
    "PushRouter", "VerifyGate", "MobileBff",
]
