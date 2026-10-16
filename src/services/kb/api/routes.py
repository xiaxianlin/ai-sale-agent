# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""统一检索路由层（FR-KB-030 / SR-KB-030 §6.2~§6.3）：POST /api/v1/knowledge/search。

职责边界（装配见 services/kb/app.py create_app，路由层零具体实现导入）：
- 依赖注入：检索服务（SearchService 协议，RetrievalPipeline 结构性满足）与
  chunk 元数据回表（ChunkMetaProvider 协议）均经 app.state 注入；
- 错误码映射（04-接口设计 §1.3 + SR-KB-030 §5）：参数校验失败 40001
  （HTTP 400，data.errors 定位字段）、未授权/限流 40101/42901（复用 FR-KB-031
  的 api_key_exception_handler 统一信封）、下游依赖故障/超时 50002（HTTP 503，
  降级语义文案）；
- trace_id 透传（04 §1.4）：优先 X-Trace-Id 头，缺失新生成 uuid；鉴权依赖
  写入 request.state 后全程复用，信封必携带；
- 回表组装：向量库命中（SearchView 契约）仅含 chunk_id/score，title/source
  （及 kb_type）由 ChunkMetaProvider 批量补齐；元数据缺失的命中整条丢弃
  （增量删除竞态兜底，SR-KB-030 §6.3）。

偏差标注：
- SR-KB-030 §6.3 的 PG 回表（_ENRICH_SQL）属 kb-service 数据访问层，本迭代以
  ChunkMetaProvider 协议 + 内存实现承接（chunk_id → 元数据映射），PG 实现
  随数据访问层迭代接入，路由层组装逻辑不变；
- kb_type 一并由回表提供：仓库向量库 SearchView 冻结契约仅 chunk_id+score
  （03-数据库设计 §3.1 职责边界：PG 为权威源），而 SR-KB-030 §6.3 假设
  Milvus 命中携带 kb_type——以回表为准、命中值为兜底；
- 消费方差异化默认值（SR-KB-030 §2.3/§6.4，consumer.apply_defaults）属
  SR-KB-031 注册表扩展，后续迭代接入；本迭代契约缺省值恒定
  （kb_types=全部、top_k=5、rerank=true）。
