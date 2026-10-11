# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""配额引擎与用量小时快照（FR-SAAS-001 §6.4，SR-SAAS-001 §4/§6.4/§7）。

- 四维资源：api_qps（高频，秒级固定窗口实时拦截）/ storage_bytes /
  account_cnt / kb_item_cnt（低频，写路径前置校验 + 定时复核兜底）；
- 判定+计数原子完成（线程锁内，等价 Redis Lua 单脚本语义）；拒绝不消耗额度
  （对齐 foundation/apikey/ratelimit 平台基座口径），被拒次数计入 blocked_cnt；
- 恰等放行、超 1 拒绝；hard_limit=0 禁止语义（tenant_quota DDL 注释口径）；
- 用量按 UTC 整点桶累计，flush_hour 读清语义落盘——重复 flush 幂等不重复累加
  （SR §9「用量落盘幂等」），快照为 FR-SAAS-003 计费数据源。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

#: 四类资源维度（tenant_quota.resource CHECK 枚举，SR §4）
RESOURCE_QPS: str = "api_qps"
RESOURCE_STORAGE: str = "storage_bytes"
RESOURCE_ACCOUNT: str = "account_cnt"
RESOURCE_KB: str = "kb_item_cnt"
QUOTA_RESOURCES: tuple[str, ...] = (RESOURCE_QPS, RESOURCE_STORAGE, RESOURCE_ACCOUNT, RESOURCE_KB)

#: 默认配额（SR §7 quota_default：50 QPS / 50GB / 4 账号 / 5 万条目）
DEFAULT_LIMITS: dict[str, int] = {
    RESOURCE_QPS: 50,
    RESOURCE_STORAGE: 50 * 1024**3,
    RESOURCE_ACCOUNT: 4,
    RESOURCE_KB: 50000,
}

#: 超配额错误码（42901/429 全服务统一，SR §5）
QUOTA_EXCEEDED_CODE: int = 42901


class QuotaExceededError(Exception):
    """配额超限（42901/429，QUOTA_EXCEEDED）：携带资源维度与 Retry-After。"""

    code: int = QUOTA_EXCEEDED_CODE
    http_status: int = 429
    message: str = "已达租户配额上限，请联系运营提升配额"

    def __init__(
        self,
        *,
        tenant_id: int,
        resource: str,
        used: int,
        limit: int,
        retry_after: int | None,
    ) -> None:
        super().__init__(self.message)
        self.tenant_id = tenant_id
        self.resource = resource
        self.used = used
        self.limit = limit
        self.retry_after = retry_after


@dataclass(frozen=True)
class QuotaVerdict:
    """配额判定结果：恰等放行/超 1 拒的裁决载体（SR §6.4 QuotaVerdict）。"""

    allowed: bool
    used: int
    limit: int
    retry_after: int | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    """小时用量快照（tenant_usage_hour 行形态，计费数据源，FR-SAAS-003 消费）。"""

    tenant_id: int
    stat_hour: str            # UTC 整点桶，如 "2026-10-11T12:00:00+00:00"
    api_calls: int = 0
    storage_bytes: int = 0
    account_cnt: int = 0
    kb_item_cnt: int = 0
    blocked_cnt: int = 0      # 超配额被拦截次数（运营视角，SR §4）

    def field_of(self, resource: str) -> int:
        """按资源维度取字段值（导出聚合用）。"""
        return int(getattr(self, resource))


def hour_bucket(moment: datetime) -> str:
    """任意时刻归一到 UTC 整点桶字符串（tenant_usage_hour.stat_hour 口径）。"""
    utc = moment.astimezone(timezone.utc)
    return utc.replace(minute=0, second=0, microsecond=0).isoformat()


