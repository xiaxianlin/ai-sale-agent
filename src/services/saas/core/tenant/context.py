# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户上下文（FR-SAAS-001 §6.1，SR-SAAS-001 §5/§6.1）——请求级 ContextVar 唯一定义点。

- 解析优先级：JWT 声明（tenant_id 与角色同源签发）→ 白标域名兜底 →
  多租户开关关闭时回落默认租户 1（P0-P4 单租户向前兼容，SR §7）；
- fail-closed：缺上下文 get_tenant_id() 直接抛错，绝不落到默认租户造成串数据；
- ContextVar 按协程隔离，请求结束在 finally 中复位（防协程复用串租户）；
- 租户边界只取自凭证/域名：请求头与业务参数中的 tenant_id 一律不参与解析
  （NFR-SEC-07 §6.3 上下文逃逸防护的 API 形态落点）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid as _uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from services.saas.core.quota.engine import QuotaExceededError

#: 默认租户（甲方本体）：P0-P4 全部业务行 tenant_id=1 的显式化（SR-SAAS-001 §6.5）
DEFAULT_TENANT_ID: int = 1

#: 双域令牌 audience（SR-SAAS-003 §2 决策 1：平台域/租户域两套 JWT，互不接受）
PLATFORM_AUD: str = "platform"
TENANT_AUD: str = "tenant"

#: 令牌签名密钥环境变量（CR P0-5 修复：参数 → env → 启动失败，无缺省硬编码；
#: NFR-SEC-04 生产经密钥管理下发）
SAAS_TOKEN_SECRET_ENV: str = "KB_SAAS_TOKEN_SECRET"


class SaasConfigError(Exception):
    """配置缺失（fail-fast）：令牌签名密钥未注入（CR P0-5 修复，NFR-SEC-04；对齐
    vault.VaultConfigError 模式——拒绝无密钥运行，绝不回落到源码硬编码缺省值）。"""


class TenantContextError(Exception):
    """租户上下文缺失（40101/TENANT_UNRESOLVED）：业务数据访问被拒绝（fail-closed）。"""

    code: int = 40101
    http_status: int = 401
    message: str = "无法识别租户：缺少有效凭证或未绑定域名"


class TokenInvalidError(TenantContextError):
    """令牌无效/过期/aud 不符（40101）：不区分具体原因，防凭证探测（对齐 FR-KB-031 口径）。"""

    message: str = "令牌无效或已过期"


# --------------------------------------------------------------------------- 令牌编解码（demo HMAC 形态）


@dataclass(frozen=True)
class SaasClaims:
    """令牌声明（JWT 载荷子集）：双域 aud + 主体 + 角色 + 租户边界。"""

    aud: str
    sub: str
    role: str = ""
    tenant_id: int | None = None
    exp: int | None = None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def resolve_saas_secret(secret: str | None) -> str:
    """令牌签名密钥解析（CR P0-5 修复）：显式参数 → 环境变量 KB_SAAS_TOKEN_SECRET；
    均缺抛 SaasConfigError（中文 fail-fast，拒绝无密钥签发/验签）。"""
    value = secret if secret is not None else os.environ.get(SAAS_TOKEN_SECRET_ENV)
    if not value:
        raise SaasConfigError(
            f"缺少 SaaS 令牌签名密钥：请经构造参数 secret 注入或设置环境变量 "
            f"{SAAS_TOKEN_SECRET_ENV}（NFR-SEC-04；生产经密钥管理下发，严禁缺省硬编码）"
        )
    return value


