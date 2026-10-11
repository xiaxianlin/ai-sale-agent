# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""site-service 测试共享夹具：可拨时钟 / 内存桩检索 / 门控桩（FR-SITE-001/002/003/007 通用）。"""
from __future__ import annotations

from typing import Any

from services.site.core.session.service import (
    MemorySdkKeyStore,
    MemorySessionStore,
    SessionService,
    VisitorTokenCodec,
)

#: 演示密钥与来源白名单（与 app.py 工厂缺省一致）
DEMO_KEY = "pk_site_demo_key"
DEMO_ORIGIN = "https://shop.example.com"


class FakeClock:
    """可拨时钟（确定性时间：过期/冷却/留存边界用例零 sleep）。"""

    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class StaticGate:
    """consent 门控桩（追踪/邀聊用例隔离 ConsentManager 细节）。"""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.checks = 0

    def allows_tracking(self, visitor_uid: str) -> bool:
        self.checks += 1
        return self.allow

    # 邀聊引擎读 allows_invite（三态口径与 tracking 一致）
    def allows_invite(self, visitor_uid: str) -> bool:
        return self.allows_tracking(visitor_uid)


class FakeSearch:
    """FR-KB-030 契约检索桩：固定返回预置 chunk（记录调用参数供断言）。"""

    def __init__(self, chunks: list[dict[str, Any]] | None = None) -> None:
        self.chunks = list(chunks or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        query: str,
        kb_types: list[str] | None = None,
        filters: Any = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        self.calls.append(
            {"query": query, "kb_types": kb_types, "filters": filters, "top_k": top_k}
        )
        results = [
            {
                "chunk_id": i + 1,
                "score": round(0.9 - 0.1 * i, 4),
                "title": c["title"],
                "text": c["text"],
            }
            for i, c in enumerate(self.chunks[:top_k])
        ]
        return {"results": results}


class RecordingSink:
    """通用记录器（画像重建/事件追加等副作用观察）。"""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def __call__(self, *args: Any) -> Any:
        self.items.append(args if len(args) != 1 else args[0])
        return args[0] if len(args) == 1 else args


def make_key_store(
    *,
    key: str = DEMO_KEY,
    origins: tuple[str, ...] = (DEMO_ORIGIN,),
    widget_config: dict[str, Any] | None = None,
    status: str = "active",
) -> MemorySdkKeyStore:
    """构造含单把演示密钥的内存密钥库。"""
    store = MemorySdkKeyStore()
    store.register(key, origins=origins, widget_config=widget_config or {}, status=status)
    return store


def make_session_service(
    clock: FakeClock | None = None,
    *,
    keys: MemorySdkKeyStore | None = None,
    consent_required: bool = True,
    ttl_s: float = 86400.0,
) -> tuple[SessionService, MemorySessionStore]:
    """构造会话服务（可拨时钟 + 演示密钥库），返回 (服务, 会话存储)。"""
    clock = clock or FakeClock()
    sessions = MemorySessionStore()
    codec = VisitorTokenCodec("test-secret", ttl_s=ttl_s, clock=clock)
    service = SessionService(
        keys or make_key_store(),
        sessions,
        codec,
        consent_required=consent_required,
        clock=clock,
    )
    return service, sessions


def make_session(
    service: SessionService | None = None,
    *,
    lang: str = "en",
    origin: str = DEMO_ORIGIN,
) -> Any:
    """直接建一个访客会话记录（默认参数快路径）。"""
    service = service or make_session_service()[0]
    return service.create_session(
        key=DEMO_KEY, origin=origin, page="https://shop.example.com/", lang=lang
    )
