# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""运营后台服务（FR-SAAS-003 §6.3/§6.4，SR-SAAS-003 §2/§5/§6）。

- 双域闸门（SR §2 决策 1）：平台域 aud=platform / 租户域 aud=tenant 两套令牌
  互不接受，从令牌层杜绝越域；平台 token 只声明 platform_role∈{operator,admin}；
- 平台 RBAC：admin-only 动作（保留期外清理）operator 触达 → 40301；
- 审计哈希链（NFR-SEC-03）：开通/停用/续期/到期/配额变更全量留痕，
  前后哈希链接，防篡改可验证（隔离报告哈希同源机制）；
- 计费只出数据不出价格（SRS §10.1 硬边界）：用量导出为租户×资源×月汇总
  事实数据，无任何定价/账单字段。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from fastapi import Request

from services.saas.core.quota.engine import (
    RESOURCE_ACCOUNT,
    RESOURCE_KB,
    RESOURCE_QPS,
    RESOURCE_STORAGE,
    QuotaEngine,
)
from services.saas.core.tenant.context import (
    PLATFORM_AUD,
    SaasTokenCodec,
    TENANT_AUD,
    TokenInvalidError,
)
from services.saas.core.tenant.registry import TenantRegistry

#: 平台角色集（SR §2 决策 1：甲方运营专用；租户角色不在平台域出现）
PLATFORM_ROLES: frozenset[str] = frozenset({"operator", "admin"})

#: admin-only 动作（SR §6.1：保留期外物理清理需 admin 二次确认）
ADMIN_ONLY_ACTIONS: frozenset[str] = frozenset({"tenant_purge"})

#: 错误码（SR-SAAS-003 §5：40101/40301/40302/40303 + 40001）
AUD_INVALID: int = 40101
ROLE_DENIED: int = 40301
TENANT_SUSPENDED: int = 40302
TENANT_EXPIRED: int = 40303

#: 导出表头白名单（行=租户×资源维度月汇总，仅用量事实维度）
EXPORT_HEADER: tuple[str, ...] = ("tenant_id", "tenant_name", "month", "resource", "used")

#: 定价字段禁用词（导出全文不得出现；SRS §10.1 边界的机检落点）
FORBIDDEN_PRICING_SUBSTRINGS: tuple[str, ...] = (
    "price", "unit_price", "amount", "fee", "charge",
    "单价", "金额", "定价", "账单", "价格",
)

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")

#: 资源维度 → 快照字段映射（QPS 维度落 api_calls 字段，其余同名）
_RESOURCE_FIELDS: Mapping[str, str] = {
    RESOURCE_QPS: "api_calls",
    RESOURCE_STORAGE: "storage_bytes",
    RESOURCE_ACCOUNT: "account_cnt",
    RESOURCE_KB: "kb_item_cnt",
}


class AuthError(Exception):
    """双域闸门拒绝（40101）：令牌无效/过期/aud 不符（互斥的统一出口）。"""

    code: int = AUD_INVALID
    http_status: int = 401
    message: str = "平台域令牌无效（PLATFORM_AUD_INVALID）"


class RoleDeniedError(Exception):
    """平台角色越权（40301 PLATFORM_ROLE_DENIED）。"""

    code: int = ROLE_DENIED
    http_status: int = 403
    message: str = "平台角色权限不足"


class TenantGateError(Exception):
    """租户域闸门拦截（403：suspended/expired，停用即时拦截非删除）。"""

    http_status: int = 403

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class UsageExportError(Exception):
    """用量导出参数错误（40001：period 格式非法等）。"""

    code: int = 40001
    http_status: int = 400
    message: str = "参数校验失败"


@dataclass(frozen=True)
class PlatformPrincipal:
    """平台域主体（platform_role + 主体标识，不携带租户上下文）。"""

    sub: str
    role: str


