# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""kb-service 应用工厂（FR-KB-030 / SR-KB-030 §6.2 装配层，迭代 0.2）。

create_app 组装统一检索 API 的真实链路，全部组件为**已冻结接口的只读引用**
（不改 core/、platform/ 任何文件）：

    向量化（FR-KB-013 EmbeddingClient 批量契约）
      └─ QueryEmbedderAdapter ─→ 批量 → 检索单查询形态
    向量库（FR-KB-014 VectorStore 协议：InMemory / Milvus 实现）
      └─ ExprDialectAdapter ─→ recall 的 Milvus 方言 expr → 向量库简化 DSL
    三段式检索（FR-KB-015 RetrievalPipeline：改写/召回/重排 + 预算守卫）
      └─ 路由层（FR-KB-030 api/routes.py，契约冻结 v1）
    鉴权限流（FR-KB-031 api_key_auth + api_key_exception_handler，内存实现）
    可观测（NFR-PER-02 api/metrics.timing_middleware + /internal/metrics）
    管理后台（FR-KB-020 api/admin 三路由，迭代 0.3 追加挂载：
      registry 真实 schema_provider 适配 + 内存条目存储
      + IncrementalSyncEngine 同步钩子（FR-KB-017）+ 内存审计写入器（NFR-SEC-03））

两处装配适配器（均为冻结接口之间的桥，两侧不可改，见各自 docstring）：
- QueryEmbedderAdapter：MultiRouteRecall.EmbedderLike 冻结协议期望
  embed_batch([q]) 返回带 dense/sparse **单查询**属性的对象；FR-KB-013
  EmbeddingClient 冻结契约返回**批量** EmbeddingResult（dense: list[list[float]]）
  ——检索恒单查询，取首行对接；
- ExprDialectAdapter：recall.build_expr 冻结输出为 Milvus 方言
  （``字段 == "值"`` / ``字段 in ["a", "b"]``），而仓库两个 VectorStore 实现的
  冻结入口 hybrid_search 均消费简化 DSL（``字段 = '值'`` / ``字段 in ('a','b')``，
  经 parse_filter_expr 白名单校验）——装配层做方言翻译，语义不变。
