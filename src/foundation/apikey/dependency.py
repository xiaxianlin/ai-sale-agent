# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-KB-031 FastAPI 鉴权依赖装配（Bearer → 注册表 → 限流/配额 → 上下文注入）。

依据：docs/07-实现方案/KB/SR-KB-031-API鉴权与配额.md
- §6.2 authenticate 契约（SR-KB-030 §6.2 引用）：Bearer 解析 → 哈希查注册表 →
  消费方上下文 → 限流/配额判定；40101/40301/42901 在此抛出；
- §6.3 42901 附 Retry-After 头（04-接口设计 §1.3 未定义该头，属向下兼容增补）；
- §6.4 调用日志：每次判定（成功与被拒）经 ApiCallLogger 留痕，consumer 无法归因记
  'unknown'（暴力探测审计线索）。

用法（受保护路由）::

    registry = KeyRegistry()
    limiter = SlidingWindowRateLimiter()
    logger = ApiCallLogger()
    auth = api_key_auth(registry, limiter, call_logger=logger)

    app = FastAPI()
    app.add_exception_handler(HTTPException, api_key_exception_handler)  # 统一错误信封

    @app.post("/api/v1/knowledge/search")
    async def search(consumer: ConsumerContext = Depends(auth)) -> dict:
        return {"code": 0, "message": "ok",
                "data": {"consumer": consumer.consumer}, "trace_id": "..."}

