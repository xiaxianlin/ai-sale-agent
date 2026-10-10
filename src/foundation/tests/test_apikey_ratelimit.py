"""知识产权归甲方所有（COM-IP-01）

FR-KB-031 限流与日配额单元测试（QPS 滑动窗口边界 / 日配额 / 拒绝不计数 / Redis 占位）。

覆盖（SR-KB-031 §9 限流边界与拒绝不计数 + §6.3）：
- 突发：qps=N 窗口内第 N+1 次拒绝（reason=qps，Retry-After=最老记录滑出秒数）；
- 跨窗：窗口宽度边界（恰好滑出放行 / 差一毫秒仍拒绝）；
- 日配额：daily 超限拒绝（reason=daily，Retry-After=至次日零点秒数，Asia/Shanghai 切日）；
- 拒绝不消耗配额：QPS 拒绝不增日配额、日配额拒绝不占 QPS 窗口；
- DailyQuota 计数器：跨日翻转 / 保留天数清理；
- RedisRateLimiter：import 守护占位（模块可导入、构造给明确错误）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pytest

from foundation.apikey import ratelimit as rl
from foundation.apikey.ratelimit import (
    DailyQuota,
    QuotaExceededError,
    RateLimitResult,
    RedisRateLimiter,
    SlidingWindowRateLimiter,
    seconds_to_midnight,
)


class FakeTime:
    """可推进假时钟：float 秒 ↔ UTC 感知 datetime（滑动窗口边界的时间源）。"""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.t, tz=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.t += seconds


def fixed_clock(moment: datetime) -> Callable[[], datetime]:
    """固定时钟（日配额 Retry-After 的确定性断言用）。"""
    return lambda: moment


# --------------------------------------------------------------------------- QPS 滑动窗口


class TestSlidingWindowQps:
    def test_burst_within_window(self) -> None:
        """突发：qps=5 时同窗第 6 次拒绝、前 5 次放行（SR §9 qps=5 第 6 次）。"""
        time = FakeTime()
        limiter = SlidingWindowRateLimiter(clock=time)
        results = [limiter.check("c1", qps=5, daily=100) for _ in range(6)]
        assert all(r.allowed for r in results[:5])
        denied = results[5]
        assert not denied.allowed and denied.reason == "qps"

    def test_burst_retry_after_bounded_by_window(self) -> None:
        """QPS 拒绝的 Retry-After ≤ 窗口宽度（等待最老记录滑出）且恒 ≥1。"""
        time = FakeTime()
        limiter = SlidingWindowRateLimiter(window_ms=2000, clock=time)
        for _ in range(2):
            limiter.check("c1", qps=2, daily=100)
        time.advance(0.5)
        denied = limiter.check("c1", qps=2, daily=100)
        assert not denied.allowed
        assert denied.retry_after == 2  # 窗口 2s，最老记录 0.5s 前进入 → 等待 1.5s 向上取整

    def test_cross_window_slide_out(self) -> None:
        """跨窗：记录滑出窗口后恢复放行（窗口随时间平移，非固定桶）。"""
        time = FakeTime()
        limiter = SlidingWindowRateLimiter(clock=time)
        assert limiter.check("c1", qps=1, daily=100).allowed
        time.advance(0.6)
        assert not limiter.check("c1", qps=1, daily=100).allowed  # 窗口内仍占用
        time.advance(0.4 + 0.001)  # 越过 1s 窗口
        assert limiter.check("c1", qps=1, daily=100).allowed

    def test_window_boundary_exact(self) -> None:
        """边界精确判定：now - 窗口宽 == 最老记录时间戳时，该记录恰好滑出（放行）。"""
        time = FakeTime()
        limiter = SlidingWindowRateLimiter(clock=time)
        assert limiter.check("c1", qps=1, daily=100).allowed      # t=0 记录
        time.advance(0.999)
        assert not limiter.check("c1", qps=1, daily=100).allowed  # t=0.999：差 1ms 未滑出
        time.advance(0.001)                                        # t=1.000：恰好滑出
        assert limiter.check("c1", qps=1, daily=100).allowed

    def test_consumers_isolated_windows(self) -> None:
        """按 Key 独立限流：消费方之间窗口互不影响（04 §1.2 按 key 配 QPS）。"""
        time = FakeTime()
        limiter = SlidingWindowRateLimiter(clock=time)
        assert limiter.check("c1", qps=1, daily=100).allowed
        assert not limiter.check("c1", qps=1, daily=100).allowed
        assert limiter.check("c2", qps=1, daily=100).allowed      # c2 独立窗口

    def test_result_truthiness(self) -> None:
        """RateLimitResult 真值即「允许」（布尔兼容用法，SR-KB-030 §6.2）。"""
        assert bool(RateLimitResult(allowed=True))
        assert not bool(RateLimitResult(allowed=False, reason="qps", retry_after=3))

    def test_invalid_quota_args(self) -> None:
        """非正配额属配置错误：立即抛 ValueError（fail-fast，不静默放行）。"""
        limiter = SlidingWindowRateLimiter()
        with pytest.raises(ValueError):
            limiter.check("c1", qps=0, daily=100)
        with pytest.raises(ValueError):
            limiter.check("c1", qps=5, daily=0)
        with pytest.raises(ValueError):
            SlidingWindowRateLimiter(window_ms=0)


# --------------------------------------------------------------------------- 日配额


class TestDailyQuota:
    def test_daily_exceeded_with_retry_until_midnight(self) -> None:
        """daily=10 第 11 次拒绝，Retry-After=至次日零点秒数（SR §9：=至次日零点秒数）。"""
        moment = datetime(2026, 10, 9, 7, 30, 0, tzinfo=timezone.utc)  # 上海 15:30 → 余 8.5h
        limiter = SlidingWindowRateLimiter(clock=fixed_clock(moment))
        for _ in range(10):
            assert limiter.check("c1", qps=1000, daily=10).allowed
        denied = limiter.check("c1", qps=1000, daily=10)
        assert not denied.allowed and denied.reason == "daily"
        assert denied.retry_after == 30600  # 15:30:00 → 次日 00:00:00 恰 8.5 小时

    def test_daily_rollover_next_day(self) -> None:
        """跨日（Asia/Shanghai 切日）计数自动翻转：次日恢复可用额度。"""
        time = FakeTime(start=datetime(2026, 10, 9, 15, 30, 0, tzinfo=timezone.utc).timestamp())  # 上海 23:30
        limiter = SlidingWindowRateLimiter(clock=time)
        assert limiter.check("c1", qps=1000, daily=1).allowed
        assert not limiter.check("c1", qps=1000, daily=1).allowed   # 当日打满
        time.advance(3600)                                          # 越过上海零点
        assert limiter.check("c1", qps=1000, daily=1).allowed       # 新的一天恢复

    def test_qps_rejection_does_not_consume_daily(self) -> None:
        """拒绝不计数（口径①）：QPS 超限的请求不消耗日配额（SR §9 日配额计数不增长）。"""
        time = FakeTime()
        limiter = SlidingWindowRateLimiter(clock=time)
        limiter.check("c1", qps=1, daily=10)                        # 唯一放行 → daily=1
        for _ in range(5):
            denied = limiter.check("c1", qps=1, daily=10)
            assert not denied.allowed and denied.reason == "qps"
        assert limiter.daily_quota.used("c1") == 1                  # 5 次拒绝未消耗配额

    def test_daily_rejection_does_not_consume_qps_window(self) -> None:
        """拒绝不计数（口径②）：日配额超限的请求不占用 QPS 滑动窗口名额。"""
        time = FakeTime()
        limiter = SlidingWindowRateLimiter(clock=time)
        limiter.check("c1", qps=2, daily=1)                         # 放行 → qps 窗口 1 条 / daily=1
        time.advance(0.1)
        denied = limiter.check("c1", qps=2, daily=1)
        assert not denied.allowed and denied.reason == "daily"
        assert limiter.window_count("c1") == 1                      # 拒绝未追加窗口记录

    def test_daily_quota_counter_used_remaining_reset(self) -> None:
        """DailyQuota 计数器原语：used/remaining/increment/reset。"""
        moment = datetime(2026, 10, 9, 7, 30, 0, tzinfo=timezone.utc)
        daily = DailyQuota(clock=fixed_clock(moment))
        assert daily.used("c1") == 0 and daily.remaining("c1", daily=10) == 10
        assert daily.increment("c1") == 1 and daily.increment("c1") == 2
        assert daily.used("c1") == 2 and daily.remaining("c1", daily=10) == 8
        daily.reset("c1")
        assert daily.used("c1") == 0

    def test_daily_prune_stale_days(self) -> None:
        """过期日键清理：保留窗口外的旧日计数不长期驻留（等价 Redis TTL 48h）。"""
        time = FakeTime(start=datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        daily = DailyQuota(clock=time)
        daily.increment("c1")
        time.advance(3 * 86400)  # 3 天后
        daily.increment("c1")    # 触发清理
        assert len(daily._counts) == 1  # 仅保留当前日键

    def test_seconds_to_midnight_values(self) -> None:
        """零点等待秒数：正点整算精确值，微秒残余向上取整（恒 ≥1）。"""
        exact = datetime(2026, 10, 9, 7, 30, 0, tzinfo=timezone.utc)     # 上海 15:30 整
        assert seconds_to_midnight(exact) == 30600
        with_micro = datetime(2026, 10, 9, 15, 59, 59, 500000, tzinfo=timezone.utc)  # 上海 23:59:59.5
        assert seconds_to_midnight(with_micro) == 1                      # 0.5s 余量向上取整


# --------------------------------------------------------------------------- 异常与 Redis 占位


class TestQuotaErrorAndRedis:
    def test_quota_exceeded_error_shape(self) -> None:
        """QuotaExceededError：42901/429 + retry_after 下取整钳制 ≥1。"""
        err = QuotaExceededError(reason="daily", retry_after=0)
        assert err.code == 42901 and err.http_status == 429
        assert err.retry_after == 1 and err.reason == "daily"

    def test_module_importable_without_redis(self) -> None:
        """import 守护：redis 客户端未安装时模块可正常导入（本地/测试环境约束）。"""
        assert hasattr(rl, "_SLIDING_LUA")  # 生产脚本契约随模块暴露

    def test_redis_placeholder_raises_on_construct(self) -> None:
        """RedisRateLimiter 占位：构造给出明确错误（未装 redis → RuntimeError；已装 →
        NotImplementedError，Lua 实现随部署侧落地）。"""
        with pytest.raises((RuntimeError, NotImplementedError)):
            RedisRateLimiter()