"""
from __future__ import annotations

from foundation.core.envelope import envelope

import asyncio
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from foundation.apikey.dependency import api_key_exception_handler, resolve_trace_id
from foundation.core.envelope import error_envelope
from foundation.apikey.keys import ConsumerContext

from services.kb.core.retrieval import Hit, RetrievalDownstreamError, RetrievalResult

from .schemas import SearchData, SearchRequest, SearchResponse, SearchResultItem, SourceRef

#: 统一检索端点（与 api.metrics.SEARCH_PATH 同一端点：timing 中间件计时口径）
SEARCH_PATH: str = "/api/v1/knowledge/search"

#: API 层全链硬截止默认值（KB_API_TIMEOUT_S，SR-KB-030 §7：0.5s，对齐 480ms 硬截止 + 余量）
DEFAULT_SEARCH_TIMEOUT_S: float = 0.5

#: 参数校验失败业务码（04-接口设计 §1.3）
CODE_BAD_REQUEST: int = 40001
#: 下游依赖超时/故障业务码（04-接口设计 §1.3；HTTP 状态取网关标准语义 503）
CODE_DOWNSTREAM: int = 50002


# --------------------------------------------------------------------------- 错误体系与统一信封


class ApiError(Exception):
    """路由层业务异常：携带业务码 / HTTP 状态 / 文案 / 可选 data，由信封处理器展开。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（CR A2 全面委托化：foundation.core.envelope 单点实现）。"""
    return envelope(code, message, data, trace_id)


def _request_trace_id(request: Request) -> str:
    """取本请求 trace_id：鉴权依赖已写入 request.state 则复用，否则按 04 §1.4 解析。"""
    return getattr(request.state, "trace_id", "") or resolve_trace_id(request)


def _json_safe_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """校验错误规整为 JSON 安全子集（loc/msg/type）。

    pydantic v2 的 errors() 在 ctx 中可能携带 ValueError 实例（不可 JSON 化），
    保留 loc/msg/type 三键即可满足「detail 定位字段」的验收口径。
    """
    return [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()
    ]


def install_error_handlers(app: FastAPI) -> FastAPI:
    """注册统一信封错误处理器（信封格式全路由一致，04-接口设计 §1.3）。

    - ApiError：路由层主动抛出的业务错误（当前为 50002 族）；
    - RequestValidationError：Pydantic 校验失败 → 40001（data.errors 定位字段）；
    - RetrievalDownstreamError：全部召回路失败（SR-KB-015 §6.1）→ 50002；
    - HTTPException：40101/40301/42901 复用 FR-KB-031 的 api_key_exception_handler
      （结构化 detail 展开为信封，42901 附 Retry-After 头）。
    """

    @app.exception_handler(ApiError)
    async def _on_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            # CR i18n 接线：错误信封自动附用户行动建议（技术 message 保持）
            content=error_envelope(exc.code, exc.message, request, exc.data),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=error_envelope(
                CODE_BAD_REQUEST, "参数校验失败", request,
                {"errors": _json_safe_errors(exc)},
            ),
        )

    @app.exception_handler(RetrievalDownstreamError)
    async def _on_downstream_error(request: Request, exc: RetrievalDownstreamError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=_envelope(
                _request_trace_id(request), CODE_DOWNSTREAM,
                "下游依赖超时（Embedding/向量库/重排），已触发降级策略", None,
            ),
        )

    app.add_exception_handler(HTTPException, api_key_exception_handler)
    return app


# --------------------------------------------------------------------------- 回表协议（SR-KB-030 §6.3）


@dataclass(frozen=True)
class ChunkMeta:
    """chunk 元数据（PG knowledge_chunk 回表行的内存形态）。

    kb_type 为偏差增补（见模块 docstring 偏差标注②）：向量库命中不含
    kb_type，回表为权威源。
    """

    kb_type: str
    title: str
    item_uid: str
    origin_file: str
    version: int


@runtime_checkable
class ChunkMetaProvider(Protocol):
    """回表协议：chunk_id 批量 → 元数据映射（生产为 PG 批量 JOIN，见模块 docstring）。"""

    def lookup(self, chunk_ids: Sequence[int]) -> Mapping[int, ChunkMeta]:
        """按主键批量取元数据；未命中的主键不在返回映射中（调用方丢弃对应命中）。"""
        ...  # pragma: no cover - Protocol 桩


class InMemoryChunkMetaProvider:
    """内存回表实现（chunk_id → ChunkMeta；线程安全），单元测试与本地开发用。"""

    def __init__(self, items: Mapping[int, ChunkMeta] | None = None) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, ChunkMeta] = dict(items or {})

    def upsert(self, chunk_id: int, meta: ChunkMeta) -> None:
        """写入/覆盖单条元数据（测试灌数入口）。"""
        with self._lock:
            self._items[chunk_id] = meta

    def delete(self, chunk_ids: Sequence[int]) -> None:
        """删除元数据（模拟增量删除，测试灌数入口）。"""
        with self._lock:
            for chunk_id in chunk_ids:
                self._items.pop(chunk_id, None)

    def lookup(self, chunk_ids: Sequence[int]) -> dict[int, ChunkMeta]:
        """批量取元数据快照；未命中主键不出现在结果中。"""
        with self._lock:
            return {cid: self._items[cid] for cid in chunk_ids if cid in self._items}


# --------------------------------------------------------------------------- 检索服务协议与依赖注入


@runtime_checkable
class SearchService(Protocol):
    """检索服务协议：RetrievalPipeline.search 签名的结构性投影（SR-KB-030 §6.2③）。

    路由层按本协议调用（鸭子类型注入），具体实现由 create_app 装配
    （RetrievalPipeline）或测试替换；协议与实现解耦，检索内核对 API 层黑盒。
    """

    async def search(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        kb_types: Sequence[str] | None = None,
        top_k: int = 5,
        rerank: bool = True,
    ) -> RetrievalResult:
        """执行检索，返回统一检索结果（命中/改写文本/分段计时/降级链）。"""
        ...  # pragma: no cover - Protocol 桩


def get_search_service(request: Request) -> SearchService:
    """依赖：从 app.state 取检索服务（create_app 装配注入）。"""
    service = getattr(request.app.state, "search_service", None)
    if service is None:
        raise RuntimeError("检索服务未装配：create_app 负责注入 app.state.search_service")
    return service  # type: ignore[no-any-return]


def get_chunk_meta(request: Request) -> ChunkMetaProvider:
    """依赖：从 app.state 取回表提供者（create_app 装配注入）。"""
    provider = getattr(request.app.state, "chunk_meta", None)
    if provider is None:
        raise RuntimeError("回表提供者未装配：create_app 负责注入 app.state.chunk_meta")
    return provider  # type: ignore[no-any-return]


async def authenticate(request: Request) -> ConsumerContext:
    """鉴权+限流依赖（FR-KB-031 装配注入 api_key_auth 产物）。

    Bearer 解析 → 注册表校验（40101/40301）→ 限流/配额判定（42901 +
    Retry-After）；通过后消费方上下文注入并写 request.state.trace_id。
    """
    auth = getattr(request.app.state, "auth_dependency", None)
    if auth is None:
        raise RuntimeError("鉴权依赖未装配：create_app 负责注入 app.state.auth_dependency")
    return await auth(request)


# --------------------------------------------------------------------------- 响应组装（SR-KB-030 §6.3）


def _coerce_chunk_id(raw: Any) -> int | None:
    """候选主键规整为 int（PG bigint 口径）；不可转换形态返回 None（异常数据丢弃）。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def assemble_results(
    hits: Sequence[Hit],
    meta_provider: ChunkMetaProvider,
) -> list[SearchResultItem]:
    """召回命中 + 回表元数据 → 契约结果列表（04 §2 results 元素逐字段组装）。

    - kb_type：回表为权威源，命中值为兜底（偏差标注②）；
    - snippet：取检索视图携带值（当前向量库 SearchView 契约未含 → 空串，
      Milvus 生产形态/后续迭代补齐）；
    - 元数据缺失或主键不可转换的命中整条丢弃（增量删除竞态兜底）。
    """
    metas = meta_provider.lookup(
        [key for hit in hits if (key := _coerce_chunk_id(hit.chunk_id)) is not None]
    )
    items: list[SearchResultItem] = []
    for hit in hits:
        key = _coerce_chunk_id(hit.chunk_id)
        if key is None:
            continue
        meta = metas.get(key)
        if meta is None:
            continue
        items.append(
            SearchResultItem(
                chunk_id=key,
                kb_type=meta.kb_type or hit.kb_type,
                title=meta.title,
                snippet=hit.snippet,
                source=SourceRef(
                    item_uid=meta.item_uid, origin_file=meta.origin_file, version=meta.version
                ),
                score=hit.score,
            )
        )
    return items