"""
from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from foundation.apikey.calllog import ApiCallLogger
from foundation.apikey.dependency import api_key_auth
from foundation.apikey.keys import KeyRegistry
from foundation.apikey.ratelimit import RateLimiter, SlidingWindowRateLimiter

from services.kb.api.admin import InMemoryKnowledgeItemStore, KnowledgeItemStore, configure_admin
from services.kb.api.metrics import MetricsRegistry as ApiMetricsRegistry
from services.kb.api.metrics import TimingMiddleware, mount_metrics_endpoint
from services.kb.api.routes import (
    DEFAULT_SEARCH_TIMEOUT_S,
    ChunkMetaProvider,
    InMemoryChunkMetaProvider,
    install_error_handlers,
)
from services.kb.api.routes import router as search_router
from services.kb.core.ingestion import vector_dims_for
from services.kb.core.pipeline.embedding.base import EmbeddingClient
from services.kb.core.pipeline.sync.engine import (
    ACTION_ARCHIVE,
    ACTION_PUBLISH,
    InMemoryKnowledgeChunkStore,
    IncrementalSyncEngine,
    KnowledgeItemEvent,
    make_text_chunk_fn,
)
from services.kb.core.retrieval import (
    MultiRouteRecall,
    QueryRewriter,
    Reranker,
    RetrievalPipeline,
    VectorStoreLike,
)
from services.kb.core.retrieval.metrics import MetricsRegistry as RetrievalMetricsRegistry
from services.kb.models.registry import REGISTRY

# ---------------------------------------------------------------------------
# 适配器一：向量化批量契约 → 检索单查询形态（EmbedderLike 协议）
# ---------------------------------------------------------------------------


@dataclass
class _QueryEmbedding:
    """单查询向量化结果（recall.EmbedderLike 期望的属性形态）。"""

    dense: list[float] | None = None
    sparse: dict[int, float] | None = None


class QueryEmbedderAdapter:
    """FR-KB-013 批量契约 → 检索单查询形态适配器（见模块 docstring）。"""

    def __init__(self, client: EmbeddingClient) -> None:
        self._client = client

    def embed_batch(self, texts: Sequence[str]) -> _QueryEmbedding:
        """取批量结果首行（检索恒单查询）；空入参返回双 None（recall 走本地稀疏兜底）。

        EmbeddingError（TEI 超时/不可用）原样上抛——由 recall 判定点 C 捕获并
        降级 BM25 稀疏路，本适配器不吞错。
        """
        result = self._client.embed_batch(list(texts))
        if len(result) == 0:
            return _QueryEmbedding(dense=None, sparse=None)
        dense_row, sparse_row = next(result.rows())
        return _QueryEmbedding(dense=dense_row, sparse=sparse_row)


# ---------------------------------------------------------------------------
# 适配器二：recall 的 Milvus 方言 expr → 向量库简化 DSL
# ---------------------------------------------------------------------------

# Milvus 方言单条件词法：字段 +（== 单值 | in [多值列表]），词尾消费 ` and ` 或行尾
_MILVUS_TERM_RE: re.Pattern[str] = re.compile(
    r"""\s*
        (?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*
        (?:
            ==\s*"(?P<one>(?:[^"\\]|\\.)*)"
          | in\s*\[\s*(?P<many>(?:"(?:[^"\\]|\\.)*"\s*,?\s*)+)\]
        )
        (?:\s+and\s+|\s*$)
    """,
    re.VERBOSE,
)
# in 列表内的双引号值抽取（词法已约束为逗号分隔的双引号串）
_MILVUS_VALUE_RE: re.Pattern[str] = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _unescape_literal(value: str) -> str:
    """Milvus 字符串字面量反转义（``\\\\`` → ``\\``，``\\"`` → ``"``）。"""
    return re.sub(r"\\(.)", r"\1", value)


def _reject_single_quote(value: str) -> None:
    """简化 DSL 的单引号字面量无法携带单引号值：显式拒绝（fail-closed）。"""
    if "'" in value:
        raise ValueError(f"过滤值含单引号，简化 DSL 无法表示: {value[:32]!r}")


def translate_milvus_expr(expr: str | None) -> str | None:
    """Milvus 方言 expr → 向量库简化 DSL（只译不改语义；白名单由下游校验）。

    逐条件匹配翻译（不做字符串切分），值内含 ``and``/空格/逗号均安全；
    空串/None 原样返回（无过滤）。非法片段显式 ValueError（fail-closed）。
    """
    if expr is None or not expr.strip():
        return expr
    rendered: list[str] = []
    rest = expr
    while True:
        matched = _MILVUS_TERM_RE.match(rest)
        if matched is None:
            raise ValueError(f"expr 含无法翻译的 Milvus 方言片段: {rest.strip()[:50]!r}")
        field = matched.group("field")
        if matched.group("one") is not None:
            value = _unescape_literal(matched.group("one"))
            _reject_single_quote(value)
            rendered.append(f"{field} = '{value}'")
        else:
            values = [_unescape_literal(v) for v in _MILVUS_VALUE_RE.findall(matched.group("many"))]
            for value in values:
                _reject_single_quote(value)
            listing = ", ".join(f"'{value}'" for value in values)
            rendered.append(f"{field} in ({listing})")
        rest = rest[matched.end():]
        if not rest.strip():
            return " and ".join(rendered)


class ExprDialectAdapter:
    """recall（Milvus 方言 expr）↔ 向量库实现（简化 DSL expr）的装配层适配器。"""

    def __init__(self, store: VectorStoreLike) -> None:
        self._store = store

    def hybrid_search(
        self,
        dense: Any = None,
        sparse: Any = None,
        expr: Any = None,
        top_k: int = 5,
    ) -> Any:
        """转发混合检索：expr 经方言翻译，dense/sparse/top_k 原样透传。

        同步/异步实现均兼容（调用方 recall 经 maybe_await 等待）。
        """
        translated = translate_milvus_expr(expr) if isinstance(expr, str) else expr
        return self._store.hybrid_search(dense=dense, sparse=sparse, expr=translated, top_k=top_k)


# ---------------------------------------------------------------------------
# 迭代 0.3 追加：管理后台装配（FR-KB-020 / FR-KB-017 / NFR-SEC-03）
# ---------------------------------------------------------------------------


class RegistrySchemaProvider:
    """models/registry 字段注册表 → 管理端 SchemaProvider 鸭子契约适配器。

    键名翻译（items.py 模块 docstring 偏差②的注册表侧收敛）：FieldDef.key →
    name、FieldDef.widget → control；rules/visible_when/help/filterable 为契约外
    键，schemas_api.normalize_field 原样透传（前端零拼装）。两侧实现均冻结，
    本适配器是唯一桥接点。
    """

    def list_schemas(self) -> list[dict[str, Any]]:
        """输出六库字段定义清单（[{kb_type, label, fields:[…]}] 契约形态）。"""
        from services.kb.models.registry import get_schema

        schemas: list[dict[str, Any]] = []
        for kb_type, field_defs in REGISTRY.items():
            schemas.append(
                {
                    "kb_type": kb_type,
                    "label": str(get_schema(kb_type).get("label", kb_type)),
                    "fields": [
                        {
                            "name": field.key,
                            "label": field.label,
                            "required": field.required,
                            "control": field.widget,
                            "options": field.options,
                            "rules": field.rules,
                            "visible_when": field.visible_when,
                            "help": field.help,
                            "filterable": field.filterable,
                        }
                        for field in field_defs
                    ],
                }
            )
        return schemas


class InMemoryAuditTrail:
    """审计写入器内存桩（NFR-SEC-03 最小承接）：线程安全追加 kb_change 事件。

    生产替换为平台 audit_log 哈希链写入器（platform/core/audit 侧适配），
    鸭子契约不变；本地开发与测试可直接读取 events 断言留痕。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []

    def record(self, event: Mapping[str, Any]) -> None:
        """追加一条审计事件（同步实现，管理端统一等待协议兼容）。"""
        with self._lock:
            self._events.append(dict(event))

    @property
    def events(self) -> list[dict[str, Any]]:
        """已落审计事件快照（只读副本）。"""
        with self._lock:
            return [dict(event) for event in self._events]