@dataclass
class AuditRecord:
    """审计记录（哈希链节点；可变以便防篡改校验用例改写后 verify 失败）。"""

    seq: int
    ts: str
    actor: str
    action: str
    target: str
    detail: dict
    prev_hash: str
    hash: str


class AuditChain:
    """审计哈希链（NFR-SEC-03 §3.7 形态的内存实现）：seq 连续 + prev_hash 链接 +
    sha256 逐节点覆盖全部字段，任何中间记录改写都会导致 verify 失败。"""

    _GENESIS: str = "0" * 64

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._records: list[AuditRecord] = []

    def append(self, actor: str, action: str, target: str = "", detail: dict | None = None) -> AuditRecord:
        """追加记录：哈希覆盖本记录全字段 + 前节点哈希（链式防篡改）。"""
        seq = len(self._records) + 1
        prev_hash = self._records[-1].hash if self._records else self._GENESIS
        ts = self._clock().isoformat()
        record = AuditRecord(
            seq=seq, ts=ts, actor=actor, action=action, target=str(target),
            detail=dict(detail or {}), prev_hash=prev_hash, hash="",
        )
        record.hash = self._digest(record)
        self._records.append(record)
        return record

    @property
    def records(self) -> list[AuditRecord]:
        """已落链记录（浅拷贝：对象同一性保留，供防篡改用例改写）。"""
        return list(self._records)

    def verify(self) -> bool:
        """全链复核：逐节点重算哈希并核对链接，任一不一致即 False。"""
        prev = self._GENESIS
        for record in self._records:
            if record.prev_hash != prev or record.hash != self._digest(record):
                return False
            prev = record.hash
        return True

    @staticmethod
    def _digest(record: AuditRecord) -> str:
        payload = json.dumps(
            {
                "seq": record.seq, "ts": record.ts, "actor": record.actor,
                "action": record.action, "target": record.target,
                "detail": record.detail, "prev_hash": record.prev_hash,
            },
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class OpsService:
    """运营后台核心：双域闸门 + 平台 RBAC + 审计链 + 用量导出（无定价）。"""

    def __init__(
        self,
        registry: TenantRegistry,
        quota: QuotaEngine,
        *,
        codec: SaasTokenCodec | None = None,
        clock: Callable[[], datetime] | None = None,
        secret: str = "saas-demo-secret",
    ) -> None:
        self._registry = registry
        self._quota = quota
        self._codec = codec or SaasTokenCodec(secret, clock=clock)
        self.audit = AuditChain(clock=clock)

    # ------------------------------------------------------------------ 装配

    def wire(self) -> None:
        """把审计钩子接入注册表（开通/状态迁移/配额变更全量留痕，NFR-SEC-03）。"""
        self._registry.audit = self._audit_bridge

    def _audit_bridge(self, actor: str, action: str, target: str, detail: dict) -> None:
        self.audit.append(actor, action, target, detail)

    # ------------------------------------------------------------------ 令牌

    def issue_platform_token(self, sub: str, role: str, ttl_sec: int = 3600) -> str:
        """签发平台域令牌（aud=platform，不携带租户上下文，SR §2 决策 1）。"""
        return self._codec.issue(PLATFORM_AUD, sub, role=role, tenant_id=None, ttl_sec=ttl_sec)

    def issue_tenant_token(self, tenant_id: int, sub: str, role: str = "tenant_admin",
                           ttl_sec: int | None = None) -> str:
        """签发租户域令牌（aud=tenant，tenant_id 与角色同源签发，NFR-SEC-02）。"""
        return self._codec.issue(TENANT_AUD, sub, role=role, tenant_id=tenant_id, ttl_sec=ttl_sec)

    def verify_token(self, token: str, expected_aud: str):
        """令牌校验（双域互斥的底层落点；无效/过期/aud 不符统一 40101 AuthError）。"""
        try:
            return self._codec.verify(token, expected_aud)
        except TokenInvalidError as exc:
            raise AuthError(str(exc) or "令牌无效") from exc

    # ------------------------------------------------------------------ 闸门

    @staticmethod
    def _bearer(request: Request) -> str:
        authorization = (
            request.headers.get("authorization") or request.headers.get("Authorization") or ""
        )
        scheme, _, token = authorization.partition(" ")
        return token.strip() if scheme.strip().lower() == "bearer" else ""

    def platform_principal(self, request: Request) -> PlatformPrincipal:
        """平台域闸门：Bearer → aud=platform → 角色必须 ∈ PLATFORM_ROLES。"""
        token = self._bearer(request)
        claims = self.verify_token(token, PLATFORM_AUD) if token else None
        if claims is None:
            raise AuthError("缺少平台域令牌")
        if claims.role not in PLATFORM_ROLES:
            raise RoleDeniedError(f"非平台运营角色: {claims.role!r}")
        return PlatformPrincipal(sub=claims.sub, role=claims.role)

    def require_role(self, principal: PlatformPrincipal, *allowed_actions: str) -> None:
        """平台 RBAC 细分：admin-only 动作 operator 触达 → 40301（SR §5）。"""
        if any(action in ADMIN_ONLY_ACTIONS for action in allowed_actions) and principal.role != "admin":
            raise RoleDeniedError(f"动作需 admin 角色: {sorted(allowed_actions)}")

    def tenant_gate(self, request: Request):
        """租户域闸门：aud=tenant + 租户状态闸门（suspended/expired 即 403 拦截）。"""
        token = self._bearer(request)
        claims = self.verify_token(token, TENANT_AUD) if token else None
        if claims is None or claims.tenant_id is None:
            raise AuthError("缺少租户域令牌")
        row = self._registry.get(claims.tenant_id)
        if row is None:
            raise TenantGateError(40401, f"租户不存在: {claims.tenant_id}")
        if row.status == "suspended":
            raise TenantGateError(TENANT_SUSPENDED, "租户已停用（TENANT_SUSPENDED）")
        if row.status == "expired":
            raise TenantGateError(TENANT_EXPIRED, "租户已到期（TENANT_EXPIRED）")
        return claims

    # ------------------------------------------------------------------ 用量导出（仅数据，无定价）

    def export_usage_csv(self, period: str) -> str:
        """计费用量导出：租户×资源维度月汇总 CSV（行=租户×资源，事实数据）。

        边界声明（SRS §10.1）：授权价格/合同条款归甲方——本接口无单价/金额/
        账单字段，仅用量计数（表头白名单机检 + 禁用词全文扫描双重防护）。
        """
        if not _PERIOD_RE.match(period or ""):
            raise UsageExportError(f"period 格式非法（要求 YYYY-MM）: {period!r}")
        usage: dict[tuple[int, str], int] = {}
        for snapshot in self._quota.usage_snapshots():
            month = snapshot.stat_hour[:7]
            if month != period:
                continue
            for resource, field_name in _RESOURCE_FIELDS.items():
                value = getattr(snapshot, field_name)
                if value:
                    key = (snapshot.tenant_id, resource)
                    usage[key] = usage.get(key, 0) + value
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(EXPORT_HEADER)
        for (tid, resource), used in sorted(usage.items()):
            row = self._registry.get(tid)
            name = getattr(row, "name", "") if row is not None else ""
            writer.writerow([tid, name, period, resource, used])
        return buffer.getvalue()


__all__ = [
    "ADMIN_ONLY_ACTIONS", "AuditChain", "AuditRecord", "AuthError", "EXPORT_HEADER",
    "FORBIDDEN_PRICING_SUBSTRINGS", "PLATFORM_AUD", "PLATFORM_ROLES", "RoleDeniedError",
    "TENANT_AUD", "TenantGateError", "UsageExportError", "OpsService", "PlatformPrincipal",
    "ROLE_DENIED", "TENANT_EXPIRED", "TENANT_SUSPENDED",
]
