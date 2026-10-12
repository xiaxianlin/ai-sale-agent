# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""site-service 应用工厂（04-接口设计 §1 通用规范；装配层，全部组件鸭子类型注入）。

create_site_app 组装独立站全链（demo 内存实现缺省可用；生产替换 PG/Redis/OSS，协议不变）：

    SessionService（visitor token / Origin 白名单，FR-SITE-001）
      + EventIngest / ProfileBuilder / ContextInjector（FR-SITE-002）
      + SiteChatService / InviteEngine / LeadCapture（FR-SITE-003，llm/search_fn/hint_fn 注入）
      + ConsentManager / DsrService（FR-SITE-007，同时充当行为与邀聊门控）
      → 三组路由（sdk_api / tracking_api / privacy_api）

统一信封与错误码（04 §1.3）：成功 code=0；40001/40101/40301/40302/40401/42901/50002
（后两族为本服务增补码，SR-SITE-002/007），映射见 core/errors.CODE_HTTP。

密钥注入（CR P0-5 修复）：secret → KB_SITE_TOKEN_SECRET、sdk_key → KB_SITE_SDK_KEY
（参数 → 环境变量 → 启动失败 SiteConfigError；无缺省硬编码值，NFR-SEC-04）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from services.site.core.conversation.service import (
    FakeSiteLlm,
    InviteEngine,
    LeadCapture,
    MemoryConversationRepo,
    MemoryCustomerWriter,
    MemoryGapRecorder,
    MemoryInviteLogStore,
    MemoryLeadStore,
    SiteChatService,
)
from services.site.core.errors import CODE_HTTP, SiteServiceError, resolve_site_secret
from services.site.core.privacy.service import (
    ConsentManager,
    DsrService,
    MemoryConsentStore,
    MemoryDsrTicketStore,
    MemoryMailJobStore,
    MemoryPrivacyData,
)
from services.site.core.session.service import (
    DEFAULT_LANG_SUPPORT,
    MemorySdkKeyStore,
    MemorySessionStore,
    SessionService,
    VisitorTokenCodec,
)
from services.site.core.tracking.service import (
    ContextInjector,
    EventIngest,
    MemoryAuditTrail,
    MemoryEventStore,
    MemoryProfileStore,
    ProfileBuilder,
    RetentionCleaner,
)
from services.site.core.session.service import SiteSessionRecord

from .common import envelope
from .privacy_api import router as privacy_router
from .sdk_api import router as sdk_router
from .tracking_api import router as tracking_router


def install_site_error_handlers(app: FastAPI) -> FastAPI:
    """统一信封错误处理器：业务错误按码表映射 HTTP；入参校验失败收敛 40001。"""

    @app.exception_handler(SiteServiceError)
    async def _on_site_error(request: Request, exc: SiteServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=CODE_HTTP.get(exc.code, 400),
            content=envelope(request, exc.code, exc.message, None),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": [str(p) for p in err.get("loc", ())], "msg": str(err.get("msg", ""))}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content=envelope(request, 40001, "参数校验失败", {"errors": errors}),
        )

    return app


@dataclass
class SiteStores:
    """demo 内存存储容器（生产替换 PG/Redis；字段即存储协议实现）。"""

    keys: MemorySdkKeyStore = field(default_factory=MemorySdkKeyStore)
    sessions: MemorySessionStore = field(default_factory=MemorySessionStore)
    events: MemoryEventStore = field(default_factory=MemoryEventStore)
    profiles: MemoryProfileStore = field(default_factory=MemoryProfileStore)
    conversations: MemoryConversationRepo = field(default_factory=MemoryConversationRepo)
    invites: MemoryInviteLogStore = field(default_factory=MemoryInviteLogStore)
    customers: MemoryCustomerWriter = field(default_factory=MemoryCustomerWriter)
    leads: MemoryLeadStore = field(default_factory=MemoryLeadStore)
    consents: MemoryConsentStore = field(default_factory=MemoryConsentStore)
    mail_jobs: MemoryMailJobStore = field(default_factory=MemoryMailJobStore)
    tickets: MemoryDsrTicketStore = field(default_factory=MemoryDsrTicketStore)
    audits: MemoryAuditTrail = field(default_factory=MemoryAuditTrail)


@dataclass
class SiteRuntime:
    """装配完成的运行时（端点经 request.app.state.site 取用）。"""

    stores: SiteStores
    sessions: SessionService
    ingest: EventIngest
    builder: ProfileBuilder
    injector: ContextInjector
    cleaner: RetentionCleaner
    chat: SiteChatService
    invite_engine: InviteEngine
    leads: LeadCapture
    consent: ConsentManager
    dsr: DsrService

    def rebuild_profile(self, visitor_uid: str) -> None:
        """事件落库后重建该访客画像（SR-SITE-002 §6.3：有新事件才触发）。"""
        events = self.stores.events.events_of(visitor_uid)
        self.stores.profiles.save(self.builder.build(visitor_uid, events))

    def session_of_token(self, token: str) -> SiteSessionRecord:
        """token → 会话记录（无效/已删除 → 40101，各端点共用）。"""
        claims = self.sessions.verify_token(token)
        record = self.stores.sessions.get(claims.visitor_uid)
        if record is None:
            from services.site.core.session.service import SessionError

            raise SessionError(40101, "会话不存在或已失效")
        return record


#: 会话签名密钥环境变量（CR P0-5 修复：参数 → env → 启动失败，无缺省硬编码）
SITE_TOKEN_SECRET_ENV = "KB_SITE_TOKEN_SECRET"
#: SDK data-key 环境变量（同上；密钥库注册的主站接入键）
SITE_SDK_KEY_ENV = "KB_SITE_SDK_KEY"


