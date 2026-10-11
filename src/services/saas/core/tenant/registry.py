# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户注册表与状态机（FR-SAAS-001/003 §6.1，SR-SAAS-003 §6.1）。

- 状态机 active→suspended→expired：合法边恰 5 条（LEGAL_TRANSITIONS），
  非法迁移一律 40901 TENANT_STATE_CONFLICT；
- 停用=资源冻结而非删除：suspended 只记录冻结字段并置闸门，租户行/配额覆盖/
  业务数据全量保留；保留期内续期完整恢复（SR §2 决策 2）；
- 并发控制：乐观快照 + 锁内复核（等价 FOR UPDATE 行锁串行化「停用 vs 续期」
  竞争，观察到过期快照的一方收 409，SR §9 并发用例口径）；
- 全部状态变化写审计（NFR-SEC-03：审计钩子注入，actor 区分人工/系统）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

#: 状态三态（SR-SAAS-003 §4：tenant.status CHECK 约束一致）
STATUS_ACTIVE: str = "active"
STATUS_SUSPENDED: str = "suspended"
STATUS_EXPIRED: str = "expired"

#: 合法迁移边（SR-SAAS-003 §6.1 ALLOWED：运营停用 / 到期扫描 / 续期恢复 /
#: 停用保留期届满 / 保留期内续期恢复）
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (STATUS_ACTIVE, STATUS_SUSPENDED),
        (STATUS_ACTIVE, STATUS_EXPIRED),
        (STATUS_SUSPENDED, STATUS_ACTIVE),
        (STATUS_SUSPENDED, STATUS_EXPIRED),
        (STATUS_EXPIRED, STATUS_ACTIVE),
    }
)

#: 错误码（04-接口设计 §1.3 + SR-SAAS-003 §5 细化）
TENANT_NOT_FOUND: int = 40401
TENANT_STATE_CONFLICT: int = 40901
TENANT_NAME_DUP: int = 40902
RETAIN_WINDOW_PASSED: int = 40903

#: 开通默认套餐（SR-SAAS-003 §7 saas.tenant.default_plan）
DEFAULT_PLAN: str = "standard"

#: 甲方本体租户 id（COM-IP-01/SR-SAAS-007：企业知识仅甲方租户可见的锚点）
CUSTOMER_TENANT_ID: int = 1

#: 停用数据保留天数 / 到期追加归档保留天数（SR §7：90 / 180）
DEFAULT_SUSPEND_RETAIN_DAYS: int = 90
DEFAULT_EXPIRED_RETAIN_DAYS: int = 180

#: 审计钩子签名：(actor, action, target, detail) —— 由运营后台 AuditChain 承接
AuditFn = Callable[[str, str, str, dict], None]


class RegistryError(Exception):
    """租户域业务异常基类：携带业务码 / HTTP 状态 / 中文文案（04 §1.3）。"""

    code: int = 50000
    http_status: int = 500
    message: str = "租户服务内部错误"


class TenantNotFoundError(RegistryError):
    code = TENANT_NOT_FOUND
    http_status = 404
    message = "租户不存在"


