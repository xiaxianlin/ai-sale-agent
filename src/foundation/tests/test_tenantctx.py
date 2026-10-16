# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户上下文薄中间件测试（CR 遗留 R2：四业务服务挂载验证）。

覆盖（开发规范 §9.1 五类）：
- 等价类：单租户缺省注入默认租户 1；多租户开+可信头 → 头值生效；
- 边界：非法头值（非数字/0/负数）回落默认 1；
- 异常：无上下文 get_tenant_id fail-closed；
- 并发：ContextVar 请求间隔离（两请求不同租户互不串扰）；
- 降级：后台/测试无中间件 → current_tenant_or_default 回落 1。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from foundation.core.tenantctx import (  # noqa: E402
    DEFAULT_TENANT_ID,
    TenantContextError,
    TenantContextLiteMiddleware,
    current_tenant_or_default,
    get_tenant_id,
)


async def _noop_app(scope, receive, send) -> None:  # 可 await 的空 ASGI 应用
    return None


def _run_asgi(mw: TenantContextLiteMiddleware, headers: list[tuple[bytes, bytes]] | None = None):
    """纯 ASGI 冒烟：中间件包裹的 app 记录进入时的租户上下文。"""
    seen: dict[str, int] = {}

    async def inner(scope, receive, send):
        seen["tenant"] = get_tenant_id()

    import asyncio

    wrapped = TenantContextLiteMiddleware(
        inner,
        multi_tenancy_enabled=mw._multi,  # noqa: SLF001 —— 复用被测实例的开关形态
    )
    scope = {"type": "http", "headers": headers or [], "method": "GET", "path": "/"}
    asyncio.run(wrapped(scope, None, None))  # type: ignore[arg-type]
    return seen


def test_单租户缺省注入默认租户1() -> None:
    """等价类：multi_tenancy 关闭 → 请求内 get_tenant_id()==1。"""
    mw = TenantContextLiteMiddleware(_noop_app)
    seen = _run_asgi(mw)
    assert seen["tenant"] == DEFAULT_TENANT_ID


def test_多租户可信头生效() -> None:
    """等价类：开启多租户 + 网关可信头 X-Tenant-Id: 7 → 请求内租户为 7。"""
    mw = TenantContextLiteMiddleware(_noop_app, multi_tenancy_enabled=True)
    seen = _run_asgi(mw, headers=[(b"x-tenant-id", b"7")])
    assert seen["tenant"] == 7


@pytest.mark.parametrize("bad", [b"abc", b"0", b"-3", b""])
def test_非法头值回落默认1(bad: bytes) -> None:
    """边界：非数字/0/负数/空 → 回落默认租户（合法性由 saas 层闸门负责）。"""
    mw = TenantContextLiteMiddleware(_noop_app, multi_tenancy_enabled=True)
    seen = _run_asgi(mw, headers=[(b"x-tenant-id", bad)])
    assert seen["tenant"] == DEFAULT_TENANT_ID


def test_无上下文_fail_closed() -> None:
    """异常：中间件覆盖范围外 get_tenant_id 抛 TenantContextError。"""
    with pytest.raises(TenantContextError):
        get_tenant_id()


def test_宽松读取回落() -> None:
    """降级：后台任务无上下文 → current_tenant_or_default()==1。"""
    assert current_tenant_or_default() == DEFAULT_TENANT_ID


def test_请求间上下文隔离() -> None:
    """并发：连续两请求不同租户，第二请求读不到第一请求的值（ContextVar 复位）。"""
    mw7 = TenantContextLiteMiddleware(_noop_app, multi_tenancy_enabled=True)
    seen1 = _run_asgi(mw7, headers=[(b"x-tenant-id", b"7")])
    mw_off = TenantContextLiteMiddleware(_noop_app)
    seen2 = _run_asgi(mw_off)
    assert seen1["tenant"] == 7 and seen2["tenant"] == 1
    # 复位后无残留
    with pytest.raises(TenantContextError):
        get_tenant_id()


def test_线程隔离() -> None:
    """并发：另一线程无上下文（ContextVar 线程语义）不串扰。"""
    mw = TenantContextLiteMiddleware(_noop_app)
    _run_asgi(mw)
    errors: list[str] = []

    def other() -> None:
        try:
            get_tenant_id()
        except TenantContextError:
            errors.append("isolated")

    th = threading.Thread(target=other)
    th.start()
    th.join()
    assert errors == ["isolated"]
