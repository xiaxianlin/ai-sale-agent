# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""配额引擎单元测试（FR-SAAS-001 §6.4/§9；TDD 阶段①②，五类场景齐备）。

覆盖：四维资源（api_qps/storage_bytes/account_cnt/kb_item_cnt）恰等放行、
超 1 拒绝、42901 错误契约、并发原子性（恰 limit 个放行）、小时快照与幂等
flush（读清语义）、租户级覆盖与 hard_limit=0 禁止语义。
验收口径映射：SRS §16.6②「租户级配额可配置、超配额拦截（429）、用量按小时可查」。

实现方案：docs/07-实现方案/SAAS/SR-SAAS-001-多租户架构.md §4/§6.4/§9
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from services.saas.core.quota.engine import (
    DEFAULT_LIMITS,
    QUOTA_RESOURCES,
    QuotaEngine,
    QuotaExceededError,
    hour_bucket,
)

T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
TID = 7


class FakeClock:
    """可拨动时钟（并发/快照用例控制时间推进；advance 支持秒/分/时关键字）。"""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0) -> None:
        self.now = self.now + timedelta(seconds=seconds, minutes=minutes, hours=hours)


def make_engine(**kw) -> tuple[QuotaEngine, FakeClock]:
    clock = kw.pop("clock", None) or FakeClock()
    return QuotaEngine(clock=clock, **kw), clock


# --------------------------------------------------------------------------- 等价类（四维）


def test_default_limits_and_each_dimension_allowed() -> None:
    """四维默认配额锚点（SR §7：QPS 50 / 存储 50GB / 账号 4 / 条目 5 万）；
    未配置租户回落默认值，且四维限额内正常消费路径全部放行。"""
    assert QUOTA_RESOURCES == ("api_qps", "storage_bytes", "account_cnt", "kb_item_cnt")
    assert DEFAULT_LIMITS["api_qps"] == 50
    assert DEFAULT_LIMITS["storage_bytes"] == 50 * 1024**3
    assert DEFAULT_LIMITS["account_cnt"] == 4
    assert DEFAULT_LIMITS["kb_item_cnt"] == 50000
    engine, _ = make_engine()
    for resource in QUOTA_RESOURCES:
        assert engine.limit_of(TID, resource) == DEFAULT_LIMITS[resource]
        verdict = engine.check(TID, resource, amount=1)
        assert verdict.allowed and verdict.used == 1 and verdict.limit == DEFAULT_LIMITS[resource]


# --------------------------------------------------------------------------- 边界（恰等放行 / 超 1 拒）


def test_qps_equal_to_limit_allowed_over_by_one_denied() -> None:
    """QPS：恰等放行（第 3 次 == limit=3 允许），超 1 拒（第 4 次拒绝）。"""
    engine, _ = make_engine(limits={"api_qps": 3})
    assert engine.check(TID, "api_qps").allowed
    assert engine.check(TID, "api_qps").allowed
    third = engine.check(TID, "api_qps")
    assert third.allowed and third.used == 3
    fourth = engine.check(TID, "api_qps")
    assert not fourth.allowed and fourth.retry_after == 1


def test_low_freq_equal_to_limit_allowed_over_by_one_denied() -> None:
    """低频（kb_item_cnt）：写路径前置校验，恰等放行、超 1 拒。"""
    engine, _ = make_engine(limits={"kb_item_cnt": 2})
    assert engine.check(TID, "kb_item_cnt").allowed
    assert engine.check(TID, "kb_item_cnt").allowed
    assert not engine.check(TID, "kb_item_cnt").allowed
    assert engine.current_usage(TID, "kb_item_cnt") == 2  # 拒绝不消耗额度


def test_storage_and_account_dimensions_gated() -> None:
    """存储与账号数维度同样前置校验（超 1 即拒）。"""
    engine, _ = make_engine(
        limits={"storage_bytes": 100, "account_cnt": 2}
    )
    assert engine.check(TID, "storage_bytes", amount=100).allowed
    assert not engine.check(TID, "storage_bytes", amount=1).allowed  # 100+1 超限
    assert engine.check(TID, "account_cnt", amount=2).allowed
    assert not engine.check(TID, "account_cnt").allowed


# --------------------------------------------------------------------------- 异常（42901 契约）


def test_require_raises_42901_contract() -> None:
    """require 抛 QuotaExceededError：code=42901、HTTP 429、retry_after=1、资源名携带。"""
    engine, _ = make_engine(limits={"api_qps": 1})
    engine.require(TID, "api_qps")
    with pytest.raises(QuotaExceededError) as excinfo:
        engine.require(TID, "api_qps")
    err = excinfo.value
    assert err.code == 42901 and err.http_status == 429
    assert err.resource == "api_qps" and err.retry_after == 1
    assert err.tenant_id == TID
    assert "配额" in err.message


