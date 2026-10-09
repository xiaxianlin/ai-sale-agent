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

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from platform.core.apikey.calllog import ApiCallLogger
from platform.core.apikey.dependency import api_key_auth
from platform.core.apikey.keys import KeyRegistry
from platform.core.apikey.ratelimit import RateLimiter, SlidingWindowRateLimiter

from services.kb.api.metrics import MetricsRegistry as ApiMetricsRegistry
from services.kb.api.metrics import TimingMiddleware, mount_metrics_endpoint
from services.kb.api.routes import (
    DEFAULT_SEARCH_TIMEOUT_S,
    ChunkMetaProvider,
    InMemoryChunkMetaProvider,
    install_error_handlers,
)
from services.kb.api.routes import router as search_router
from services.kb.core.pipeline.embedding.base import EmbeddingClient
from services.kb.core.retrieval import (
    MultiRouteRecall,
    QueryRewriter,
    Reranker,
    RetrievalPipeline,
    VectorStoreLike,
)
from services.kb.core.retrieval.metrics import MetricsRegistry as RetrievalMetricsRegistry

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
) -> FastAPI:
    """组装统一检索 API 应用（组件契约与适配器见模块 docstring）。

    - reranker=None → PassthroughReranker（精排未接入时 RRF 序直出）；
    - limiter=None → SlidingWindowRateLimiter（内存限流；生产换 Redis 形态）；
    - timeout_s：API 层全链硬截止秒数（KB_API_TIMEOUT_S，默认 0.5）；
    - trust_forwarded：Nginx 前置时取 X-Forwarded-For 做 IP 白名单判定。

    挂载端点：POST /api/v1/knowledge/search（冻结契约）、GET /healthz（存活
    探针）、GET /internal/metrics（指标快照，NFR-PER-02 基线采集通道）。
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
    return app