错误响应对齐 04-接口设计 §1.3 信封 {code, message, data, trace_id}；
40101 不区分「缺失/错误/吊销」具体原因（防密钥探测，SR §5 错误码映射）。
"""

from __future__ import annotations

import time
import uuid
from typing import Mapping

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from foundation.apikey.calllog import UNKNOWN_CONSUMER, ApiCallLogger
from foundation.apikey.keys import ApiKeyError, ConsumerContext, InvalidApiKeyError, KeyRegistry
from foundation.apikey.ratelimit import QuotaExceededError, RateLimiter, RateLimitResult

TRACE_HEADER: str = "X-Trace-Id"
"""链路追踪透传头（04-接口设计 §1.4）。"""

ENVELOPE_FALLBACK: Mapping[int, tuple[int, str]] = {
    401: (40101, "API Key 无效或缺失"),
    403: (40301, "来源 IP 不在白名单"),
    429: (42901, "触发限流（QPS 或日配额超限，FR-KB-031）"),
}
"""非结构化 HTTPException 的状态码兜底映射（保持信封格式全路由一致）。"""


def resolve_trace_id(request: Request) -> str:
    """解析 trace_id：优先透传 X-Trace-Id，否则新生成 uuid（04 §1.4）。"""
    header_value = request.headers.get(TRACE_HEADER)
    return header_value.strip() if header_value and header_value.strip() else str(uuid.uuid4())


def extract_bearer_token(authorization: str | None) -> str:
    """解析 ``Authorization: Bearer <api-key>``；缺失/格式错返回空串（由校验层统一 40101）。

    防探测口径（SR §6.2 ①）：缺头/格式错/空 token 不区分报错，全部走 InvalidApiKeyError。
    """
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return ""
    return token.strip()


def extract_client_ip(request: Request, *, trust_forwarded: bool = False) -> str | None:
    """提取来源 IP：默认取对端 host；trust_forwarded=True 时优先 X-Forwarded-For 首段。

    生产部署 Nginx HTTPS 终止前置（SR §2.1），开启 trust_forwarded 取真实来源 IP
    做 IP 白名单判定；直连（本地/测试）取 request.client.host。
    """
    if trust_forwarded:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client is not None else None


def to_http_exception(error: ApiKeyError, trace_id: str) -> HTTPException:
    """治理异常 → HTTPException（结构化 detail + 可选 Retry-After 头）。

    - detail 携带 {code, message, data, trace_id}，由 api_key_exception_handler 展开
      为 04 §1.3 信封（FastAPI 默认 handler 只回 {"detail": ...}，不满足信封要求）；
    - Retry-After：仅 42901（QuotaExceededError.retry_after）携带（SR §6.3 errors.py 增补）。
    """
    headers: dict[str, str] | None = None
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        headers = {"Retry-After": str(int(retry_after))}
    return HTTPException(
        status_code=error.http_status,
        detail={"code": error.code, "message": error.message, "data": None, "trace_id": trace_id},
        headers=headers,
    )


async def api_key_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """HTTPException → 统一错误信封 {code, message, data, trace_id}（04-接口设计 §1.3）。

    用法：``app.add_exception_handler(HTTPException, api_key_exception_handler)``。
    结构化 detail（本模块 to_http_exception 产物）原样展开；其他 HTTPException
    按状态码兜底映射，保证任何错误路径都返回同构信封。exc.headers（如
    Retry-After）原样透传。
    """
    detail = exc.detail
    if isinstance(detail, Mapping) and "code" in detail:
        code = int(detail.get("code", 50001))  # type: ignore[arg-type]
        message = str(detail.get("message", "内部错误"))
        trace_id = str(detail.get("trace_id") or "") or resolve_trace_id(request)
    else:
        code, message = ENVELOPE_FALLBACK.get(exc.status_code, (50001, str(detail) or "内部错误"))
        trace_id = resolve_trace_id(request)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": code, "message": message, "data": None, "trace_id": trace_id},
        headers=dict(exc.headers) if exc.headers else None,
    )


# --------------------------------------------------------------------------- 鉴权依赖（SR §6.2 authenticate）


class ApiKeyAuth:
    """FastAPI 依赖：Bearer 解析 → 注册表校验 → 限流/配额判定 → 消费方上下文。

    每次判定（成功与被拒）写调用日志（含 401/403/429，SR §6.4）；
    通过后把 ConsumerContext 挂到 request.state.consumer（SR-KB-030 §6.4
    差异化依据）并作为依赖返回值注入路由参数。
    """

    def __init__(
        self,
        registry: KeyRegistry,
        limiter: RateLimiter,
        *,
        call_logger: ApiCallLogger | None = None,
        trust_forwarded: bool = False,
    ) -> None:
        self._registry = registry
        self._limiter = limiter
        self._call_logger = call_logger
        self._trust_forwarded = trust_forwarded

    async def __call__(self, request: Request) -> ConsumerContext:
        """依赖入口：全程同步判定（内存实现无 IO）；生产 Redis/PG 形态在此换异步调用。"""
        trace_id = resolve_trace_id(request)
        request.state.trace_id = trace_id  # 路由/信封共用（04 §1.4）
        started = time.perf_counter()
        consumer_name = UNKNOWN_CONSUMER
        client_ip = extract_client_ip(request, trust_forwarded=self._trust_forwarded)
        method, path = request.method, request.url.path
        try:
            # ① Bearer 解析（缺头/格式错 → 40101，防探测统一文案）
            token = extract_bearer_token(request.headers.get("Authorization"))
            if not token:
                raise InvalidApiKeyError()
            # ② 注册表校验（哈希定位/状态/白名单 → 40101/40301）+ 消费方上下文
            context = self._registry.verify(token, client_ip=client_ip)
            consumer_name = context.consumer
            # ③ 限流/配额判定（超限 → 42901 + Retry-After）
            result: RateLimitResult = self._limiter.check(
                context.consumer, qps=context.qps, daily=context.daily
            )
            if not result.allowed:
                raise QuotaExceededError(reason=result.reason, retry_after=result.retry_after)
        except ApiKeyError as error:
            self._log(
                trace_id=trace_id, consumer=consumer_name, method=method, path=path,
                client_ip=client_ip, error=error, started=started,
            )
            raise to_http_exception(error, trace_id) from error
        # ④ 成功：上下文注入 + 留痕（http_code=200/code=0）
        request.state.consumer = context
        self._log(
            trace_id=trace_id, consumer=consumer_name, method=method, path=path,
            client_ip=client_ip, error=None, started=started,
        )
        return context

    def _log(
        self,
        *,
        trace_id: str,
        consumer: str,
        method: str,
        path: str,
        client_ip: str | None,
        error: ApiKeyError | None,
        started: float,
    ) -> None:
        """调用日志写入（含被拒；logger 未装配则跳过——日志缺失不阻断请求）。"""
        if self._call_logger is None:
            return
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._call_logger.record(
            trace_id=trace_id,
            consumer=consumer,
            method=method,
            path=path,
            client_ip=client_ip,
            http_code=error.http_status if error is not None else 200,
            code=error.code if error is not None else 0,
            latency_ms=latency_ms,
        )


def api_key_auth(
    registry: KeyRegistry,
    limiter: RateLimiter,
    *,
    call_logger: ApiCallLogger | None = None,
    trust_forwarded: bool = False,
) -> ApiKeyAuth:
    """依赖工厂：``Depends(api_key_auth(registry, limiter, call_logger=logger))``。

    返回可调用实例（ApiKeyAuth.__call__ 即 FastAPI 依赖函数），等价 SR §6.2
    deps.py 的 ``authenticate`` 装配形态。
    """
    return ApiKeyAuth(
        registry, limiter, call_logger=call_logger, trust_forwarded=trust_forwarded
    )