def create_site_app(
    *,
    secret: str | None = None,
    llm: Any | None = None,
    search_fn: Callable[..., Any] | None = None,
    card_fn: Callable[[str], dict[str, Any] | None] | None = None,
    consent_required: bool = True,
    lang_support: tuple[str, ...] = DEFAULT_LANG_SUPPORT,
    sdk_key: str | None = None,
    origins: tuple[str, ...] = ("https://shop.example.com",),
    widget_config: dict[str, Any] | None = None,
    session_ttl_s: float = 86400.0,
    invite_cfg: dict[str, Any] | None = None,
    retention_days: int = 365,
    clock: Callable[[], float] = time.time,
    app_version: str = "0.3.0",
) -> FastAPI:
    """组装独立站应用（组件契约见模块 docstring；注入点供联调替换生产形态）。

    :param secret: 访客令牌签名密钥。缺省读环境变量 KB_SITE_TOKEN_SECRET，均缺则
        启动即 SiteConfigError（CR P0-5 修复：无缺省密钥，fail-fast）。
    :param sdk_key: 主站 SDK data-key。缺省读环境变量 KB_SITE_SDK_KEY，均缺则
        启动即 SiteConfigError（同上）。
    """
    # CR P0-5 修复：缺省密钥关门——参数 → env → 启动失败（中文报错）
    token_secret = resolve_site_secret(secret, SITE_TOKEN_SECRET_ENV, "独立站访客令牌签名密钥（secret）")
    resolved_sdk_key = resolve_site_secret(sdk_key, SITE_SDK_KEY_ENV, "独立站 SDK 接入密钥（sdk_key）")
    stores = SiteStores(
        keys=MemorySdkKeyStore(),
        sessions=MemorySessionStore(),
        events=MemoryEventStore(),
        profiles=MemoryProfileStore(),
        conversations=MemoryConversationRepo(),
        invites=MemoryInviteLogStore(),
        customers=MemoryCustomerWriter(),
        leads=MemoryLeadStore(),
        consents=MemoryConsentStore(),
        mail_jobs=MemoryMailJobStore(),
        tickets=MemoryDsrTicketStore(),
        audits=MemoryAuditTrail(),
    )
    stores.keys.register(resolved_sdk_key, origins=origins, widget_config=widget_config or {})

    sessions_service = SessionService(
        stores.keys, stores.sessions,
        VisitorTokenCodec(token_secret, ttl_s=session_ttl_s, clock=clock),
        lang_support=lang_support, consent_required=consent_required, clock=clock,
    )
    # consent 管理器：行为/邀聊统一门控（FR-SITE-007 §6.2 兜底防线）
    consent = ConsentManager(stores.consents, stores.audits, clock=clock)
    ingest = EventIngest(stores.events, consent, clock=clock)
    builder = ProfileBuilder()
    injector = ContextInjector(stores.profiles)
    cleaner = RetentionCleaner(
        stores.events, stores.audits, profile_store=stores.profiles,
        retention_days=retention_days, clock=clock,
    )
    chat = SiteChatService(
        llm if llm is not None else FakeSiteLlm(),
        search_fn if search_fn is not None else (lambda **kw: {"results": []}),
        injector.prompt_hint,
        card_fn=card_fn, repo=stores.conversations,
        gaps=MemoryGapRecorder(), invites=stores.invites,
    )
    cfg = {"dwell_s": 45.0, "repeat_view_n": 3, "daily_max": 2, "cooldown_s": 600.0}
    cfg.update(invite_cfg or {})
    invite_engine = InviteEngine(
        search_fn, consent, stores.invites, clock=clock, **cfg,
    )
    leads = LeadCapture(
        stores.customers, stores.leads,
        profiles=stores.profiles, invites=stores.invites, clock=clock,
    )
    dsr = DsrService(
        MemoryPrivacyData(
            sessions=stores.sessions, events=stores.events, profiles=stores.profiles,
            conversations=stores.conversations, customers=stores.customers, leads=stores.leads,
            mail_jobs=stores.mail_jobs, consents=stores.consents, invites=stores.invites,
        ),
        stores.tickets, stores.audits, clock=clock,
    )
    runtime = SiteRuntime(
        stores=stores, sessions=sessions_service, ingest=ingest, builder=builder,
        injector=injector, cleaner=cleaner, chat=chat, invite_engine=invite_engine,
        leads=leads, consent=consent, dsr=dsr,
    )

    app = FastAPI(
        title="site-service 独立站 API",
        version=app_version,
        description=(
            "Phase 3 独立站（FR-SITE-001/002/003/007 第一批）：一行接入会话、"
            "访客行为追踪与画像注入、AI 对话引擎（邀聊/留资）、隐私合规（consent 三态/DSR）。"
        ),
    )
    install_site_error_handlers(app)
    app.include_router(sdk_router)
    app.include_router(tracking_router)
    app.include_router(privacy_router)

    @app.get("/healthz", tags=["internal"], include_in_schema=False, summary="健康检查")
    def healthz() -> dict[str, str]:
        """存活探针（部署与运维方案 §4）。"""
        return {"status": "ok"}

    app.state.site = runtime
    return app


__all__ = [
    "SITE_SDK_KEY_ENV",
    "SITE_TOKEN_SECRET_ENV",
    "SiteRuntime",
    "SiteStores",
    "create_site_app",
    "install_site_error_handlers",
]
