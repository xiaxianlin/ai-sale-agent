"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-06 数据不出域——出域白名单引擎（平台基座实现）。

依据：docs/07-实现方案/NFR/SR-NFR-SEC-06-数据不出域.md、docs/02-设计文档/06-安全与合规设计.md §6
- §2/§6.1 出域目的地注册表（host/port/用途/负责人）是唯一权威：代码层校验与网络层
  防火墙（deploy/firewall/egress.rules）双视图同源（决策 D1/D2）；
- fail-closed：未注册目的地、IP 直连绕过域名、端口/协议不匹配、OI-09 未获甲方确认
  （disabled/pending_reeval）一律默认拒绝，宁拒勿漏（SR §6.6）；
- §4 egress_call_log 的进程内形态：每次出域尝试（放行与拦截均）留下
  「内容摘要 SHA-256 + 目的地 + 时间」调用记录，并提供审计钩子接口对接 NFR-SEC-03 审计链。

平台基座范围：注册表 + 判定 + 调用留痕（内存账本 + 钩子）；PG 持久化（egress_dest /
egress_call_log 表）与 OI-09 评估单状态机（assessment.py）随服务层落地，本文件仅保持
与表字段同名同义的口径（code/host/port/purpose/owner/status）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

# --------------------------------------------------------------------------- 错误码（SR §5）

EGRESS_UNREGISTERED = "EGRESS-1001"
"""白名单外目标：未注册域名 / IP 直连 / 端口或协议不匹配 / URL 不可解析（fail-closed）。"""

EGRESS_PURPOSE_OVERFLOW = "EGRESS-1002"
"""用途（数据类型）越界：调用声明的用途不在目的地注册的用途范围内。"""

EGRESS_NOT_CONFIRMED = "EGRESS-1003"
"""目的地已注册但未获甲方确认（OI-09）或处于待重评状态，唯一放行途径是评估单确认。"""

DestStatus = Literal["enabled", "disabled", "pending_reeval"]
"""目的地状态：仅 enabled 放行；pending_reeval 为变更触发的待重评（SR §6.4）。"""

CallVerdict = Literal["allowed", "blocked"]
"""出域尝试判定：allowed=放行；blocked=拦截（含白名单外/未确认/用途越界）。"""

_DEFAULT_SCHEME_PORTS: dict[str, int] = {"https": 443, "http": 80}
"""URL 未显式携带端口时按 scheme 取默认端口（注册端口须与其一致才放行）。"""


# --------------------------------------------------------------------------- 异常体系


class EgressError(Exception):
    """出域治理基础异常。"""


class EgressRegistryError(EgressError):
    """注册表维护错误（编码/主机端口冲突、未知编码等）。"""


class EgressViolation(EgressError):
    """出域违规（fail-closed 拒绝）：携带错误码、中文原因与目标 URL。

    供 embedding/LLM/SMTP 等出域客户端在发起连接前经 assert_egress 强制触发；
    拦截发生前调用已留痕（verdict=blocked），无「先发后审」路径。
    """

    def __init__(self, code: str, detail: str, url: str = "") -> None:
        message = f"{code} {detail}" if detail else code
        if url:
            message = f"{message}（url={url}）"
        super().__init__(message)
        self.code: str = code
        self.detail: str = detail
        self.url: str = url


# --------------------------------------------------------------------------- 目的地注册模型