class SaasTokenCodec:
    """HMAC-SHA256 签名令牌（demo 形态，契约与 JWT 一致：header.payload.signature 的
    payload/signature 语义按 base64url(json)+hmac 简化为 sig.payload——校验强度等价，
    生产替换为 python-jose/pyjwt 签发时不改消费方接口）。"""

    def __init__(
        self,
        secret: str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # CR P0-5 修复：无缺省密钥——参数 → env → 构造即 SaasConfigError（fail-fast）
        self._secret = resolve_saas_secret(secret).encode("utf-8")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(
        self,
        aud: str,
        sub: str,
        role: str = "",
        tenant_id: int | None = None,
        ttl_sec: int | None = 3600,
    ) -> str:
        """签发令牌：payload 含 aud/sub/role/tenant_id/exp（exp 由时钟 + ttl 推得）。"""
        payload: dict[str, object] = {"aud": aud, "sub": sub, "role": role}
        if tenant_id is not None:
            payload["tenant_id"] = int(tenant_id)
        if ttl_sec is not None:
            exp_at = self._clock() + timedelta(seconds=ttl_sec)
            payload["exp"] = int(exp_at.timestamp())
        body = _b64url_encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        sig = _b64url_encode(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        return f"{sig}.{body}"

    def verify(self, token: str, expected_aud: str) -> SaasClaims:
        """校验签名 → 有效期 → aud（双域互斥落点）；任一失败抛 TokenInvalidError。"""
        sig_part, _, body_part = token.partition(".")
        if not sig_part or not body_part:
            raise TokenInvalidError("令牌格式非法")
        expected_sig = hmac.new(
            self._secret, body_part.encode("ascii"), hashlib.sha256
        ).digest()
        try:
            actual_sig = _b64url_decode(sig_part)
        except Exception as exc:  # base64 非法即无效令牌
            raise TokenInvalidError("令牌编码非法") from exc
        if not hmac.compare_digest(expected_sig, actual_sig):
            raise TokenInvalidError("令牌签名校验失败")
        try:
            payload = json.loads(_b64url_decode(body_part))
        except Exception as exc:
            raise TokenInvalidError("令牌载荷非法") from exc
        exp = payload.get("exp")
        if exp is not None and datetime.fromtimestamp(int(exp), tz=timezone.utc) < self._clock():
            raise TokenInvalidError("令牌已过期")
        if payload.get("aud") != expected_aud:
            raise TokenInvalidError(f"令牌域不符：要求 aud={expected_aud}")
        tenant_id = payload.get("tenant_id")
        return SaasClaims(
            aud=str(payload["aud"]),
            sub=str(payload.get("sub", "")),
            role=str(payload.get("role", "")),
            tenant_id=int(tenant_id) if tenant_id is not None else None,
            exp=int(exp) if exp is not None else None,
        )


# --------------------------------------------------------------------------- ContextVar（唯一定义点）

_tenant_id: ContextVar[int | None] = ContextVar("saas_tenant_id", default=None)

#: 缺上下文案头（fail-closed，与「无法识别租户」401 语义区分：这是业务侧拒绝）
_CONTEXT_MISSING_MESSAGE: str = "租户上下文缺失：业务数据访问被拒绝"


def get_tenant_id() -> int:
    """读取当前租户。缺上下文直接抛错（fail-closed，SR §6.1）。"""
    tid = _tenant_id.get()
    if tid is None:
        raise TenantContextError(_CONTEXT_MISSING_MESSAGE)
    return tid


def peek_tenant_id() -> int | None:
    """只读窥探（测试/观测用）：缺上下文返回 None 不抛错。"""
    return _tenant_id.get()


def bind_tenant(tid: int) -> Token[int | None]:
    """绑定租户（仅中间件与后台任务入口允许调用；业务代码禁止调用，防显式传参绕过）。"""
    return _tenant_id.set(int(tid))


@contextmanager
def tenant_context(tid: int) -> Iterator[int]:
    """租户上下文管理器：进入注入、退出还原（后台任务与单元测试推荐用法）。"""
    token = bind_tenant(tid)
    try:
        yield tid
    finally:
        _tenant_id.reset(token)


def bearer_token_of(request: Request) -> str:
    """从请求提取 Bearer 令牌（缺失/格式错返回空串，由校验层统一 401，防探测）。"""
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return ""
    return token.strip()


def _error_response(
    status: int,
    code: int,
    message: str,
    *,
    data: object = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """中间件层统一错误信封（04-接口设计 §1.3 四键齐全）。"""
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "data": data, "trace_id": _uuid.uuid4().hex},
        headers=headers,
    )


class TenantContextMiddleware(BaseHTTPMiddleware):
    """租户上下文中间件（SR-SAAS-001 §6.1，挂全部服务，置于鉴权后、路由前）。

    平台域路径（/api/v1/saas/platform）在此豁免——平台域鉴权由路由依赖完成；
    租户域路径强制解析并注入租户：JWT → 域名 → （多租户关闭时）默认租户 1；
    伪造头/参数永不参与解析；suspended/expired 一律 403（闸门，非删除）；
    计费路径做 QPS 实时扣减，超限 429 + Retry-After；finally 复位上下文。
    """

    def __init__(
        self,
        app: object,
        registry: object,
        codec: SaasTokenCodec,
        *,
        clock: Callable[[], datetime] | None = None,
        multi_tenancy_enabled: bool = True,
        host_resolver: Callable[[str], int | None] | None = None,
        quota_engine: object | None = None,
        billed_paths: tuple[str, ...] = (),
        platform_prefix: str = "/api/v1/saas/platform",
        blocked_audit: Callable[[int, str], None] | None = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type] - Starlette BaseHTTPMiddleware 签名
        self._registry = registry
        self._codec = codec
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._multi_tenancy = multi_tenancy_enabled
        self._host_resolver = host_resolver
        self._quota = quota_engine
        self._billed_paths = frozenset(billed_paths)
        self._platform_prefix = platform_prefix
        self._blocked_audit = blocked_audit  # 拦截也留痕（NFR-SEC-03 联动，SR §6.1）

    async def dispatch(self, request: Request, call_next):  # noqa: C901 - 解析/闸门/复位单一入口
        """请求入口：解析 → 状态闸门 → （计费路径）配额闸门 → 业务 → 复位。"""
        # 平台域路径豁免租户解析（双域闸门的平台侧由路由依赖承担）
        if request.url.path.startswith(self._platform_prefix) or request.url.path == "/healthz":
            return await call_next(request)

        tid: int | None = None
        # 优先级 1：JWT 声明（伪造头/参数不参与——解析只看凭证）
        token = bearer_token_of(request)
        if token:
            try:
                claims = self._codec.verify(token, TENANT_AUD)
            except TokenInvalidError as exc:  # 令牌无效/过期/aud 不符 → 401 信封
                return _error_response(401, 40101, str(exc))
            tid = claims.tenant_id
        # 优先级 2：白标域名兜底（FR-SAAS-006 host → tenant 映射）
        if tid is None:
            host = (request.headers.get("host", "") or "").split(":")[0]
            if self._host_resolver is not None and host:
                tid = self._host_resolver(host)
        # 优先级 3：多租户开关关闭 → 默认租户 1（提前验证链路，SR §7）
        if tid is None and not self._multi_tenancy:
            tid = DEFAULT_TENANT_ID
        if tid is None:
            err = TenantContextError()
            return _error_response(err.http_status, err.code, err.message)

        tenant = self._registry.get(tid)
        if tenant is None or getattr(tenant, "status", "active") != "active":
            status = getattr(tenant, "status", "NA")
            return _error_response(403, 40302, f"租户不可用(status={status})：TENANT_INACTIVE")

        token_var = bind_tenant(tid)
        try:
            # 配额闸门：仅计费路径做 QPS 实时扣减（SR §6.1 双速路径的高频侧）
            if self._quota is not None and request.url.path in self._billed_paths:
                try:
                    self._quota.require(tid, "api_qps")
                except QuotaExceededError as exc:
                    if self._blocked_audit is not None:
                        self._blocked_audit(tid, request.url.path)
                    return _error_response(
                        429,
                        42901,
                        exc.message,
                        data={"resource": exc.resource, "tenant_id": tid},
                        headers={"Retry-After": str(max(1, exc.retry_after or 1))},
                    )
            return await call_next(request)
        finally:
            _tenant_id.reset(token_var)  # 请求结束即复位，防协程复用串租户