def _stable_item_id(item_uid: str) -> int:
    """条目 UID → 稳定正整数（FR-KB-017 事件 item_id；PG 形态即 knowledge_item.id）。"""
    digest = hashlib.blake2b(item_uid.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % (2**31 - 1) + 1


class IncrementalSyncHook:
    """管理端 SyncHook → IncrementalSyncEngine 适配器（FR-KB-017 增量索引入队）。

    change=upsert → publish（重分块 + 嵌入 + 向量 upsert，分钟级生效）；
    change=remove → archive（向量彻底清除）。条目正文与 kb_type/维度从条目存储
    回查（管理端写路径只传 uid 与版本，不随行正文）。
    """

    def __init__(self, engine: IncrementalSyncEngine, item_store: KnowledgeItemStore) -> None:
        self._engine = engine
        self._item_store = item_store

    def enqueue(self, *, item_uid: str, change: str, idem_key: str, version: int) -> None:
        """入队一次索引变更（异常交由管理端 fire_sync 兜底置 failed，不阻断保存）。"""
        record = self._item_store.get(item_uid)
        if record is None:  # 软删/归档后的竞态窗口：条目已不可见，交对账任务收尾
            return
        event = KnowledgeItemEvent(
            item_id=_stable_item_id(record.uid),
            body=record.content,
            kb_type=record.kb_type,
            action=ACTION_PUBLISH if change == "upsert" else ACTION_ARCHIVE,
            **vector_dims_for(record.kb_type, dict(record.metadata)),
        )
        self._engine.handle_event(event)


def _mount_admin(
    app: FastAPI,
    *,
    vector_store: VectorStoreLike,
    embedder: EmbeddingClient,
    item_store: KnowledgeItemStore | None,
    schema_provider: Any,
    sync_hook: Any,
    audit_hook: Any,
    admin_auth: Any = None,
) -> None:
    """装配管理后台组件并挂载三个路由（缺省组件见各自 docstring）。

    admin_auth 缺省为「未配置鉴权即拒绝写操作」依赖（CR P0-3 修复；
    生产注入 JWT/RBAC 路由依赖，NFR-SEC-02）。
    """
    store = item_store if item_store is not None else InMemoryKnowledgeItemStore()
    resolved_sync = sync_hook if sync_hook is not None else IncrementalSyncHook(
        IncrementalSyncEngine(
            make_text_chunk_fn(),
            InMemoryKnowledgeChunkStore(),
            vector_store,  # type: ignore[arg-type] —— 检索 VectorStoreLike 兼容 upsert/delete 协议
            embedder,
        ),
        store,
    )
    configure_admin(
        app,
        store=store,
        schema_provider=schema_provider if schema_provider is not None else RegistrySchemaProvider(),
        sync_hook=resolved_sync,
        audit_hook=audit_hook if audit_hook is not None else InMemoryAuditTrail(),
        admin_auth=admin_auth,
    )


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(
    store: VectorStoreLike,
    embedder: EmbeddingClient,
    reranker: Reranker | None,
    key_registry: KeyRegistry,
    *,
    limiter: RateLimiter | None = None,
    call_logger: ApiCallLogger | None = None,
    meta_provider: ChunkMetaProvider | None = None,
    rewriter: QueryRewriter | None = None,
    metrics: ApiMetricsRegistry | None = None,
    timeout_s: float = DEFAULT_SEARCH_TIMEOUT_S,
    trust_forwarded: bool = False,
    app_version: str = "1.0.0",
    mount_admin: bool = True,
    admin_item_store: KnowledgeItemStore | None = None,
    admin_schema_provider: Any = None,
    admin_sync_hook: Any = None,
    admin_audit_hook: Any = None,
    admin_auth: Any = None,
) -> FastAPI:
    """组装统一检索 API 应用（组件契约与适配器见模块 docstring）。

    - reranker=None → PassthroughReranker（精排未接入时 RRF 序直出）；
    - limiter=None → SlidingWindowRateLimiter（内存限流；生产换 Redis 形态）；
    - timeout_s：API 层全链硬截止秒数（KB_API_TIMEOUT_S，默认 0.5）；
    - trust_forwarded：Nginx 前置时取 X-Forwarded-For 做 IP 白名单判定；
    - mount_admin（迭代 0.3）：是否挂载管理后台三路由（items/schemas/stats），
      缺省挂载——组件缺省为内存条目存储 + registry 真实 Schema 适配 +
      IncrementalSyncEngine 同步钩子 + 内存审计写入器（生产注入 PG 存储、
      Redis 队列与 audit_log 哈希链写入器，见 _mount_admin）。

    挂载端点：POST /api/v1/knowledge/search（冻结契约）、GET /healthz（存活
    探针）、GET /internal/metrics（指标快照，NFR-PER-02 基线采集通道）、
    /api/v1/knowledge/items|schemas 与 /api/v1/admin/stats（FR-KB-020 管理端）。
    """
    api_metrics = metrics if metrics is not None else ApiMetricsRegistry()
    # 检索管道内打点（kb_search_* 序列）使用 retrieval 侧 MetricsRegistry
    # （counter(name, **labels) 签名），与 API 层注册表（线程安全 + 快照导出）
    # 是两套冻结口径，互不混用。
    pipeline = RetrievalPipeline(
        rewriter=rewriter if rewriter is not None else QueryRewriter(),
        recall=MultiRouteRecall(ExprDialectAdapter(store), QueryEmbedderAdapter(embedder)),
        reranker=reranker,
        metrics=RetrievalMetricsRegistry(),
    )
    auth = api_key_auth(
        key_registry,
        limiter if limiter is not None else SlidingWindowRateLimiter(),
        call_logger=call_logger,
        trust_forwarded=trust_forwarded,
    )

    app = FastAPI(
        title="kb-service 统一知识检索 API",
        version=app_version,
        description=(
            "FR-KB-030 统一检索 API（契约冻结 v1，04-接口设计 §2）：P1-P5 全模块"
            "唯一知识入口。P0 验收后变更走 04 §5 版本化流程；冻结基线快照见 "
            "docs/openapi/openapi.yaml。"
        ),
    )
    install_error_handlers(app)
    app.include_router(search_router)
    app.add_middleware(TimingMiddleware, registry=api_metrics)
    mount_metrics_endpoint(app, api_metrics)

    @app.get("/healthz", tags=["internal"], include_in_schema=False, summary="健康检查")
    def healthz() -> dict[str, str]:
        """存活探针（部署与运维方案 §4：容器编排健康检查端点）。"""
        return {"status": "ok"}

    # 依赖注入挂载（routes.py 的依赖函数从 app.state 取用）
    app.state.search_service = pipeline
    app.state.chunk_meta = meta_provider if meta_provider is not None else InMemoryChunkMetaProvider()
    app.state.auth_dependency = auth
    app.state.search_timeout_s = timeout_s

    # 迭代 0.3：管理后台挂载（FR-KB-020；组件与检索链路共用向量库与向量化客户端）
    if mount_admin:
        _mount_admin(
            app,
            vector_store=store,
            embedder=embedder,
            item_store=admin_item_store,
            schema_provider=admin_schema_provider,
            sync_hook=admin_sync_hook,
            audit_hook=admin_audit_hook,
            admin_auth=admin_auth,
        )
    return app