class EgressDestination(BaseModel):
    """出域目的地注册项（SR §4 egress_dest 表的进程内形态，字段同名同义）。

    host/port 为代码层校验目标（与防火墙 CIDR:端口 规则同源）；purposes 声明该目的地
    允许的调用用途编码（与 OI-09 评估单行项目同源）；owner 为负责人；status 三态——
    仅 enabled 放行，甲方确认（OI-09 confirmed）是置 enabled 的唯一途径。
    """

    model_config = ConfigDict(frozen=True)

    code: str
    """目的地编码（全局唯一）：deepseek_api/aliyun_oss/dingtalk/wechat/smtp/proxy_provider。"""

    host: str
    """注册主机名（裸域名，小写；禁止 IP 字面量——防 IP 直连绕过域名白名单）。"""

    port: int = 443
    """放行端口（出域目标仅 TLS 端口：443/465，SR §8）。"""

    schemes: tuple[str, ...] = ("https",)
    """允许的 URL scheme（http/https 子集）。"""

    purposes: tuple[str, ...]
    """允许的调用用途编码（如 llm_chat/reply_generate/mail_send/oss_backup）。"""

    purpose_desc: str = ""
    """用途中文说明（评估单行项目同源，供审计报表展示）。"""

    owner: str
    """负责人（团队或角色，变更时触发 OI-09 重评的联络对象）。"""

    status: DestStatus = "enabled"
    data_types: tuple[str, ...] = ()
    """允许出域数据类型（SR §2 治理矩阵；越界即拒，变更触发重评）。"""

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        code = value.strip()
        if not code:
            raise ValueError("code 不能为空")
        return code

    @field_validator("host")
    @classmethod
    def _normalize_host(cls, value: str) -> str:
        host = value.strip().lower()
        if not host:
            raise ValueError("host 不能为空")
        if "://" in value or any(ch in host for ch in ":/?#@\\"):
            raise ValueError(f"host 仅允许裸主机名（不含 scheme/端口/路径等成分）：{value!r}")
        if _is_ip_literal(host):
            raise ValueError(f"禁止注册 IP 直连主机（出域目标必须为域名，防绕过域名白名单）：{value!r}")
        return host

    @field_validator("port")
    @classmethod
    def _check_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError(f"port 必须在 1~65535 内：{value}")
        return value

    @field_validator("schemes")
    @classmethod
    def _check_schemes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        schemes = tuple(s.strip().lower() for s in value)
        if not schemes or any(s not in _DEFAULT_SCHEME_PORTS for s in schemes):
            raise ValueError(f"schemes 仅允许 http/https 非空子集：{list(value)}")
        return schemes

    @field_validator("purposes")
    @classmethod
    def _check_purposes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        purposes = tuple(p.strip() for p in value)
        if not purposes or any(not p for p in purposes):
            raise ValueError("purposes 必须至少声明一个非空用途编码")
        return purposes


@dataclass(frozen=True)
class EgressCheck:
    """一次出域判定的结果：ok + 错误码 + 中文说明 + 命中的注册项（未命中为 None）。"""

    ok: bool
    code: str = ""
    detail: str = ""
    dest: EgressDestination | None = None


class EgressCallRecord(BaseModel):
    """出域调用留痕（SR §4 egress_call_log 的进程内形态）。

    放行与拦截尝试均落一条：内容摘要 hash（content_sha256）+ 目的地 + 时间（created_at）
    为最小审计三元组；blocked 时 block_reason 携带「错误码 + 中文说明」。
    """

    model_config = ConfigDict(frozen=True)

    seq: int
    """账本内单调序号（进程内递增；PG 落地为自增 id）。"""

    dest_code: str
    """目的地编码（URL 未命中注册表时为空串）。"""

    host: str
    port: int
    scheme: str = ""
    purpose: str
    caller: str = ""
    trace_id: str = ""
    content_sha256: str = ""
    """出域内容摘要（SHA-256 hex，与快照关联、可去重；未携带内容时为空串）。"""

    bytes_out: int = 0
    verdict: CallVerdict
    block_reason: str = ""
    created_at: datetime


class EgressAuditHook(Protocol):
    """出域调用审计钩子接口：每次判定（放行/拦截）后回调一次，供 NFR-SEC-03 审计链接入。"""

    def on_egress_call(self, record: EgressCallRecord) -> None: ...


