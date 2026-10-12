# 知识产权归甲方所有（COM-IP-01）
"""TDD 补充测试（平台域）：MAI-02 配置中心 + KB-031 API 鉴权与配额——五类场景缺口补齐。

TDD 新规口径：等价类 / 边界 / 异常 / 并发 / 降级 五类场景逐列审计后，
本文件只覆盖既有 test_config_center.py / test_apikey_*.py 未覆盖的缺口：
- MAI-02：区间端点边界（min/max 恰好命中）、并发 set 版本号严格递增（竞态）、
  订阅回调内重入读写（重入死锁风险）、set 列表值深拷贝（共享态防篡改）、
  回滚到版本 0 边界、同 key 连续两次变更的单笔回滚、未注册 key 查版本号；
- KB-031：QuotaConfig 取值边界、轮换负宽限钳制、并发 verify 注册表一致性（竞态）、
  并发 check 精确放行数（限流原子性竞态）、calllog 分页/耗时钳制边界、
  Bearer 方案大小写等价类、401/429 Retry-After 头差异。

KPI 映射：MAI-02 → NFR-MAI-02（热更新生效 / 版本留痕完整率）；
KB-031 → 42901 限流判定（并发超发=0、拒绝不计数）、40101/40301 鉴权口径。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from foundation.apikey.calllog import ApiCallLogger
from foundation.apikey.dependency import api_key_auth, api_key_exception_handler
from foundation.apikey.keys import (
    ConsumerContext,
    KeyRegistry,
    QuotaConfig,
    RotatedKeyExpiredError,
    hash_key,
)
from foundation.apikey.ratelimit import SlidingWindowRateLimiter
from foundation.config import (
    ConfigCenter,
    ConfigSchema,
    ConfigValidationError,
    UnknownConfigKeyError,
)


# ---------------------------------------------------------------- MAI-02 配置中心


@pytest.fixture()
def center() -> ConfigCenter:
    """独立配置中心：一个非危险 int 项（含 min/max）+ 一个 json 项。"""
    c = ConfigCenter()
    c.register(ConfigSchema(
        scope="risk", key="limits.max_ops_per_day", vtype="int",
        default=150, minimum=30, maximum=400, desc="单账号每日操作量上限",
    ))
    c.register(ConfigSchema(
        scope="strategy", key="followup.cadence", vtype="json",
        default=[1, 3, 7, 14], desc="跟进节奏间隔天数序列",
    ))
    return c


class TestConfigSupplement:
    def test_set_at_exact_bounds_passes(self, center: ConfigCenter) -> None:
        """【边界】区间端点：恰好取 minimum=30 与 maximum=400 均放行（含端点口径）。

        KPI 映射：NFR-MAI-02 写校验（40002 只拒绝真正越界值，不误伤合法端点）。
        """
        center.set("risk.limits.max_ops_per_day", 30, operator="ops", reason="下界探活")
        assert center.get("risk.limits.max_ops_per_day") == 30
        center.set("risk.limits.max_ops_per_day", 400, operator="ops", reason="上界探活")
        assert center.get("risk.limits.max_ops_per_day") == 400
        # 端点外 1 步即拒绝（CR P3-b 收窄：越界 → ConfigValidationError(40002)，项目自定义异常类）
        with pytest.raises(ConfigValidationError) as excinfo:
            center.set("risk.limits.max_ops_per_day", 401, reason="越界")
        assert excinfo.value.code == 40002

    def test_concurrent_set_versions_strictly_increasing(self, center: ConfigCenter) -> None:
        """【并发】12 线程 × 5 次 set：全局序号 1..60 无一丢失或重复（RLock 串行化）。

        KPI 映射：NFR-MAI-02 变更留痕完整率（并发变更不丢版本、审计链无重号）。
        """
        errors: list[Exception] = []

        def produce(worker: int) -> None:
            for i in range(5):
                try:
                    center.set("risk.limits.max_ops_per_day", 30 + worker * 10 + i, reason=f"w{worker}")
                except Exception as exc:  # pragma: no cover - 出现即断言失败
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(produce, range(12)))
        assert errors == []
        records = center.history()
        assert len(records) == 60
        assert sorted(r.version for r in records) == list(range(1, 61))  # 序号唯一且连续
        assert center.get_version("risk.limits.max_ops_per_day") == 60
        assert center.snapshot().version == 60

    def test_callback_reentrant_read_write(self, center: ConfigCenter) -> None:
        """【并发】订阅回调内重入 get/set：不死锁、嵌套变更照常入历史并再推送（锁外派发）。

        KPI 映射：NFR-MAI-02 热更新订阅链路可用性（回调内联动配置读写为受支持形态）。
        """
        entered = threading.Event()
        nested_seen = threading.Event()

        class ReentrantSubscriber:
            def __init__(self) -> None:
                self.done = False

            def on_config_change(self, event: object) -> None:
                if self.done:
                    nested_seen.set()  # 嵌套变更的二次推送到达
                    return
                self.done = True
                entered.set()
                # 回调内重入：读其他 key + 写其他 key（若派发在锁内此处将死锁）
                assert center.get("strategy.followup.cadence") == [1, 3, 7, 14]
                center.set("strategy.followup.cadence", [2, 5], reason="回调内联动调整")

        subscriber = ReentrantSubscriber()
        center.subscribe(subscriber)
        center.set("risk.limits.max_ops_per_day", 120, reason="触发回调")
        assert entered.wait(timeout=5), "回调未执行（疑似死锁）"
        assert nested_seen.wait(timeout=5), "嵌套变更未再推送"
        assert center.get("strategy.followup.cadence") == [2, 5]
        actions = [(r.action, r.reason) for r in center.history()]
        assert ("update", "回调内联动调整") in actions

    def test_set_list_value_deep_copied(self, center: ConfigCenter) -> None:
        """【降级】set 传入可变列表：入库深拷贝——调用方事后改写入参不污染配置存储。

        KPI 映射：NFR-MAI-02 配置存储一致性（共享可变态防篡改）。
        """
        value = [1, 2, 3]
        center.set("strategy.followup.cadence", value, reason="初次设置")
        value.append(99)  # 入库后改写调用方入参
        assert center.get("strategy.followup.cadence") == [1, 2, 3]

    def test_rollback_to_zero_restores_defaults(self, center: ConfigCenter) -> None:
        """【边界】rollback_to(0)：目标为「从未变更」时点 → 全部层移除、读回注册表默认值。

        KPI 映射：NFR-MAI-02 一键回滚全覆盖（含回滚到初始态的最小目标版本）。
        """
        center.set("risk.limits.max_ops_per_day", 300, reason="第一次")
        center.set("strategy.followup.cadence", [9], reason="第二次")
        rolled = center.rollback_to(0, operator="admin", reason="全量清退")
        assert len(rolled) == 2
        assert all(r.action == "rollback" and r.layer_present_after is False for r in rolled)
        assert center.get("risk.limits.max_ops_per_day") == 150      # 注册表默认值
        assert center.get("strategy.followup.cadence") == [1, 3, 7, 14]

    def test_rollback_same_key_updated_twice_single_restore(self, center: ConfigCenter) -> None:
        """【边界】目标时点后同 key 连续两次变更：回滚仅产生一条记录、一步恢复到目标值。

        KPI 映射：NFR-MAI-02 回滚记录与审计链一致（不重复补偿）。
        """
        center.set("risk.limits.max_ops_per_day", 100, reason="目标值")   # v1
        target = center.snapshot().version
        center.set("risk.limits.max_ops_per_day", 200, reason="改错一")   # v2
        center.set("risk.limits.max_ops_per_day", 300, reason="改错二")   # v3
        rolled = center.rollback_to(target, operator="admin", reason="一键恢复")
        assert len(rolled) == 1
        assert rolled[0].new_value == 100
        assert center.get("risk.limits.max_ops_per_day") == 100
        key_actions = [r.action for r in center.history(key="limits.max_ops_per_day")]
        assert key_actions == ["update", "update", "update", "rollback"]

    def test_get_version_unregistered_key_raises(self, center: ConfigCenter) -> None:
        """【异常】get_version 查询未注册 key → UnknownConfigKeyError（fail fast 对齐 get/set）。

        KPI 映射：NFR-MAI-02 未注册 key 零容忍（拼写错配在读取面同样暴露）。
        """
        with pytest.raises(UnknownConfigKeyError):
            center.get_version("risk.not.exists")


# ---------------------------------------------------------------- KB-031 API 鉴权与配额


class TestApikeySupplement:
    def test_quota_config_bounds(self) -> None:
        """【边界】QuotaConfig 取值边界：qps∈[1,1000]、daily∈[1,1000000]，
        越界即 ValidationError（SR §7 全局取值范围的应用侧硬约束）。

        KPI 映射：42901 限流判定前置（配额配置非法值在登记期拒绝，不入注册表）。
        """
        for bad in ({"qps": 0}, {"qps": 1001}, {"daily": 0}, {"daily": 1_000_001}):
            with pytest.raises(ValidationError):
                QuotaConfig(**bad)
        edge = QuotaConfig(qps=1, daily=1)
        assert (edge.qps, edge.daily) == (1, 1)
        top = QuotaConfig(qps=1000, daily=1_000_000)
        assert (top.qps, top.daily) == (1000, 1_000_000)

    def test_rotate_negative_grace_clamped_to_immediate_expiry(self) -> None:
        """【边界】rotate(grace_seconds<0)：钳制为 0——旧 Key 立即失效（不允许负宽限后门）。

        KPI 映射：40101 吊销/轮换口径（宽限窗口不出现负值语义）。
        """
        registry = KeyRegistry()
        old = registry.issue("agent-core").api_key
        rotated = registry.rotate("agent-core", grace_seconds=-5.0)
        assert rotated.grace_seconds == 0.0
        assert rotated.old_key_valid_until is None
        registry.verify(rotated.api_key)                 # 新 Key 立即可用
        with pytest.raises(RotatedKeyExpiredError):      # 旧 Key 立即失效
            registry.verify(old)

    def test_concurrent_verify_keeps_registry_consistent(self) -> None:
        """【并发】16 线程并发 verify 同一 Key：全部通过、注册表行仍可按哈希定位
        （整行替换形态下并发鉴权不损坏注册表——last_used_at 丢更新属良性）。

        KPI 映射：40101 鉴权可用性（并发请求下注册表零损坏）。
        """
        registry = KeyRegistry()
        key = registry.issue("agent-core").api_key
        results: list[str] = []
        lock = threading.Lock()

        def verify() -> None:
            context = registry.verify(key, client_ip="10.0.0.1")
            with lock:
                results.append(context.consumer)

        threads = [threading.Thread(target=verify) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == ["agent-core"] * 16
        record = registry.get("agent-core")
        assert record is not None
        assert record.api_key_hash == hash_key(key)      # 哈希未被并发替换损坏
        registry.verify(key)                             # 并发后仍可正常鉴权

    def test_concurrent_check_exact_admission(self) -> None:
        """【并发】40 线程并发 check（qps=5）：恰 5 次放行、日配额计 5——判定+计数原子，
        并发下零超发（内存实现与生产 Lua 同口径）。

        KPI 映射：42901 限流（并发超发数=0、拒绝不计数）。
        """
        limiter = SlidingWindowRateLimiter()
        results: list[bool] = []
        lock = threading.Lock()

        def check() -> None:
            outcome = limiter.check("c1", qps=5, daily=100)
            with lock:
                results.append(outcome.allowed)

        threads = [threading.Thread(target=check) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 5                  # 恰好放行 qps 份
        assert results.count(False) == 35
        assert limiter.daily_quota.used("c1") == 5       # 被拒 35 次未消耗日配额
        assert limiter.window_count("c1") == 5           # 窗口内恰 5 条记录

    def test_calllog_page_and_latency_clamped(self) -> None:
        """【边界】调用日志：page<1 钳制为第 1 页、负 latency 钳制为 0（入参防御）。

        KPI 映射：调用日志可查（检索口径稳定，畸形入参不越界）。
        """
        logger = ApiCallLogger()
        for i in range(3):
            logger.record(trace_id=f"t-{i}", consumer="c1", latency_ms=-5)
        entry = logger.entries()[0]
        assert entry.latency_ms == 0                     # 负耗时钳制为 0
        page_zero = logger.query(page=0, page_size=2)
        assert [e.trace_id for e in page_zero] == ["t-0", "t-1"]   # page=0 视为第 1 页
        negative = logger.query(page=-3, page_size=2)
        assert [e.trace_id for e in negative] == ["t-0", "t-1"]

    def test_bearer_scheme_case_insensitive(self) -> None:
        """【等价类】Authorization 方案名大小写不敏感：bearer/BEARER 与 Bearer 等价放行。

        KPI 映射：40101 防探测口径（方案名形态差异不产生行为分歧）。
        """
        registry, limiter = KeyRegistry(), SlidingWindowRateLimiter()
        key = registry.issue("agent-core").api_key
        app = FastAPI()
        app.add_exception_handler(HTTPException, api_key_exception_handler)
        auth = api_key_auth(registry, limiter)

        @app.post("/api/v1/knowledge/search")
        async def search(consumer: ConsumerContext = Depends(auth)) -> dict:
            return {"ok": True, "consumer": consumer.consumer}

        client = TestClient(app)
        for scheme in ("bearer", "BEARER", "Bearer"):
            resp = client.post("/api/v1/knowledge/search", headers={"Authorization": f"{scheme} {key}"})
            assert resp.status_code == 200, f"scheme={scheme} 应放行"
        # 方案名错写（非 Bearer 家族）仍 401
        assert client.post(
            "/api/v1/knowledge/search", headers={"Authorization": f"Token {key}"}
        ).status_code == 401

    def test_retry_after_header_only_on_429(self) -> None:
        """【异常/边界】Retry-After 头仅 429 携带：401/403 响应不得携带（语义精确）。

        KPI 映射：42901 Retry-After 可用（客户端按头等待）；40101 不误导重试节奏。
        """
        registry = KeyRegistry()
        tight = registry.issue("tight", quota=QuotaConfig(qps=1, daily=100)).api_key
        app = FastAPI()
        app.add_exception_handler(HTTPException, api_key_exception_handler)
        auth = api_key_auth(registry, SlidingWindowRateLimiter())

        @app.post("/api/v1/knowledge/search")
        async def search(consumer: ConsumerContext = Depends(auth)) -> dict:
            return {"ok": True}

        client = TestClient(app)
        assert client.post(
            "/api/v1/knowledge/search", headers={"Authorization": f"Bearer {tight}"}
        ).status_code == 200
        limited = client.post("/api/v1/knowledge/search", headers={"Authorization": f"Bearer {tight}"})
        assert limited.status_code == 429
        assert "Retry-After" in limited.headers and int(limited.headers["Retry-After"]) >= 1
        unauth = client.post("/api/v1/knowledge/search")  # 无 Key → 401
        assert unauth.status_code == 401
        assert "Retry-After" not in unauth.headers        # 401 不携带重试语义头
