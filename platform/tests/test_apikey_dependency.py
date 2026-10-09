"""知识产权归甲方所有（COM-IP-01）

FR-KB-031 FastAPI 鉴权依赖集成测试（TestClient：受保护端点各状态码与错误体格式）。

覆盖（SR-KB-031 §9 集成/验收 ①② + 04-接口设计 §1.3 信封）：
- 401：无 Key / 错误 Key / 已吊销 Key → HTTP 401 + code=40101，错误体 {code,message,data,trace_id}；
- 403：来源 IP 不在白名单 → 403 + 40301（X-Forwarded-For 信任开关两形态）；
- 429：QPS 与日配额超限 → 429 + 42901 + Retry-After 头（拒绝不消耗日配额）；
- 200：正确 Key → 上下文注入路由（consumer/配额）+ trace_id 透传（X-Trace-Id）；
- 调用日志联动：成功/401/429 每次判定各落一行（含被拒，consumer 归因口径）。
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from platform.core.apikey.calllog import UNKNOWN_CONSUMER, ApiCallLogger
from platform.core.apikey.dependency import api_key_auth, api_key_exception_handler
from platform.core.apikey.keys import ConsumerContext, KeyRegistry, QuotaConfig
from platform.core.apikey.ratelimit import SlidingWindowRateLimiter

TEST_CLIENT_IP = "testclient"  # TestClient 默认对端 host（IP 白名单用例的「来源 IP」）


def build_app(
    registry: KeyRegistry,
    limiter: SlidingWindowRateLimiter,
    logger: ApiCallLogger | None = None,
    *,
    trust_forwarded: bool = False,
) -> TestClient:
    """装配受保护应用：统一错误信封 + /api/v1/knowledge/search 受保护端点。"""
    app = FastAPI()
    app.add_exception_handler(HTTPException, api_key_exception_handler)
    auth = api_key_auth(registry, limiter, call_logger=logger, trust_forwarded=trust_forwarded)

    @app.post("/api/v1/knowledge/search")
    async def search(request: Request, consumer: ConsumerContext = Depends(auth)) -> dict:
        # 统一成功信封（04 §1.3）：trace_id 由依赖写入 request.state（X-Trace-Id 透传）
        return {
            "code": 0,
            "message": "ok",
            "data": {"consumer": consumer.consumer, "qps": consumer.qps, "daily": consumer.daily},
            "trace_id": request.state.trace_id,
        }

    @app.get("/api/v1/_plain-error")
    async def plain_error() -> dict:
        raise HTTPException(status_code=400, detail="参数校验失败")  # 兜底映射形态观测

    return TestClient(app)


@pytest.fixture()
def components() -> tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]:
    """默认组件：内存注册表 + 内存限流器 + 调用日志（每次用例全新隔离）。"""
    return KeyRegistry(), SlidingWindowRateLimiter(), ApiCallLogger()


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def issue_key(registry: KeyRegistry, consumer: str = "agent-core", **quota: int) -> str:
    """签发并返回明文密钥（配额缺省 qps=1000/daily=1000，避免干扰非限流用例）。"""
    return registry.issue(
        consumer, quota=QuotaConfig(**{"qps": 1000, "daily": 1000, **quota})
    ).api_key


# --------------------------------------------------------------------------- 401（SR §9-①）


class TestUnauthorized:
    def test_missing_key(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """无 Key → 401 + 40101，错误体四键对齐 04 §1.3。"""
        registry, limiter, logger = components
        client = build_app(registry, limiter, logger)
        resp = client.post("/api/v1/knowledge/search")
        assert resp.status_code == 401
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"}
        assert body["code"] == 40101 and body["data"] is None and body["trace_id"]
        assert body["message"] == "API Key 无效或缺失"

    def test_malformed_scheme(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """非 Bearer 方案 → 401 + 40101（与缺失同文案，防探测）。"""
        registry, limiter, _ = components
        client = build_app(registry, limiter)
        resp = client.post("/api/v1/knowledge/search", headers={"Authorization": "Basic kbk_xxx"})
        assert resp.status_code == 401 and resp.json()["code"] == 40101

    def test_wrong_key(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """错误 Key → 401 + 40101（SR §9-①）。"""
        registry, limiter, _ = components
        issue_key(registry)
        client = build_app(registry, limiter)
        resp = client.post("/api/v1/knowledge/search", headers=bearer("kbk_wrong-key-not-exists"))
        assert resp.status_code == 401 and resp.json()["code"] == 40101

    def test_revoked_key(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """已吊销 Key → 401 + 40101（泄露应急秒级生效，SR §2.2）。"""
        registry, limiter, _ = components
        key = issue_key(registry)
        registry.revoke("agent-core", operator="ops", reason="泄露应急演练")
        client = build_app(registry, limiter)
        resp = client.post("/api/v1/knowledge/search", headers=bearer(key))
        assert resp.status_code == 401 and resp.json()["code"] == 40101


# --------------------------------------------------------------------------- 200（上下文注入与追踪）


class TestSuccess:
    def test_valid_key_injects_context(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """正确 Key → 200 信封 code=0，路由拿到消费方上下文（consumer/配额）。"""
        registry, limiter, _ = components
        key = issue_key(registry, qps=7, daily=1234)
        client = build_app(registry, limiter)
        resp = client.post("/api/v1/knowledge/search", headers=bearer(key))
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0 and body["message"] == "ok"
        assert body["data"] == {"consumer": "agent-core", "qps": 7, "daily": 1234}
        assert body["trace_id"]

    def test_trace_id_passthrough(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """X-Trace-Id 透传：响应与调用日志使用同一 trace_id（04 §1.4 全链路）。"""
        registry, limiter, logger = components
        key = issue_key(registry)
        client = build_app(registry, limiter, logger)
        resp = client.post(
            "/api/v1/knowledge/search", headers={**bearer(key), "X-Trace-Id": "trace-abc-123"}
        )
        assert resp.json()["trace_id"] == "trace-abc-123"
        assert logger.query(consumer="agent-core")[0].trace_id == "trace-abc-123"


# --------------------------------------------------------------------------- 403（IP 白名单）


class TestIpWhitelist:
    def test_denied_ip_40301(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """来源 IP 不在白名单 → 403 + 40301（SR §9 白名单用例的 HTTP 形态）。"""
        registry, limiter, _ = components
        key = registry.issue("agent-core", quota=QuotaConfig(qps=10, daily=10),
                             ip_whitelist=["10.0.0.9"]).api_key
        client = build_app(registry, limiter)  # TestClient 对端 host=testclient，不在名单
        resp = client.post("/api/v1/knowledge/search", headers=bearer(key))
        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == 40301 and set(body) == {"code", "message", "data", "trace_id"}

    def test_allowed_ip_passes(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """来源 IP 在白名单 → 200。"""
        registry, limiter, _ = components
        key = registry.issue("agent-core", quota=QuotaConfig(qps=10, daily=10),
                             ip_whitelist=[TEST_CLIENT_IP, "10.0.0.9"]).api_key
        client = build_app(registry, limiter)
        assert client.post("/api/v1/knowledge/search", headers=bearer(key)).status_code == 200

    def test_forwarded_for_trusted(self) -> None:
        """trust_forwarded：取 X-Forwarded-For 首段做白名单判定（Nginx 前置形态，SR §2.1）。"""
        registry, limiter = KeyRegistry(), SlidingWindowRateLimiter()
        key = registry.issue("agent-core", quota=QuotaConfig(qps=10, daily=10),
                             ip_whitelist=["203.0.113.7"]).api_key
        client = build_app(registry, limiter, trust_forwarded=True)
        resp = client.post("/api/v1/knowledge/search", headers={
            **bearer(key), "X-Forwarded-For": "203.0.113.7, 10.0.0.1",
        })
        assert resp.status_code == 200
        # 无 X-Forwarded-For 时回退对端 host（testclient）→ 仍不在白名单 → 403
        resp2 = client.post("/api/v1/knowledge/search", headers=bearer(key))
        assert resp2.status_code == 403


# --------------------------------------------------------------------------- 429（SR §9-②）


class TestRateLimited:
    def test_qps_exceeded_with_retry_after(
        self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]
    ) -> None:
        """QPS 超限 → 429 + 42901 + Retry-After（窗口 1s → 恒 1；SR §9-②）。"""
        registry, limiter, _ = components
        key = issue_key(registry, qps=1, daily=1000)
        client = build_app(registry, limiter)
        assert client.post("/api/v1/knowledge/search", headers=bearer(key)).status_code == 200
        resp = client.post("/api/v1/knowledge/search", headers=bearer(key))
        assert resp.status_code == 429
        body = resp.json()
        assert body["code"] == 42901 and set(body) == {"code", "message", "data", "trace_id"}
        assert "触发限流" in body["message"]
        assert int(resp.headers["Retry-After"]) == 1  # 1s 窗口内最老记录即将滑出

    def test_daily_exceeded_with_retry_after(
        self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]
    ) -> None:
        """日配额超限 → 429 + 42901 + Retry-After（=至次日零点秒数量级，恒 ≥1）。"""
        registry, limiter, _ = components
        key = issue_key(registry, qps=1000, daily=2)
        client = build_app(registry, limiter)
        for _ in range(2):
            assert client.post("/api/v1/knowledge/search", headers=bearer(key)).status_code == 200
        resp = client.post("/api/v1/knowledge/search", headers=bearer(key))
        assert resp.status_code == 429 and resp.json()["code"] == 42901
        assert int(resp.headers["Retry-After"]) >= 1

    def test_rejection_does_not_consume_daily(
        self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]
    ) -> None:
        """QPS 拒绝不消耗日配额（SR §9：拒绝不计数）。"""
        registry, limiter, _ = components
        key = issue_key(registry, qps=1, daily=10)
        client = build_app(registry, limiter)
        client.post("/api/v1/knowledge/search", headers=bearer(key))          # 唯一放行
        for _ in range(3):
            client.post("/api/v1/knowledge/search", headers=bearer(key))      # QPS 拒绝 ×3
        assert limiter.daily_quota.used("agent-core") == 1


# --------------------------------------------------------------------------- 调用日志联动（SR §6.4）


class TestCallLogIntegration:
    def test_all_outcomes_logged_with_trace_binding(
        self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]
    ) -> None:
        """成功/401/429 每次判定各落一行；trace_id 与响应信封一致；被拒归因口径正确。"""
        registry, limiter, logger = components
        ok_key = issue_key(registry, consumer="agent-core", qps=1000, daily=1000)
        tight_key = issue_key(registry, consumer="tight", qps=1, daily=1000)
        client = build_app(registry, limiter, logger)

        ok_resp = client.post("/api/v1/knowledge/search", headers=bearer(ok_key))
        unauth_resp = client.post("/api/v1/knowledge/search")                 # 无 Key
        client.post("/api/v1/knowledge/search", headers=bearer(tight_key))    # 200
        limited_resp = client.post("/api/v1/knowledge/search", headers=bearer(tight_key))  # 429

        entries = logger.entries()
        assert len(entries) == 4
        assert [(e.http_code, e.code) for e in entries] == [
            (200, 0), (401, 40101), (200, 0), (429, 42901),
        ]
        assert [e.consumer for e in entries] == ["agent-core", UNKNOWN_CONSUMER, "tight", "tight"]
        assert unauth_resp.json()["trace_id"] == entries[1].trace_id          # 信封↔日志同 trace
        assert limited_resp.json()["trace_id"] == entries[3].trace_id
        for entry in entries:
            assert entry.method == "POST" and entry.path == "/api/v1/knowledge/search"
            assert entry.client_ip == TEST_CLIENT_IP
            assert entry.latency_ms >= 0

    def test_query_rejected_by_code(self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]) -> None:
        """被拒记录可按结果码检索（验收口径 3 的数据出口）。"""
        registry, limiter, logger = components
        client = build_app(registry, limiter, logger)
        client.post("/api/v1/knowledge/search")                                # 401
        assert [e.code for e in logger.query(code=40101)] == [40101]


# --------------------------------------------------------------------------- 信封兜底


class TestEnvelopeFallback:
    def test_plain_http_exception_keeps_envelope(
        self, components: tuple[KeyRegistry, SlidingWindowRateLimiter, ApiCallLogger]
    ) -> None:
        """非结构化 HTTPException 仍输出同构信封（全路由格式一致）。"""
        registry, limiter, _ = components
        client = build_app(registry, limiter)
        resp = client.get("/api/v1/_plain-error")
        assert resp.status_code == 400
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"}
        assert body["code"] == 50001 and body["message"] == "参数校验失败"
