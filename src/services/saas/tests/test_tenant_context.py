# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户上下文单元测试（FR-SAAS-001 §6.1/§9；TDD 阶段①②，五类场景齐备）。

覆盖（SR-SAAS-001 §9 + 本批用例清单）：
- 等价类：JWT 声明解析注入 / 白标域名兜底 / 多租户关闭回落默认租户 1；
- 边界：未绑定域名 401、suspended/expired 闸门 403；
- 异常：伪造 X-Tenant-Id 头与 tenant_id 查询参数无效、get_tenant_id 缺上下文 fail-closed；
- 并发：两请求不同租户经 ContextVar 隔离互不串号、请求结束复位；
- API 形态断言：租户域全部端点签名无 tenant_id 形参（业务代码无法显式传参绕过）。
验收口径映射：SRS §16.6「多租户隔离测试通过」（API 层，联动 SR-NFR-SEC-07 §6.1）。

实现方案：docs/07-实现方案/SAAS/SR-SAAS-001-多租户架构.md §5/§6.1/§9
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.testclient import TestClient

from services.saas.api.saas_api import SAAS_TENANT_PREFIX, create_saas_app
from services.saas.core.tenant.context import (
    DEFAULT_TENANT_ID,
    TenantContextError,
    TenantContextMiddleware,
    get_tenant_id,
    peek_tenant_id,
)

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


class TenantRegistryShim:
    """测试桩：最小注册表形态（仅承载 get/status，供中间件闸门消费）。"""

    def __init__(self, *, clock) -> None:
        self._clock = clock
        self._tenants: dict[int, dict[str, str]] = {
            DEFAULT_TENANT_ID: {"status": "active", "name": "甲方本体", "plan": "enterprise"},
        }
        self._next_id = DEFAULT_TENANT_ID + 1

    def add(self, status: str = "active") -> int:
        tid = self._next_id
        self._next_id += 1
        self._tenants[tid] = {"status": status, "name": f"tenant-{tid}", "plan": "standard"}
        return tid

    def get(self, tid: int):
        row = self._tenants.get(tid)
        return None if row is None else type("T", (), {"id": tid, **row})()


def build_env(multi_tenancy: bool = True, host_resolver=None):
    """标准装配：注册表（租户 1=甲方本体 + 若干测试租户）+ 应用 + 组件字典。"""
    clock = lambda: NOW  # noqa: E731 - 冻结时钟（注入式，对齐 SR §6.4 时钟口径）
    registry = TenantRegistryShim(clock=clock)
    app, comps = create_saas_app(
        registry=registry,
        clock=clock,
        multi_tenancy_enabled=multi_tenancy,
        host_resolver=host_resolver,
    )
    return TestClient(app), comps, registry


def _asgi_request(path: str, token: str | None = None) -> Request:
    """构造最小 ASGI scope 的 Request（中间件直连并发测试用）。"""
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers,
            "query_string": b"",
        }
    )


def _direct_middleware(registry, comps) -> TenantContextMiddleware:
    """从装配组件取中间件实例（绕过 HTTP 栈直连 dispatch，供并发/复位用例）。"""
    app = FastAPI()
    return TenantContextMiddleware(
        app,
        registry,
        comps["codec"],
        clock=comps["clock"],
    )


# --------------------------------------------------------------------------- 等价类


def test_middleware_jwt_claims_inject_tenant() -> None:
    """中间件解析 JWT 声明注入租户：A 令牌 → whoami 返回 A 租户 id。"""
    client, comps, registry = build_env()
    tid_a = registry.add()
    token_a = comps["ops"].issue_tenant_token(tid_a, sub="user-a")
    body = client.get(
        f"{SAAS_TENANT_PREFIX}/whoami",
        headers={"Authorization": f"Bearer {token_a}"},
    ).json()
    assert body["code"] == 0 and body["data"]["tenant_id"] == tid_a


def test_middleware_host_header_fallback_resolution() -> None:
    """解析优先级 2：无令牌时按白标域名兜底解析租户（SR-SAAS-001 §6.1）。"""
    mapping = {"b.example.com": 7}
    client, comps, registry = build_env(host_resolver=lambda host: mapping.get(host))
    while registry._next_id <= 7:  # 预置 id=7 的活跃租户（域名映射目标）
        registry.add()
    body = client.get(
        f"{SAAS_TENANT_PREFIX}/whoami", headers={"Host": "b.example.com"}
    ).json()
    assert body["data"]["tenant_id"] == 7


def test_middleware_default_tenant_when_multi_tenancy_disabled() -> None:
    """多租户开关关闭：无凭证请求注入默认租户 1（P0-P4 单租户向前兼容，SR §7）。"""
    client, comps, _ = build_env(multi_tenancy=False)
    body = client.get(f"{SAAS_TENANT_PREFIX}/whoami").json()
    assert body["data"]["tenant_id"] == DEFAULT_TENANT_ID == 1


# --------------------------------------------------------------------------- 边界


def test_middleware_missing_credentials_rejected_401() -> None:
    """无凭证且多租户开启 → 401 TENANT_UNRESOLVED（信封四键齐全，data 为 None）。"""
    client, _, _ = build_env()
    resp = client.get(f"{SAAS_TENANT_PREFIX}/whoami")
    assert resp.status_code == 401
    body = resp.json()
    assert set(body) == {"code", "message", "data", "trace_id"}
    assert body["code"] == 40101 and body["data"] is None


def test_middleware_unknown_host_rejected() -> None:
    """域名未绑定 → 401（fail-closed：宁可失败，绝不落到默认租户串数据）。"""
    client, _, _ = build_env(host_resolver=lambda host: None)
    resp = client.get(f"{SAAS_TENANT_PREFIX}/whoami", headers={"Host": "stranger.example.com"})
    assert resp.status_code == 401 and resp.json()["code"] == 40101


