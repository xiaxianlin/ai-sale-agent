# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-KB-031 API 限流与日配额（QPS 滑动窗口 + 日配额计数）——平台基座实现。

依据：docs/07-实现方案/KB/SR-KB-031-API鉴权与配额.md
- §6.3 限流器：QPS 滑动窗口 + 日配额，判定+计数原子完成（生产为 Redis Lua 单次往返）；
  拒绝不消耗配额（被拒请求不占用调用方额度）；
- §4 Redis 键：kb:quota:qps:{consumer}（zset，score=毫秒时间戳）/ kb:quota:day:{consumer}:{yyyymmdd}
  （counter，TTL 48h）——键语义在本文件以内存数据结构等价实现；
- §7 配置项：KB_API_QPS_WINDOW_MS 默认 1000（500~5000）；
- 日配额时区 Asia/Shanghai（§4 yyyymmdd 口径）。

平台基座范围：RateLimiter Protocol + SlidingWindowRateLimiter（内存实现，判定/计数在
同一线程锁内原子完成）+ DailyQuota（日计数器，跨日自动翻转）+ RedisRateLimiter 占位
（import 守护：redis 客户端未安装时模块可正常导入，构造时给出明确错误）。
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, Protocol
from zoneinfo import ZoneInfo

from platform.core.apikey.keys import ApiKeyError, DEFAULT_DAILY, DEFAULT_QPS

TZ_SHANGHAI: ZoneInfo = ZoneInfo("Asia/Shanghai")
"""日配额时区（SR §4：yyyymmdd 按 Asia/Shanghai 切日）。"""

DEFAULT_WINDOW_MS: int = 1000
"""QPS 滑动窗口宽度默认值（毫秒，SR §7 KB_API_QPS_WINDOW_MS）。"""

DAILY_RETENTION_DAYS: int = 2
"""日配额计数保留天数（等价 Redis TTL 48h，SR §4）。"""

try:  # import 守护：redis 客户端未安装时不得阻断模块导入（本地/测试环境无 redis）
    import redis as _redis  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 取决于部署环境是否安装 redis
    _redis = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- 结果与异常


RateLimitReason = Literal["", "qps", "daily"]
"""超限原因：空=允许；qps=滑动窗口超限；daily=日配额超限（Retry-After 语义不同）。"""


@dataclass(frozen=True)
class RateLimitResult:
    """限流判定结果：真值即「允许」（兼容 SR-KB-030 §6.2 的布尔用法）。

    - retry_after：建议重试等待秒数（HTTP Retry-After 头来源），恒 ≥1；
      qps 超限 = 窗口内最老记录滑出所需秒数；daily 超限 = 距次日零点秒数（Asia/Shanghai）。
    """

    allowed: bool
    reason: RateLimitReason = ""
    retry_after: int = 1

    def __bool__(self) -> bool:
        """允许时为真（if limiter.check(...): ... 的直接用法）。"""
        return self.allowed


class QuotaExceededError(ApiKeyError):
    """触发限流（42901/429，FR-KB-031）：QPS 或日配额超限，携带 Retry-After。"""

    code = 42901
    http_status = 429
    message = "触发限流（QPS 或日配额超限，FR-KB-031）"

    def __init__(self, *, reason: RateLimitReason, retry_after: int) -> None:
        super().__init__(self.message)
        self.reason: RateLimitReason = reason
        self.retry_after: int = max(1, int(retry_after))


# --------------------------------------------------------------------------- 限流器协议


class RateLimiter(Protocol):
    """限流器协议：判定 + 计数原子完成（SR §6.3 QuotaGuard.check 契约）。

    实现方必须保证「拒绝不计数」——QPS/日配额任一超限时不得追加任何计数
    （生产由 Lua 单脚本原子性保证；内存实现由线程锁内先判后记保证）。
    """

    def check(self, consumer: str, *, qps: int, daily: int) -> RateLimitResult:
        """按调用方判定 QPS 窗口与日配额；通过则同时计数并返回 allowed=True。"""
        ...  # pragma: no cover - Protocol 桩


# --------------------------------------------------------------------------- 日配额计数器


def _day_key(now: datetime) -> str:
    """日配额键的 yyyymmdd 段（Asia/Shanghai 切日，SR §4）。"""
    return now.astimezone(TZ_SHANGHAI).strftime("%Y%m%d")


