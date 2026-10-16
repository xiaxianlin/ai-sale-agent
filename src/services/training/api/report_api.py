# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果报告导出 API（FR-TR-009，SR-TR-009 §5 端点细化，迭代 1.3）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3）：
- POST /api/v1/training/reports/batch/{batch_id}     批次导出任务（幂等：未终态同参 40902）
- POST /api/v1/training/reports/trainee/{trainee_id} 个人导出任务
- GET  /api/v1/training/reports/tasks/{task_uid}           任务状态（状态机 queued→done/failed）
- GET  /api/v1/training/reports/tasks/{task_uid}/download  下载产物（未完成 40902）

装配方式：ReportAssembler / ExportTaskManager 经 ``create_report_app`` 注入；
渲染器注册表随应用注入（缺省 MarkdownRenderer；xlsx/pdf 生产渲染器以注入接入，
core 不耦合具体库）。错误码：40001 入参 / 40404 不存在 / 40902 状态冲突。
"""
from __future__ import annotations

from foundation.core.envelope import envelope

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from services.training.core.board.report import (
    BatchNotFound,
    ExportRunningError,
    ExportTaskManager,
    ReportAssembler,
    TraineeNotFound,
    UnknownRendererError,
)

# --------------------------------------------------------------------------- 路径与错误码常量

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
REPORTS_PREFIX: str = "/api/v1/training/reports"
BATCH_EXPORT_PATH: str = f"{REPORTS_PREFIX}/batch/{{batch_id}}"
TRAINEE_EXPORT_PATH: str = f"{REPORTS_PREFIX}/trainee/{{trainee_id}}"
TASK_PATH: str = f"{REPORTS_PREFIX}/tasks/{{task_uid}}"
TASK_DOWNLOAD_PATH: str = f"{REPORTS_PREFIX}/tasks/{{task_uid}}/download"

#: 业务码（04-接口设计 §1.3）：成功 / 入参 / 不存在 / 状态冲突
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40404
CODE_CONFLICT: int = 40902

#: 产物媒体类型（下载端点 Content-Type 口径）
MEDIA_TYPES: dict[str, str] = {
    ".md": "text/markdown; charset=utf-8",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（CR A2 全面委托化：foundation 单点实现）。"""
    return envelope(code, message, data, trace_id)


def _trace_id_of(request: Request) -> str:
    """trace_id：X-Trace-Id 头优先，缺失新生成（04 §1.4）。"""
    return (request.headers.get("X-Trace-Id") or "").strip() or uuid.uuid4().hex[:16]


class ExportRequest(BaseModel):
    """导出任务入参：格式与章节勾选（缺省 Markdown 全章节）。"""

    model_config = ConfigDict(frozen=True)

    format: str = Field(default="md", description="导出格式（须为已注册渲染器：md/xlsx/…）")
    sections: list[int] | None = Field(default=None, description="勾选章节编号（缺省全部）")


# --------------------------------------------------------------------------- 应用工厂