def test_middleware_suspended_tenant_gated_403() -> None:
    """suspended 租户令牌 → 403 TENANT_INACTIVE（40302，闸门统一拦截）。"""
    client, comps, registry = build_env()
    tid = registry.add(status="suspended")
    token = comps["ops"].issue_tenant_token(tid, sub="u")
    resp = client.get(
        f"{SAAS_TENANT_PREFIX}/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403 and resp.json()["code"] == 40302
    assert "suspended" in resp.json()["message"]


def test_middleware_expired_tenant_gated_403() -> None:
    """expired 租户令牌 → 403 TENANT_INACTIVE。"""
    client, comps, registry = build_env()
    tid = registry.add(status="expired")
    token = comps["ops"].issue_tenant_token(tid, sub="u")
    resp = client.get(
        f"{SAAS_TENANT_PREFIX}/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403 and resp.json()["code"] == 40302


# --------------------------------------------------------------------------- 异常（伪造拒绝）


def test_forged_header_tenant_id_ignored() -> None:
    """伪造 X-Tenant-Id 头无效：租户边界仅取自凭证/域名，请求头不参与解析。"""
    client, comps, registry = build_env()
    tid_a, tid_b = registry.add(), registry.add()
    token_a = comps["ops"].issue_tenant_token(tid_a, sub="a")
    body = client.get(
        f"{SAAS_TENANT_PREFIX}/whoami",
        headers={"Authorization": f"Bearer {token_a}", "X-Tenant-Id": str(tid_b)},
    ).json()
    assert body["data"]["tenant_id"] == tid_a


def test_forged_query_param_tenant_id_ignored() -> None:
    """伪造 tenant_id 查询参数无效（NFR-SEC-07 §6.3 上下文逃逸用例 API 形态）。"""
    client, comps, registry = build_env()
    tid_a, tid_b = registry.add(), registry.add()
    token_a = comps["ops"].issue_tenant_token(tid_a, sub="a")
    body = client.get(
        f"{SAAS_TENANT_PREFIX}/whoami",
        params={"tenant_id": str(tid_b)},
        headers={"Authorization": f"Bearer {token_a}"},
    ).json()
    assert body["data"]["tenant_id"] == tid_a


def test_get_tenant_id_fail_closed_without_context() -> None:
    """缺上下文直接抛错（fail-closed）：绝不落到默认租户造成串数据（SR §6.1）。"""
    with pytest.raises(TenantContextError, match="租户上下文缺失"):
        get_tenant_id()


# --------------------------------------------------------------------------- 并发


def test_context_reset_after_request() -> None:
    """请求结束上下文复位（finally reset）：dispatch 返回后 ContextVar 为空。"""

    async def scenario() -> None:
        clock = lambda: NOW  # noqa: E731
        registry = TenantRegistryShim(clock=clock)
        tid = registry.add()
        _, comps = create_saas_app(registry=registry, clock=clock)
        middleware = _direct_middleware(registry, comps)
        token = comps["ops"].issue_tenant_token(tid, sub="u")

        async def call_next(request: Request) -> JSONResponse:
            return JSONResponse({"tenant": get_tenant_id()})

        resp = await middleware.dispatch(
            _asgi_request(f"{SAAS_TENANT_PREFIX}/whoami", token), call_next
        )
        assert json.loads(resp.body)["tenant"] == tid
        assert peek_tenant_id() is None  # 复位完成，防协程复用串租户

    asyncio.run(scenario())


def test_concurrent_requests_no_context_bleed() -> None:
    """并发两请求不同租户：ContextVar 按协程隔离，互不串号（SR §9 连接复用同源口径）。"""

    async def scenario() -> None:
        clock = lambda: NOW  # noqa: E731
        registry = TenantRegistryShim(clock=clock)
        tid_a, tid_b = registry.add(), registry.add()
        _, comps = create_saas_app(registry=registry, clock=clock)
        middleware = _direct_middleware(registry, comps)
        token_a = comps["ops"].issue_tenant_token(tid_a, sub="a")
        token_b = comps["ops"].issue_tenant_token(tid_b, sub="b")

        async def call_next(request: Request) -> JSONResponse:
            await asyncio.sleep(0.001)  # 制造交叠窗口
            return JSONResponse({"tenant": get_tenant_id()})

        r1, r2 = await asyncio.gather(
            middleware.dispatch(
                _asgi_request(f"{SAAS_TENANT_PREFIX}/whoami", token_a), call_next
            ),
            middleware.dispatch(
                _asgi_request(f"{SAAS_TENANT_PREFIX}/whoami", token_b), call_next
            ),
        )
        got = {json.loads(r1.body)["tenant"], json.loads(r2.body)["tenant"]}
        assert got == {tid_a, tid_b}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- API 形态断言


def test_api_surface_no_tenant_id_parameter() -> None:
    """租户域全部端点签名无 tenant_id 形参：API 形态上杜绝显式传参绕过上下文。

    （SR-SAAS-001 关键决策 2：业务代码禁止显式传 tenant_id；本断言把该约束固化为
    可回归的接口形态检查——端点只能经 ContextVar 取租户。）
    """
    clock = lambda: NOW  # noqa: E731
    app, _ = create_saas_app(registry=TenantRegistryShim(clock=clock), clock=clock)
    checked = 0
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(SAAS_TENANT_PREFIX):
            params = inspect.signature(route.endpoint).parameters
            assert "tenant_id" not in params, f"端点 {route.path} 声明了 tenant_id 形参"
            checked += 1
    assert checked >= 5  # whoami + 知识三项 + 对象/缓存，确保断言真实覆盖到端点