def seconds_to_midnight(now: datetime) -> int:
    """距次日零点的秒数（Asia/Shanghai；日配额 Retry-After 来源，恒 ≥1）。

    取值含 ceil 向上取整：避免整秒边界算出 0（Retry-After 按规范须为正整数）。
    """
    local = now.astimezone(TZ_SHANGHAI)
    next_midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((next_midnight - local).total_seconds()))


class DailyQuota:
    """日配额计数器：按 (consumer, yyyymmdd) 计数，跨日自动翻转、过期键清理。

    内存形态等价 Redis counter（kb:quota:day:{consumer}:{day}，TTL 48h）：
    increment 时顺手清理超过保留天数的旧日键，防止长期运行内存无界增长。
    时钟注入：clock 返回任意时区感知时间（内部统一换算 Asia/Shanghai）。
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}

    def now(self) -> datetime:
        """统一时间源。"""
        return self._clock()

    def used(self, consumer: str, *, now: datetime | None = None) -> int:
        """当日已用额度（只读，不计数；拒绝判定与观测共用）。"""
        moment = now if now is not None else self.now()
        with self._lock:
            return self._counts.get((consumer, _day_key(moment)), 0)

    def remaining(self, consumer: str, *, daily: int, now: datetime | None = None) -> int:
        """当日剩余额度（观测用）。"""
        return max(0, daily - self.used(consumer, now=now))

    def increment(self, consumer: str, *, now: datetime | None = None) -> int:
        """计数 +1（仅在判定通过后由限流器调用），返回当日新值；顺带清理过期旧日键。"""
        moment = now if now is not None else self.now()
        day = _day_key(moment)
        with self._lock:
            self._prune_stale_days(day)
            key = (consumer, day)
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]

    def reset(self, consumer: str | None = None) -> None:
        """清零计数（测试隔离用；consumer=None 清全部）。"""
        with self._lock:
            if consumer is None:
                self._counts.clear()
            else:
                self._counts = {k: v for k, v in self._counts.items() if k[0] != consumer}

    def _prune_stale_days(self, today: str) -> None:
        """清理超出保留天数的旧日键（调用方须已持锁）。"""
        keep_from = _shift_day(today, -(DAILY_RETENTION_DAYS - 1))
        stale = [key for key in self._counts if key[1] < keep_from]
        for key in stale:
            del self._counts[key]


def _shift_day(day: str, delta_days: int) -> str:
    """yyyymmdd 字符串按天数平移（保留天数比较用；闭包 YYYYMMDD 字典序即时间序）。"""
    base = datetime.strptime(day, "%Y%m%d").date()
    return (base + timedelta(days=delta_days)).strftime("%Y%m%d")


# --------------------------------------------------------------------------- 内存滑动窗口限流器


class SlidingWindowRateLimiter:
    """内存限流器：QPS 滑动窗口 + 日配额（经 DailyQuota），实现 RateLimiter 协议。

    判定顺序与生产 Lua 一致（SR §6.3）：① QPS 滑动窗口超限 → ② 日配额超限 →
    ③ 通过则双计数（QPS 记时间戳 + 日配额 +1）；拒绝路径不产生任何计数。

    原子性：单消费者 deque + 全局线程锁，check 全程持锁——等价 Lua 单脚本原子语义；
    生产横向扩展时替换为 RedisRateLimiter（状态集中 Redis，NFR-SCA-01）。

    时钟注入：clock 返回感知时间；窗口计算基于 timestamp() 浮点秒。
    """

    def __init__(
        self,
        *,
        window_ms: int = DEFAULT_WINDOW_MS,
        clock: Callable[[], datetime] | None = None,
        daily_quota: DailyQuota | None = None,
    ) -> None:
        if window_ms <= 0:
            raise ValueError(f"窗口宽度必须为正毫秒数: {window_ms}")
        self._window_seconds = window_ms / 1000.0
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._daily = daily_quota if daily_quota is not None else DailyQuota(clock=self._clock)
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    @property
    def window_seconds(self) -> float:
        """窗口宽度（秒，测试/观测用）。"""
        return self._window_seconds

    @property
    def daily_quota(self) -> DailyQuota:
        """日配额计数器（观测/测试只读入口；生产等价 Redis counter 镜像）。"""
        return self._daily

    def check(self, consumer: str, *, qps: int, daily: int) -> RateLimitResult:
        """判定 + 计数（原子；拒绝不计数）。qps/daily须 ≥1（配额语义为正整数）。"""
        if qps < 1 or daily < 1:
            raise ValueError(f"配额必须为正整数: qps={qps}, daily={daily}")
        moment = self._clock()
        now_ts = moment.timestamp()
        with self._lock:
            # ① QPS 滑动窗口：清理窗口外记录后计数（等价 ZREMRANGEBYSCORE + ZCARD）
            window = self._windows.setdefault(consumer, deque())
            boundary = now_ts - self._window_seconds
            while window and window[0] <= boundary:
                window.popleft()
            if len(window) >= qps:
                oldest = window[0]
                wait = max(1, math.ceil(self._window_seconds - (now_ts - oldest)))
                return RateLimitResult(allowed=False, reason="qps", retry_after=wait)
            # ② 日配额（等价 GET 后比较；拒绝不递增）
            if self._daily.used(consumer, now=moment) >= daily:
                return RateLimitResult(
                    allowed=False, reason="daily", retry_after=seconds_to_midnight(moment)
                )
            # ③ 通过：双计数（等价 ZADD + INCR，同脚本原子完成）
            window.append(now_ts)
        self._daily.increment(consumer, now=moment)  # 锁外递增：DailyQuota 自带线程锁
        return RateLimitResult(allowed=True)

    def window_count(self, consumer: str, *, now: datetime | None = None) -> int:
        """当前窗口内记录数（测试观测用；只读不清理）。"""
        moment = now if now is not None else self._clock()
        now_ts = moment.timestamp()
        boundary = now_ts - self._window_seconds
        with self._lock:
            window = self._windows.get(consumer, deque())
            return sum(1 for ts in window if ts > boundary)


# --------------------------------------------------------------------------- Redis 限流占位（部署侧落地）

# 生产 Lua 脚本（SR §6.3 逐字保留）：QPS 滑动窗口 + 日配额单脚本原子判定，
# 键名对齐 §4（qkey=kb:quota:qps:{consumer}，dkey=kb:quota:day:{consumer}:{yyyymmdd}）。
_SLIDING_LUA = """
local qkey, dkey = KEYS[1], KEYS[2]
local now, window, qps = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local daily, day_retry, ttl, member = tonumber(ARGV[4]), tonumber(ARGV[5]), tonumber(ARGV[6]), ARGV[7]
redis.call('ZREMRANGEBYSCORE', qkey, 0, now - window)
if redis.call('ZCARD', qkey) >= qps then
  local oldest = redis.call('ZRANGE', qkey, 0, 0, 'WITHSCORES')
  return {0, 1, window/1000 - (now - tonumber(oldest[2]))/1000 + 0.05}