class QuotaEngine:
    """配额引擎：原子判定/扣减 + 用量累计 + 小时快照（内存形态，生产为 Redis+PG）。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        limits: Mapping[str, int] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._limits: dict[str, int] = dict(DEFAULT_LIMITS)
        if limits:  # 全局默认覆盖（租户级覆盖见 set_limit）
            self._limits.update(limits)
        self._tenant_limits: dict[tuple[int, str], int] = {}       # 租户覆盖（tenant_quota 行）
        self._current: dict[tuple[int, str], int] = {}             # 低频当前量（写路径校验基准）
        self._qps_windows: dict[tuple[int, int], int] = {}         # (tid, epoch 秒) 固定窗口
        self._pending: dict[tuple[int, str], dict[str, int]] = {}  # (tid, 整点桶) 待落盘增量
        self._flushed: list[UsageSnapshot] = []                    # 已落盘快照（查询/导出读）

    # ------------------------------------------------------------------ 配置

    def set_limit(self, tenant_id: int, resource: str, hard_limit: int) -> None:
        """租户级覆盖（运营后台变更即生效；0=禁止，SR §4 DDL 注释口径）。"""
        self._check_resource(resource)
        with self._lock:
            self._tenant_limits[(int(tenant_id), resource)] = int(hard_limit)

    def limit_of(self, tenant_id: int, resource: str) -> int:
        """生效上限：租户覆盖 > 全局默认（未配置回落，SR §7）。"""
        self._check_resource(resource)
        with self._lock:
            override = self._tenant_limits.get((int(tenant_id), resource))
            return self._limits[resource] if override is None else override

    # ------------------------------------------------------------------ 判定

    def check(self, tenant_id: int, resource: str, amount: int = 1) -> QuotaVerdict:
        """原子「检查+扣减」（拒绝不消耗额度，但计入 blocked_cnt 运营计数）。"""
        self._check_resource(resource)
        tid = int(tenant_id)
        limit = self.limit_of(tid, resource)
        with self._lock:
            if resource == RESOURCE_QPS:
                verdict = self._check_qps_locked(tid, limit, amount)
            else:
                verdict = self._check_low_freq_locked(tid, resource, limit, amount)
            if not verdict.allowed:
                self._record_blocked(tid)  # 拦截也留痕（SR §6.1，blocked_cnt 统计）
            return verdict

    def require(self, tenant_id: int, resource: str, amount: int = 1) -> QuotaVerdict:
        """判定不过即抛 QuotaExceededError（42901，中间件/路由直接映射 429）。"""
        verdict = self.check(tenant_id, resource, amount)
        if not verdict.allowed:
            raise QuotaExceededError(
                tenant_id=int(tenant_id),
                resource=resource,
                used=verdict.used,
                limit=verdict.limit,
                retry_after=verdict.retry_after,
            )
        return verdict

    def record(self, tenant_id: int, resource: str, amount: int = 1) -> None:
        """不经闸门的用量上报通道（llm_tokens 等异步上报，SR §6.4 末段）。"""
        self._check_resource(resource)
        tid = int(tenant_id)
        with self._lock:
            self._current[(tid, resource)] = self.current_usage(tid, resource) + amount
            self._bump_pending(tid, resource, amount)

    # ------------------------------------------------------------------ 观测 / 落盘

    def current_usage(self, tenant_id: int, resource: str) -> int:
        """当前已用量（低频=累计值；QPS=本秒窗口值，只读不清理）。"""
        tid = int(tenant_id)
        with self._lock:
            if resource == RESOURCE_QPS:
                return self._qps_windows.get((tid, int(self._clock().timestamp())), 0)
            return self._current.get((tid, resource), 0)

    def flush_hour(self, bucket: datetime | None = None) -> list[UsageSnapshot]:
        """按小时落盘（读清语义，SR §6.4 usage_flusher）：同桶重复 flush 返回空，
        幂等不重复累加。bucket 缺省取当前时钟的整点桶。"""
        target = hour_bucket(bucket if bucket is not None else self._clock())
        with self._lock:
            drained = [
                key for key in self._pending if key[1] == target
            ]
            snapshots: list[UsageSnapshot] = []
            for tid, _ in sorted(drained):
                fields = self._pending.pop((tid, target))
                snapshots.append(
                    UsageSnapshot(
                        tenant_id=tid,
                        stat_hour=target,
                        api_calls=fields.get(RESOURCE_QPS, 0),
                        storage_bytes=fields.get(RESOURCE_STORAGE, 0),
                        account_cnt=fields.get(RESOURCE_ACCOUNT, 0),
                        kb_item_cnt=fields.get(RESOURCE_KB, 0),
                        blocked_cnt=fields.get("blocked", 0),
                    )
                )
            self._flushed.extend(snapshots)
            return snapshots

    def usage_snapshots(self, tenant_id: int | None = None) -> list[UsageSnapshot]:
        """已落盘快照读取（运营后台用量查询/导出数据源）。"""
        with self._lock:
            rows = list(self._flushed)
        if tenant_id is None:
            return rows
        return [row for row in rows if row.tenant_id == int(tenant_id)]

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_resource(resource: str) -> None:
        if resource not in QUOTA_RESOURCES:
            raise ValueError(f"未知配额资源: {resource}（合法维度: {QUOTA_RESOURCES}）")

    def _check_qps_locked(self, tid: int, limit: int, amount: int) -> QuotaVerdict:
        """QPS 固定窗口（1 秒桶）：清理过期桶 → 判定 → 通过才计数。"""
        second = int(self._clock().timestamp())
        stale = [key for key in self._qps_windows if key[1] < second]
        for key in stale:  # 顺手清理，防长期运行无界增长
            del self._qps_windows[key]
        used = self._qps_windows.get((tid, second), 0)
        if used + amount > limit:  # 恰等放行、超 1 拒；拒绝不消耗窗口额度
            return QuotaVerdict(allowed=False, used=used, limit=limit, retry_after=1)
        self._qps_windows[(tid, second)] = used + amount
        self._bump_pending(tid, RESOURCE_QPS, amount)
        return QuotaVerdict(allowed=True, used=used + amount, limit=limit)

    def _check_low_freq_locked(self, tid: int, resource: str, limit: int, amount: int) -> QuotaVerdict:
        """低频写路径前置校验：通过才累计（无脏数据，SR §9「知识条目超限」）。"""
        used = self._current.get((tid, resource), 0)
        if used + amount > limit:
            return QuotaVerdict(allowed=False, used=used, limit=limit)
        self._current[(tid, resource)] = used + amount
        self._bump_pending(tid, resource, amount)
        return QuotaVerdict(allowed=True, used=used + amount, limit=limit)

    def _bump_pending(self, tid: int, resource: str, amount: int) -> None:
        """待落盘增量累计（调用方持锁；按当前整点桶归集）。"""
        bucket = hour_bucket(self._clock())
        fields = self._pending.setdefault((tid, bucket), {})
        fields[resource] = fields.get(resource, 0) + amount

    def _record_blocked(self, tid: int) -> None:
        """被 429 拦截计数（blocked_cnt，运营视角；不计入资源用量）。"""
        with self._lock:
            bucket = hour_bucket(self._clock())
            fields = self._pending.setdefault((tid, bucket), {})
            fields["blocked"] = fields.get("blocked", 0) + 1
