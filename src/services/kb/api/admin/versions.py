# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# versions.py —— 管理后台版本 API（FR-KB-022 / SR-KB-022 §5）：历史/对比/回滚。
"""版本管理路由层（04-接口设计 §3.1 登记口径的细化，SR-KB-022 §2 偏差说明）：

    GET  /api/v1/knowledge/items/{uid}/versions                 版本历史（倒序时间线）
    GET  /api/v1/knowledge/items/{uid}/versions/{v1}/diff/{v2}  双版对比（DiffReport UI 契约）
    POST /api/v1/knowledge/items/{uid}/rollback                 回滚 {to_version, reason, ...}

统一信封 {code, message, data, trace_id}（04-接口设计 §1.3）；错误码：
40001 参数非法 / 40401 条目或版本不存在 / 40901 乐观并发冲突（SR-KB-022 §5）。

装配约定（与 api/routes.py 同风格的路由层零实现导入）：
- VersioningService 经 app.state.versioning_service 注入，由 install_version_error_handlers
  注册 VersioningError / RequestValidationError 的信封处理器（与 routes.py 的
  40001 处理器同构，若并存由装配层保证注册顺序，响应形态一致）；
- trace_id 透传：优先 X-Trace-Id 头，缺失新生成 uuid（04 §1.4）。

迭代 0.3 偏差（接入时替换，登记于模块 docstring）：
- JWT + RBAC（kb:read / kb:rollback，NFR-SEC-02）未接入：操作人以 X-Actor 头
  注入（缺省 admin），鉴权依赖随平台身份组件迭代挂载；
- 分页为内存全量切片（数据访问层接 PG 后换 UNIQUE(item_id, version) 倒序分页）；
- 回滚 request_id 幂等（Redis）未接入：由 service 层幂等口径与前端二次确认兜底。
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from foundation.core.envelope import envelope

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.kb.core.versioning import (
    VersioningError,
    VersioningService,
    VersionSnapshot,
)

#: 操作人请求头（JWT 接入前的桩口径；NFR-SEC-02 鉴权随平台迭代挂载）
ACTOR_HEADER: str = "x-actor"
#: 缺省操作人（无头时兜底，仅迭代 0.3 联调期使用）
DEFAULT_ACTOR: str = "admin"
#: 全链追踪头（04-接口设计 §1.4）
TRACE_HEADER: str = "x-trace-id"

#: 成功业务码（04-接口设计 §1.3）
CODE_OK: int = 0
#: 参数校验失败业务码（与 api/routes.py 同口径）
CODE_BAD_REQUEST: int = 40001

router = APIRouter(tags=["知识版本管理（FR-KB-022）"])


# ── 请求/响应模型 ───────────────────────────────────────────────────────────


class RollbackRequest(BaseModel):
    """回滚入参（SR-KB-022 §5：extra="forbid" 冻结接口防字段拼错静默吞没）。"""

    model_config = ConfigDict(extra="forbid")

    to_version: int = Field(..., ge=1, description="回滚目标版本号（≥1，须为既有版本）")
    reason: str = Field("", max_length=500, description="回滚原因（写入 change_note 与审计）")
    expected_version: int | None = Field(
        None,
        ge=1,
        description="乐观并发：调用方看到的当前版本号；与实际不符返回 40901",
    )


# ── 依赖与信封 ──────────────────────────────────────────────────────────────


def get_versioning_service(request: Request) -> VersioningService:
    """从 app.state 取版本服务（装配层注入；未装配属部署错误，显式 500）。"""
    service = getattr(request.app.state, "versioning_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="versioning_service 未装配（app.state）")
    return service


def _trace_id(request: Request) -> str:
    """取本请求 trace_id：X-Trace-Id 透传，缺失新生成 uuid（04 §1.4）。"""
    return request.headers.get(TRACE_HEADER) or uuid4().hex


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（CR A2 全面委托化：foundation.core.envelope 单点实现）。"""
    return envelope(code, message, data, trace_id)