end
local used = tonumber(redis.call('GET', dkey) or '0')
if used >= daily then return {0, 2, day_retry} end
redis.call('ZADD', qkey, now, member)
redis.call('PEXPIRE', qkey, window * 2)
if used == 0 then redis.call('SET', dkey, 1, 'EX', ttl) else redis.call('INCR', dkey) end
return {1, 0, 0}
"""


class RedisRateLimiter:
    """Redis Lua 限流器占位（部署侧落地，SR §6.3）：本基座仅冻结契约与脚本。

    - import 守护：模块导入不依赖 redis 客户端（见文件头 try/except）；
    - 未安装 redis：构造即抛 RuntimeError（本地/测试请用 SlidingWindowRateLimiter）；
    - 已安装 redis：抛 NotImplementedError——Lua 实现（_SLIDING_LUA）随 kb-service
      部署侧落地（时区切日键、毫秒时间戳、单飞回填等生产细节在该层补齐）。
    """

    SCRIPT = _SLIDING_LUA
    """生产判定脚本（只读暴露供部署侧注册 EVALSHA 与评审比对）。"""

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self._url = url
        if _redis is None:
            raise RuntimeError(
                "redis 客户端未安装：RedisRateLimiter 为部署侧占位，"
                "本地与单元测试请使用 SlidingWindowRateLimiter（内存实现）"
            )
        raise NotImplementedError(
            "Redis Lua 限流实现随部署侧落地（SR-KB-031 §6.3 _SLIDING_LUA；"
            "平台基座仅冻结 RateLimiter 协议与脚本契约）"
        )

    def check(self, consumer: str, *, qps: int = DEFAULT_QPS, daily: int = DEFAULT_DAILY) -> RateLimitResult:
        """协议桩：构造已被拦截，此方法不可达（保留签名供类型检查对齐协议）。"""
        raise NotImplementedError  # pragma: no cover - 构造期已拦截