class _NoopAuditHook:
    """默认空钩子（未接入审计链时静默）。"""

    def on_egress_call(self, record: EgressCallRecord) -> None:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_digest(content: str | bytes) -> str:
    """出域内容摘要（SHA-256 hex）：调用留痕与快照关联、去重的统一口径（SR §4）。"""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def _is_ip_literal(host: str) -> bool:
    """判定主机名是否为 IP 字面量（v4/v6）——出域目标禁止 IP 直连绕过域名白名单。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _split_url(url: str) -> tuple[str, str, int]:
    """解析出域 URL → (scheme, host, port)；不可解析/非法端口即抛 ValueError（fail-closed）。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_SCHEME_PORTS:
        raise ValueError(f"仅允许 http/https 出域（scheme={scheme!r}）")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("无法从 URL 解析出主机名")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"URL 端口非法：{exc}") from exc
    if port is None:
        port = _DEFAULT_SCHEME_PORTS[scheme]
    return scheme, host, port


# --------------------------------------------------------------------------- 出域白名单引擎


class EgressGuard:
    """出域白名单引擎（D1/D2 的代码层视图）：注册表唯一权威 + fail-closed 判定 + 调用留痕。

    - ``is_allowed(url)``：仅判定目标是否放行（布尔形态，供配置自检等场景）；
    - ``assert_egress(url, purpose)``：供 embedding/LLM/SMTP 客户端在发起连接前强制调用，
      通过返回留痕记录，不通过抛 :class:`EgressViolation`（拦截前已留痕）；
    - ``iter_calls()``：出域调用账本快照（供月度审计报表 report.py 聚合）。
    """

    def __init__(
        self,
        *,
        audit_hook: EgressAuditHook | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._dests: dict[str, EgressDestination] = {}
        self._by_target: dict[tuple[str, int], EgressDestination] = {}
        self._records: list[EgressCallRecord] = []
        self._lock = threading.Lock()
        self._audit_hook: EgressAuditHook = audit_hook or _NoopAuditHook()
        self._clock: Callable[[], datetime] = clock or _utcnow
        self._seq = 0

    # --------------------------------------------------------------- 注册表维护

    def register(self, dest: EgressDestination) -> None:
        """注册出域目的地（编码与 host:port 均唯一；变更走服务层评估重评流程）。"""
        with self._lock:
            if dest.code in self._dests:
                raise EgressRegistryError(f"目的地编码已注册：{dest.code}")
            key = (dest.host, dest.port)
            if key in self._by_target:
                raise EgressRegistryError(
                    f"主机/端口已被 {self._by_target[key].code} 注册：{dest.host}:{dest.port}"
                )
            self._dests[dest.code] = dest
            self._by_target[key] = dest

    def register_all(self, dests: Iterable[EgressDestination]) -> None:
        """批量注册（顺序应用同一校验，任一冲突即抛错并保持已注册前缀不变）。"""
        for dest in dests:
            self.register(dest)

    def set_status(self, code: str, status: DestStatus) -> None:
        """变更目的地状态（评估确认置 enabled / 变更触发置 pending_reeval，SR §6.4）。"""
        with self._lock:
            dest = self._dests.get(code)
            if dest is None:
                raise EgressRegistryError(f"未知目的地编码：{code}")
            updated = dest.model_copy(update={"status": status})
            self._dests[code] = updated
            self._by_target[(dest.host, dest.port)] = updated

    def set_audit_hook(self, hook: EgressAuditHook) -> None:
        """替换审计钩子（服务层接 NFR-SEC-03 哈希链写入器时调用）。"""
        self._audit_hook = hook

    @property
    def destinations(self) -> tuple[EgressDestination, ...]:
        with self._lock:
            return tuple(self._dests.values())

    # --------------------------------------------------------------- 判定（fail-closed）

    def check(self, url: str, purpose: str | None = None) -> EgressCheck:
        """判定出域目标是否放行：purpose 为 None 时跳过用途校验（is_allowed 内部使用）。"""
        if not isinstance(url, str) or not url.strip():
            return EgressCheck(False, EGRESS_UNREGISTERED, "白名单外目标（URL 为空）", None)
        try:
            scheme, host, port = _split_url(url)
        except ValueError as exc:
            return EgressCheck(False, EGRESS_UNREGISTERED, f"白名单外目标（{exc}）", None)
        if _is_ip_literal(host):
            return EgressCheck(
                False,
                EGRESS_UNREGISTERED,
                "白名单外目标（禁止 IP 直连绕过域名，必须使用注册域名）",
                None,
            )
        with self._lock:
            dest = self._by_target.get((host, port))
            registry_empty = not self._by_target
        if dest is None:
            if registry_empty:
                detail = "白名单外目标（注册表为空，fail-closed 默认拒绝）"
            else:
                detail = f"白名单外目标（{host}:{port} 未注册）"
            return EgressCheck(False, EGRESS_UNREGISTERED, detail, None)
        if scheme not in dest.schemes:
            return EgressCheck(
                False,
                EGRESS_UNREGISTERED,
                f"白名单外目标（{dest.host} 仅允许 {list(dest.schemes)}，收到 {scheme!r}）",
                dest,
            )
        if dest.status != "enabled":
            return EgressCheck(
                False,
                EGRESS_NOT_CONFIRMED,
                f"目的地 {dest.code} 状态为 {dest.status}（OI-09 未获甲方确认或待重评）",
                dest,
            )
        if purpose is not None:
            if not purpose.strip():
                return EgressCheck(False, EGRESS_PURPOSE_OVERFLOW, "未声明出域用途（purpose 为空）", dest)
            if purpose not in dest.purposes:
                return EgressCheck(
                    False,
                    EGRESS_PURPOSE_OVERFLOW,
                    f"用途越界：{purpose!r} 不在目的地 {dest.code} 声明用途 {list(dest.purposes)} 内",
                    dest,
                )
        return EgressCheck(True, dest=dest)

    def is_allowed(self, url: str) -> bool:
        """目标是否放行（未注册目的地默认拒绝，注册表为空时全部拒绝）。"""
        return self.check(url).ok

    def assert_egress(
        self,
        url: str,
        purpose: str,
        *,
        content: str | bytes | None = None,
        caller: str = "",
        trace_id: str = "",
    ) -> EgressCallRecord:
        """出域前强制断言（SR §6.2 步骤②）：通过返回留痕记录，不通过抛 EgressViolation。

        content 为本次出域载荷（用于内容摘要 hash 留痕）；拦截尝试同样落账
        （verdict=blocked + block_reason），供违规检测与月度报表统计。
        """
        result = self.check(url, purpose)
        record = self.record_call(
            url=url,
            purpose=purpose,
            verdict="allowed" if result.ok else "blocked",
            code=result.code,
            detail=result.detail,
            content=content,
            caller=caller,
            trace_id=trace_id,
        )
        if not result.ok:
            raise EgressViolation(result.code, result.detail, url)
        return record

    # --------------------------------------------------------------- 调用留痕（审计钩子）

    def record_call(
        self,
        *,
        url: str,
        purpose: str,
        verdict: CallVerdict,
        code: str = "",
        detail: str = "",
        content: str | bytes | None = None,
        caller: str = "",
        trace_id: str = "",
    ) -> EgressCallRecord:
        """追加一条出域调用留痕（assert_egress 的底层通道；网关内容违规也经此落账）。"""
        try:
            scheme, host, port = _split_url(url)
        except ValueError:
            scheme, host, port = "", "", 0
        with self._lock:
            dest = self._by_target.get((host, port)) if host else None
        record = EgressCallRecord(
            seq=-1,  # 占位，锁内回填
            dest_code=dest.code if dest is not None else "",
            host=host,
            port=port,
            scheme=scheme,
            purpose=purpose,
            caller=caller,
            trace_id=trace_id,
            content_sha256=content_digest(content) if content is not None else "",
            bytes_out=len(content.encode("utf-8")) if isinstance(content, str) else len(content or b""),
            verdict=verdict,
            block_reason=f"{code} {detail}" if code else "",
            created_at=self._clock(),
        )
        with self._lock:
            self._seq += 1
            record = record.model_copy(update={"seq": self._seq})
            self._records.append(record)
        # 钩子在锁外回调：钩子内允许反向查询注册表/账本，避免重入死锁
        self._audit_hook.on_egress_call(record)
        return record

    def iter_calls(self) -> tuple[EgressCallRecord, ...]:
        """出域调用账本快照（月度审计报表的数据源；返回副本防外部篡改）。"""
        with self._lock:
            return tuple(self._records)