def create_report_app(
    *,
    assembler: ReportAssembler,
    task_manager: ExportTaskManager,
    version: str = "0.1.0",
) -> FastAPI:
    """组装报告导出应用（装配器与任务管理器注入；渲染器注册表在 manager 内）。"""
    app = FastAPI(
        title="training-service 培训效果报告 API",
        version=version,
        description="FR-TR-009 培训效果报告（SR-TR-009）：批次/个人双维度导出 + 任务状态机 + 下载。",
    )

    def _error(request: Request, status: int, code: int, message: str) -> JSONResponse:
        """错误信封（trace_id 贯穿）。"""
        return JSONResponse(status_code=status, content=_envelope(_trace_id_of(request), code, message, None))

    @app.exception_handler(BatchNotFound)
    async def _on_batch_not_found(request: Request, exc: BatchNotFound) -> JSONResponse:
        """批次不存在 → HTTP 404 + 40404（E_BATCH_NOT_FOUND）。"""
        return _error(request, 404, exc.code, str(exc))

    @app.exception_handler(TraineeNotFound)
    async def _on_trainee_not_found(request: Request, exc: TraineeNotFound) -> JSONResponse:
        """学员不存在 → HTTP 404 + 40404（E_TRAINEE_NOT_FOUND）。"""
        return _error(request, 404, exc.code, str(exc))

    @app.exception_handler(ExportRunningError)
    async def _on_export_running(request: Request, exc: ExportRunningError) -> JSONResponse:
        """同参数任务进行中 → HTTP 409 + 40902（E_EXPORT_RUNNING）。"""
        return _error(request, 409, exc.code, str(exc))

    @app.exception_handler(UnknownRendererError)
    async def _on_unknown_renderer(request: Request, exc: UnknownRendererError) -> JSONResponse:
        """未注册格式 → HTTP 400 + 40001。"""
        return _error(request, 400, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """入参校验失败 → HTTP 400 + 40001。"""
        return _error(request, 400, CODE_BAD_REQUEST, "参数校验失败")

    router = APIRouter(prefix=REPORTS_PREFIX, tags=["training-report"])

    def _submit(
        request: Request, dimension: str, target_id: int, payload: ExportRequest | None
    ) -> JSONResponse:
        """双维度提交共用：任务创建 + 缺省内联执行（demo 同步完成状态机）。"""
        body = payload if payload is not None else ExportRequest()
        task = task_manager.submit(
            dimension, target_id,
            requested_by=request.headers.get("X-User-Id", "demo"),
            fmt=body.format,
            sections=body.sections,
        )
        return JSONResponse(content=_envelope(
            _trace_id_of(request), CODE_OK, "ok",
            {
                "task_uid": task.uid,
                "dimension": task.dimension,
                "target_id": task.target_id,
                "format": task.fmt,
                "status": task.status,
                "download_ref": task.download_ref,
            },
        ))

    @router.post("/batch/{batch_id}", summary="创建批次效果报告导出任务")
    def export_batch(batch_id: int, request: Request, payload: ExportRequest | None = None) -> JSONResponse:
        """批次维度导出（SR §5：同目标同参数未终态任务拒绝重复提交）。"""
        return _submit(request, "batch", batch_id, payload)

    @router.post("/trainee/{trainee_id}", summary="创建个人培训报告导出任务")
    def export_trainee(
        trainee_id: int, request: Request, payload: ExportRequest | None = None
    ) -> JSONResponse:
        """个人维度导出（历程/成绩单/雷达演进/证书状态四区）。"""
        return _submit(request, "individual", trainee_id, payload)

    @router.get("/tasks/{task_uid}", summary="导出任务状态")
    def task_status(task_uid: str, request: Request) -> JSONResponse:
        """任务查询（状态机观测口；下载引用仅 done 任务携带）。"""
        task = task_manager.store.get(task_uid)
        if task is None:
            return _error(request, 404, CODE_NOT_FOUND, f"任务不存在：{task_uid}")
        return JSONResponse(content=_envelope(_trace_id_of(request), CODE_OK, "ok", task.to_dict()))

    @router.get("/tasks/{task_uid}/download", summary="下载导出产物")
    def download(task_uid: str, request: Request) -> Response:
        """下载产物：仅 done 任务可下载（未完成 40902；文件缺失 40404）。"""
        task = task_manager.store.get(task_uid)
        if task is None:
            return _error(request, 404, CODE_NOT_FOUND, f"任务不存在：{task_uid}")
        if task.status != "done" or task.file_key is None:
            return _error(request, 409, CODE_CONFLICT, f"任务未完成，禁止下载（当前状态：{task.status}）")
        file_path = Path(task.file_key)
        if not file_path.exists():
            return _error(request, 404, CODE_NOT_FOUND, "产物文件缺失（可能已过期清理），请重新导出")
        media_type = MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
        return Response(
            content=file_path.read_bytes(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
        )

    app.include_router(router)
    return app


__all__ = [
    "REPORTS_PREFIX",
    "BATCH_EXPORT_PATH",
    "TRAINEE_EXPORT_PATH",
    "TASK_PATH",
    "TASK_DOWNLOAD_PATH",
    "CODE_OK",
    "CODE_BAD_REQUEST",
    "CODE_NOT_FOUND",
    "CODE_CONFLICT",
    "ExportRequest",
    "create_report_app",
]