class TenantStateConflict(RegistryError):
    code = TENANT_STATE_CONFLICT
    http_status = 409
    message = "租户状态冲突"

    def __init__(self, code: int = TENANT_STATE_CONFLICT, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.code = code
        self.message = message or self.message


@dataclass
class Tenant:
    """租户行（03-数据库设计 §3.5 tenant 表内存形态；expire/retain 为 P5 增量列）。"""

    id: int
    name: str
    plan: str = DEFAULT_PLAN
    status: str = STATUS_ACTIVE
    expire_at: datetime | None = None
    suspended_at: datetime | None = None
    suspend_reason: str = ""
    data_retain_until: datetime | None = None
    quota_overrides: dict[str, int] = field(default_factory=dict)  # tenant_quota 子表形态
    created_at: datetime | None = None


class TenantRegistry:
    """租户注册表：开通/停用/恢复/续期/到期 + 配额覆盖（NFR-SCA-02：新租户零代码，
    接入=注册表落行 + 配额行 + 品牌配置，无任何代码/DDL 变更）。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        suspend_retain_days: int = DEFAULT_SUSPEND_RETAIN_DAYS,
        expired_retain_days: int = DEFAULT_EXPIRED_RETAIN_DAYS,
        audit: AuditFn | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._suspend_retain_days = suspend_retain_days
        self._expired_retain_days = expired_retain_days
        self.audit: AuditFn | None = audit  # 运营后台装配时注入（ops.wire()）
        self._lock = threading.RLock()  # 等价 FOR UPDATE：迁移串行化（SR §6.1）
        self._tenants: dict[int, Tenant] = {}
        self._next_id = CUSTOMER_TENANT_ID + 1
        self._seed_default_tenant()

    # ------------------------------------------------------------------ 内部

    def _seed_default_tenant(self) -> None:
        """租户 1 = 甲方本体显式化（SR-SAAS-001 §6.5 迁移第 1 步的常驻形态）。"""
        self._tenants[CUSTOMER_TENANT_ID] = Tenant(
            id=CUSTOMER_TENANT_ID, name="甲方本体", plan="enterprise", created_at=self._clock()
        )

    def _now(self) -> datetime:
        return self._clock()

    def _emit_audit(self, actor: str, action: str, target: str, detail: dict) -> None:
        if self.audit is not None:
            self.audit(actor, action, target, detail)

    def _require_row(self, tid: int) -> Tenant:
        row = self._tenants.get(tid)
        if row is None:
            raise TenantNotFoundError(f"租户不存在: {tid}")
        return row

    def _transition_locked(
        self,
        row: Tenant,
        to_state: str,
        *,
        actor: str,
        reason: str,
        expected_status: str | None,
    ) -> Tenant:
        """状态迁移（调用方须持锁）：乐观快照复核 → 合法边校验 → 落字段 → 审计。"""
        if expected_status is not None and row.status != expected_status:
            raise TenantStateConflict(
                TENANT_STATE_CONFLICT,
                f"租户状态已变化（期望 {expected_status}，实际 {row.status}），请刷新后重试",
            )
        if (row.status, to_state) not in LEGAL_TRANSITIONS:
            raise TenantStateConflict(
                TENANT_STATE_CONFLICT, f"非法状态迁移 {row.status}→{to_state}"
            )
        from_state = row.status
        row.status = to_state
        now = self._now()
        if to_state == STATUS_SUSPENDED:
            row.suspended_at = now
            row.suspend_reason = reason
            row.data_retain_until = now + timedelta(days=self._suspend_retain_days)
        elif to_state == STATUS_EXPIRED:
            row.data_retain_until = now + timedelta(days=self._expired_retain_days)
        elif to_state == STATUS_ACTIVE:  # 恢复：清保留期锚点（freezer 解冻的闸门侧）
            row.data_retain_until = None
        self._emit_audit(
            actor, "tenant_state", str(row.id), {"from": from_state, "to": to_state, "reason": reason}
        )
        return row

    # ------------------------------------------------------------------ 查询

    def get(self, tid: int) -> Tenant | None:
        """按 id 读取（闸门消费；不存在返回 None 不抛错）。"""
        with self._lock:
            return self._tenants.get(tid)

    def seed(self, tid: int, name: str, status: str = STATUS_ACTIVE) -> Tenant:
        """预置固定 id 租户（隔离套件/测试设施专用，SR-NFR-SEC-07 §7 测试租户口径：
        id 与真实租户隔离；非业务接入路径，不写审计、不参与自增序号）。"""
        with self._lock:
            row = self._tenants.get(tid)
            if row is None:
                row = Tenant(id=int(tid), name=name, status=status, created_at=self._now())
                self._tenants[row.id] = row
                if row.id >= self._next_id:
                    self._next_id = row.id + 1
            return row

    def require(self, tid: int) -> Tenant:
        """按 id 读取，不存在抛 40401。"""
        with self._lock:
            return self._require_row(tid)

    def list_tenants(self) -> list[Tenant]:
        """全量列表（平台域；按 id 升序）。"""
        with self._lock:
            return sorted(self._tenants.values(), key=lambda t: t.id)

    def quota_of(self, tid: int, resource: str) -> int | None:
        """租户级配额覆盖（未配置返回 None，消费方回落默认值）。"""
        with self._lock:
            row = self._require_row(tid)
            return row.quota_overrides.get(resource)

    # ------------------------------------------------------------------ 开通

    def provision(
        self,
        name: str,
        plan: str = DEFAULT_PLAN,
        initial_days: int = 365,
        actor: str = "platform",
    ) -> Tenant:
        """开通（NFR-SCA-02 锚点：纯数据接入；名称唯一为幂等锚点，SR §6.1）。"""
        with self._lock:
            if any(t.name == name for t in self._tenants.values()):
                raise TenantStateConflict(
                    TENANT_NAME_DUP, f"租户名称已存在: {name}"
                )
            row = Tenant(
                id=self._next_id,
                name=name,
                plan=plan,
                expire_at=self._now() + timedelta(days=initial_days),
                created_at=self._now(),
            )
            self._next_id += 1
            self._tenants[row.id] = row
        self._emit_audit(actor, "tenant_provision", str(row.id), {"plan": plan, "days": initial_days})
        return row

    # ------------------------------------------------------------------ 状态迁移（公开操作）

    def transition(
        self,
        tid: int,
        to_state: str,
        *,
        expected_status: str | None = None,
        actor: str = "system",
        reason: str = "",
    ) -> Tenant:
        """原始迁移入口（表驱动测试/系统扫描用）：锁内校验合法边后迁移。"""
        with self._lock:
            row = self._require_row(tid)
            return self._transition_locked(
                row, to_state, actor=actor, reason=reason, expected_status=expected_status
            )

    def suspend(self, tid: int, reason: str = "", actor: str = "platform",
                expected_status: str | None = None) -> Tenant:
        """停用（幂等）：闸门拦截 + 资源冻结，数据全量保留至保留期（SR §2 决策 2）。

        乐观并发（SR §9「并发停用/续期竞争，其一 409，状态一致」）：expected_status
        为调用方观察到的快照（缺省取当前读）；锁内复核——状态已被先到者改变时，
        后到者收 409，绝不产生第二次状态迁移。"""
        expected = expected_status if expected_status is not None else self.require(tid).status
        with self._lock:
            row = self._require_row(tid)
            if row.status != expected:
                raise TenantStateConflict(
                    TENANT_STATE_CONFLICT,
                    f"租户状态已变化（期望 {expected}，实际 {row.status}），请刷新后重试",
                )
            if row.status == STATUS_SUSPENDED:  # 幂等：重复停用直接返回当前态
                return row
            return self._transition_locked(
                row, STATUS_SUSPENDED, actor=actor, reason=reason, expected_status=expected
            )

    def reinstate(self, tid: int, days: int = 365, actor: str = "platform",
                  expected_status: str | None = None) -> Tenant:
        """恢复（suspended/expired → active）：仅面向冻结/到期租户，active 即 409。"""
        expected = expected_status if expected_status is not None else self.require(tid).status
        with self._lock:
            row = self._require_row(tid)
            if row.status != expected:
                raise TenantStateConflict(
                    TENANT_STATE_CONFLICT,
                    f"租户状态已变化（期望 {expected}，实际 {row.status}），请刷新后重试",
                )
            if row.status == STATUS_ACTIVE:
                raise TenantStateConflict(TENANT_STATE_CONFLICT, "活跃租户无「恢复」语义")
            self._check_retain_window(row)  # 保留期外拒绝恢复
            row.expire_at = self._now() + timedelta(days=days)
            self._emit_audit(actor, "tenant_renew", str(row.id), {"days": days, "via": "reinstate"})
            return self._transition_locked(
                row, STATUS_ACTIVE, actor=actor, reason="reinstate", expected_status=expected
            )

    def renew(self, tid: int, days: int, actor: str = "platform",
              expected_status: str | None = None) -> Tenant:
        """续期：任意状态可顺延 expire_at；suspended/expired（保留期内）自动恢复 active。"""
        expected = expected_status if expected_status is not None else self.require(tid).status
        with self._lock:
            row = self._require_row(tid)
            if row.status != expected:
                raise TenantStateConflict(
                    TENANT_STATE_CONFLICT,
                    f"租户状态已变化（期望 {expected}，实际 {row.status}），请刷新后重试",
                )
            if row.status in (STATUS_SUSPENDED, STATUS_EXPIRED):
                self._check_retain_window(row)
                row.expire_at = self._now() + timedelta(days=days)
                self._emit_audit(actor, "tenant_renew", str(row.id), {"days": days})
                return self._transition_locked(
                    row, STATUS_ACTIVE, actor=actor, reason="renew", expected_status=expected
                )
            row.expire_at = row.expire_at + timedelta(days=days) if row.expire_at else self._now() + timedelta(days=days)
            self._emit_audit(actor, "tenant_renew", str(row.id), {"days": days})
            return row

    def expire(self, tid: int, actor: str = "system",
               expected_status: str | None = None) -> Tenant:
        """到期（active/suspended → expired）：保留期起算锚点落行。"""
        expected = expected_status if expected_status is not None else self.require(tid).status
        with self._lock:
            row = self._require_row(tid)
            if row.status != expected:
                raise TenantStateConflict(
                    TENANT_STATE_CONFLICT,
                    f"租户状态已变化（期望 {expected}，实际 {row.status}），请刷新后重试",
                )
            return self._transition_locked(
                row, STATUS_EXPIRED, actor=actor, reason="expire", expected_status=expected
            )

    def purge(self, tid: int, actor: str = "platform_admin") -> None:
        """物理清理（admin-only，保留期届满后）：与「停用=冻结」相区分的最终删除。"""
        expected = self.require(tid).status
        with self._lock:
            row = self._require_row(tid)
            if row.status != expected:
                raise TenantStateConflict(
                    TENANT_STATE_CONFLICT,
                    f"租户状态已变化（期望 {expected}，实际 {row.status}），请刷新后重试",
                )
            if row.status != STATUS_EXPIRED or row.data_retain_until is None:
                raise TenantStateConflict(
                    RETAIN_WINDOW_PASSED, "仅保留期届满的到期租户可清理"
                )
            if row.data_retain_until > self._now():
                raise TenantStateConflict(RETAIN_WINDOW_PASSED, "数据保留期未届满，禁止清理")
            del self._tenants[tid]
        self._emit_audit(actor, "tenant_purge", str(tid), {})

    # ------------------------------------------------------------------ 配额 / 扫描

    def set_quota(self, tid: int, resource: str, hard_limit: int, actor: str = "platform") -> Tenant:
        """配额变更（tenant_quota 行）：写租户覆盖并审计（引擎侧同步由 API 层完成）。"""
        with self._lock:
            row = self._require_row(tid)
            row.quota_overrides[resource] = int(hard_limit)
        self._emit_audit(actor, "tenant_quota", str(tid), {"resource": resource, "hard_limit": hard_limit})
        return row

    def expire_scan(self) -> list[Tenant]:
        """到期扫描：过 expire_at 的 active → expired（actor=system，SR §6.1 jobs）。"""
        now = self._now()
        expired: list[Tenant] = []
        with self._lock:
            for row in sorted(self._tenants.values(), key=lambda t: t.id):
                if row.status == STATUS_ACTIVE and row.expire_at is not None and row.expire_at < now:
                    expired.append(
                        self._transition_locked(
                            row, STATUS_EXPIRED, actor="system", reason="expiry_scan",
                            expected_status=STATUS_ACTIVE,
                        )
                    )
        return expired

    # ------------------------------------------------------------------ 内部工具

    def _check_retain_window(self, row: Tenant) -> None:
        """保留期校验：expired 且 data_retain_until 已过 → 40903（数据已归档）。"""
        if (
            row.status == STATUS_EXPIRED
            and row.data_retain_until is not None
            and row.data_retain_until < self._now()
        ):
            raise TenantStateConflict(RETAIN_WINDOW_PASSED, "数据保留期已过，不可原地恢复")