def test_unknown_resource_rejected() -> None:
    """未知资源维度 fail-closed：直接拒绝（不静默放行）。"""
    engine, _ = make_engine()
    with pytest.raises(ValueError, match="未知配额资源"):
        engine.check(TID, "gpu_hours")


def test_zero_limit_means_blocked() -> None:
    """hard_limit=0 禁止语义（tenant_quota DDL 注释：0=禁止）。"""
    engine, _ = make_engine(limits={"account_cnt": 0})
    verdict = engine.check(TID, "account_cnt")
    assert not verdict.allowed


# --------------------------------------------------------------------------- 配置（租户级覆盖）


def test_tenant_override_affects_only_target_tenant() -> None:
    """租户级覆盖生效且不外溢：tid=7 kb=2，tid=8 仍回落默认 50000。"""
    engine, _ = make_engine()
    engine.set_limit(TID, "kb_item_cnt", 2)
    assert engine.limit_of(TID, "kb_item_cnt") == 2
    assert engine.limit_of(TID + 1, "kb_item_cnt") == DEFAULT_LIMITS["kb_item_cnt"]


# --------------------------------------------------------------------------- 并发（原子性）


def test_concurrent_qps_exactly_limit_admitted() -> None:
    """QPS 并发原子：limit=20、50 线程各申请 1 → 恰 20 放行 / 30 拒绝，终值 20。"""
    engine, _ = make_engine(limits={"api_qps": 20})
    barrier = threading.Barrier(50)

    def worker() -> bool:
        barrier.wait()
        return engine.check(TID, "api_qps").allowed

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(lambda _: worker(), range(50)))
    assert sum(results) == 20
    assert engine.current_usage(TID, "api_qps") == 20


def test_concurrent_low_freq_exactly_limit_admitted() -> None:
    """低频并发原子：kb_item_cnt limit=10、25 线程 → 恰 10 写入成功。"""
    engine, _ = make_engine(limits={"kb_item_cnt": 10})
    barrier = threading.Barrier(25)

    def worker() -> bool:
        barrier.wait()
        return engine.check(TID, "kb_item_cnt").allowed

    with ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(lambda _: worker(), range(25)))
    assert sum(results) == 10
    assert engine.current_usage(TID, "kb_item_cnt") == 10


# --------------------------------------------------------------------------- 降级/快照（小时快照 + 幂等）


def test_hourly_snapshot_fields_and_utc_bucket() -> None:
    """小时快照：UTC 整点桶、七字段齐（含 blocked_cnt 运营视角字段）。"""
    engine, clock = make_engine(limits={"api_qps": 1})
    engine.check(TID, "api_qps")
    engine.check(TID, "api_qps")  # blocked
    clock.advance(seconds=1)      # 滑入下一秒仍同小时
    engine.check(TID, "kb_item_cnt")
    snapshots = engine.flush_hour()
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.tenant_id == TID
    assert snap.stat_hour == "2026-10-11T12:00:00+00:00"
    assert snap.api_calls == 1 and snap.blocked_cnt == 1 and snap.kb_item_cnt == 1
    assert snap.storage_bytes == 0 and snap.account_cnt == 0


def test_hourly_flush_idempotent_read_clear() -> None:
    """落盘幂等（SR §9 用量落盘幂等）：同桶重复 flush 第二次为空且不重复累计。"""
    engine, _ = make_engine()
    engine.check(TID, "kb_item_cnt", amount=3)
    first = engine.flush_hour()
    second = engine.flush_hour()
    assert len(first) == 1 and first[0].kb_item_cnt == 3
    assert second == []  # 读清语义：重复搬运不重复累加


def test_hourly_snapshots_split_by_bucket() -> None:
    """跨小时分桶：13 点桶与 12 点桶各自成快照（用量按小时可查口径）；
    hour_bucket 工具把任意时刻（含跨时区时刻）归一到 UTC 整点桶字符串。"""
    engine, clock = make_engine()
    engine.check(TID, "kb_item_cnt")
    clock.advance(hours=1)
    engine.check(TID, "kb_item_cnt")
    first = engine.flush_hour(bucket=T0)
    second = engine.flush_hour(bucket=T0 + timedelta(hours=1))
    assert first[0].kb_item_cnt == 1 and second[0].kb_item_cnt == 1
    assert {s.stat_hour for s in (first + second)} == {
        "2026-10-11T12:00:00+00:00",
        "2026-10-11T13:00:00+00:00",
    }
    assert hour_bucket(T0) == "2026-10-11T12:00:00+00:00"
    assert hour_bucket(T0 + timedelta(minutes=59, seconds=59)) == "2026-10-11T12:00:00+00:00"
    assert (
        hour_bucket(datetime(2026, 10, 11, 20, 30, tzinfo=timezone(timedelta(hours=8))))
        == "2026-10-11T12:00:00+00:00"
    )  # 东八区 20:30 == UTC 12:30，桶按 UTC 切