# --------------------------------------------------------------------------- 端点（契约冻结 v1）

router = APIRouter(prefix="/api/v1", tags=["knowledge"])


@router.post(
    "/knowledge/search",
    response_model=SearchResponse,
    summary="统一知识检索（FR-KB-030，契约冻结 v1：P1-P5 全模块唯一知识入口）",
)
async def knowledge_search(
    body: SearchRequest,
    request: Request,
    consumer: ConsumerContext = Depends(authenticate),
    service: SearchService = Depends(get_search_service),
    meta_provider: ChunkMetaProvider = Depends(get_chunk_meta),
) -> SearchResponse:
    """执行统一检索：鉴权（依赖）→ 三段式检索（预算守卫）→ 回表组装 → 信封返回。

    - 480ms 硬截止（KB_API_TIMEOUT_S 经 app.state 注入）：超时或全部召回路
      失败 → 50002/503（降级语义）；
    - latency_ms 为端到端在线测量值（含回表，NFR-PER-02 验收测量口径）；
    - consumer 当前仅作鉴权产物保留（差异化默认值后续迭代接入，见模块
      docstring 偏差标注③）。
    """
    trace_id = _request_trace_id(request)
    timeout_s = float(getattr(request.app.state, "search_timeout_s", DEFAULT_SEARCH_TIMEOUT_S))
    started = perf_counter()
    try:
        result: RetrievalResult = await asyncio.wait_for(
            service.search(
                query=body.query,
                filters=body.filters,
                kb_types=body.kb_types,
                top_k=body.top_k,
                rerank=body.rerank,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        # API 层全链硬截止（SR-KB-030 §6.2③）：超时即降级，不拖垮消费方
        raise ApiError(
            CODE_DOWNSTREAM, 503, "检索链路超时（Embedding/向量库/重排），已触发降级策略"
        ) from None
    except RetrievalDownstreamError:
        # 全部召回路失败（SR-KB-015 §6.1 判定点 C 之后无可用降级）
        raise ApiError(
            CODE_DOWNSTREAM, 503, "下游依赖超时（Embedding/向量库/重排），已触发降级策略"
        ) from None
    latency_ms = int((perf_counter() - started) * 1000.0)
    results = assemble_results(result.hits, meta_provider)
    return SearchResponse(
        trace_id=trace_id,
        data=SearchData(
            results=results,
            latency_ms=latency_ms,
            rewritten_query=result.rewritten_query,
        ),
    )