def _version_payload(snapshot: VersionSnapshot, *, is_current: bool) -> dict[str, Any]:
    """版本时间线单条（SR-KB-022 §5 versions[] 元素契约）。"""
    return {
        "version": snapshot.version,
        "change_note": snapshot.change_note,
        "created_at": snapshot.created_at.isoformat(),
        "created_by": snapshot.created_by,
        "rollback_from": snapshot.rollback_from,
        "is_current": is_current,
    }


def install_version_error_handlers(app: FastAPI) -> FastAPI:
    """注册版本域错误处理器：VersioningError → 信封（业务码/HTTP 状态映射）。"""

    @app.exception_handler(VersioningError)
    async def _on_versioning_error(request: Request, exc: VersioningError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(_trace_id(request), exc.code, exc.message, None),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_envelope(
                _trace_id(request),
                CODE_BAD_REQUEST,
                "参数校验失败",
                {
                    "errors": [
                        {
                            "loc": [str(part) for part in error.get("loc", ())],
                            "msg": str(error.get("msg", "")),
                            "type": str(error.get("type", "")),
                        }
                        for error in exc.errors()
                    ]
                },
            ),
        )

    return app


# ── 端点 ────────────────────────────────────────────────────────────────────


@router.get(
    "/api/v1/knowledge/items/{uid}/versions",
    summary="版本历史（倒序时间线 + 当前版标记 + 回滚来源徽标）",
)
def list_item_versions(
    uid: str,
    request: Request,
    page: int = Query(1, ge=1, le=10_000, description="页码（1 起）"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    service: VersioningService = Depends(get_versioning_service),
) -> dict[str, Any]:
    """版本历史倒序分页：versions[]（version/change_note/created_at/created_by/
    rollback_from/is_current）+ total；40401 条目不存在。"""
    item = service.get_item(uid)  # ItemNotFound → 40401 信封
    snapshots = service.list_versions(uid)  # 升序全量 → 倒序时间线
    ordered = sorted(snapshots, key=lambda snap: snap.version, reverse=True)
    start = (page - 1) * page_size
    window = ordered[start : start + page_size]
    data = {
        "item_uid": uid,
        "total": len(ordered),
        "page": page,
        "page_size": page_size,
        "versions": [
            _version_payload(snap, is_current=snap.version == item.current_version)
            for snap in window
        ],
    }
    return _envelope(_trace_id(request), CODE_OK, "ok", data)


@router.get(
    "/api/v1/knowledge/items/{uid}/versions/{version_from}/diff/{version_to}",
    summary="双版对比（服务端字段分类 diff，前端双栏渲染）",
)
def diff_item_versions(
    uid: str,
    request: Request,
    version_from: int = Path(..., ge=1, description="旧版版本号（≥1）"),
    version_to: int = Path(..., ge=1, description="新版版本号（≥1）"),
    service: VersioningService = Depends(get_versioning_service),
) -> dict[str, Any]:
    """两版对比：DiffReport UI 数据契约（from/to/summary/fields，kind 分派渲染）；
    40001 版本号非法、40401 条目或版本不存在。"""
    report = service.diff(uid, version_from, version_to)
    return _envelope(_trace_id(request), CODE_OK, "ok", report.ui_payload())


@router.post(
    "/api/v1/knowledge/items/{uid}/rollback",
    summary="回滚至指定版本（生成引用旧快照的新版本，历史保留）",
)
def rollback_item(
    uid: str,
    body: RollbackRequest,
    request: Request,
    service: VersioningService = Depends(get_versioning_service),
) -> dict[str, Any]:
    """回滚：{to_version, reason, expected_version?} → {item_uid, new_version,
    rollback_from}；40401 目标版本/条目不存在、40901 乐观并发冲突。"""
    actor = request.headers.get(ACTOR_HEADER, DEFAULT_ACTOR)
    snapshot = service.rollback(
        uid,
        body.to_version,
        actor=actor,
        reason=body.reason,
        expected_version=body.expected_version,
    )
    data = {
        "item_uid": uid,
        "new_version": snapshot.version,
        "rollback_from": snapshot.rollback_from,
    }
    return _envelope(_trace_id(request), CODE_OK, "ok", data)
