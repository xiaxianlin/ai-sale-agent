# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识库批量导入管理端路由（FR-KB-021 / SR-KB-021 §5，迭代 0.3）。

职责边界：路由层零业务实现——全部逻辑在 core/import_service（pipeline/template），
ImportService 经 ``app.state.import_service`` 注入（装配由 admin 应用工厂负责）。

端点（04-接口设计 §3.1「批量导入 POST /api/v1/knowledge/import；异步任务；错误
行报告下载」的细化展开，SR-KB-021 §5 偏差标注：向下兼容新增子资源）：

- POST /api/v1/knowledge/import            上传→四重校验→建档→异步任务→202 + job_id
- GET  /api/v1/knowledge/import/template   模板下载（xlsx 含下拉+填写说明 / csv 列头+示例）
- GET  /api/v1/knowledge/import/{job_id}   任务进度与结果（含错误行报告下载引用）
- GET  /api/v1/knowledge/import/{job_id}/errors  错误行报告 xlsx 下载（防公式注入）

统一信封 {code, message, data, trace_id}（04-接口设计 §1.3）；业务错误码：
40001（上传文件/参数不合法，人话文案）、40401（任务不存在）。

偏差标注：
- 鉴权与审计（40101/40301，kb:import RBAC / audit_log，NFR-SEC-02/03）由 admin
  包统一装配层承接（并行迭代），本路由不内置鉴权依赖；
- Redis Stream worker（SR-KB-021 §6.3）以 ImportService 的 asyncio 任务模型承接，
  接口契约不变。

依据文档：docs/07-实现方案/KB/SR-KB-021-批量导入.md §5、§8；
         docs/02-设计文档/04-接口设计.md §1.3、§3.1。
"""
from __future__ import annotations

import asyncio
import io
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.kb.core.import_service import (
    STATUS_QUEUED,
    STATUS_RUNNING,
    XLSX_MEDIA_TYPE,
    ImportService,
    ImportValidationError,
    JobNotFoundError,
)

#: 上传参数不合法业务码（04-接口设计 §1.3）
CODE_BAD_REQUEST: int = 40001
#: 任务不存在业务码（04-接口设计 §1.3）
CODE_NOT_FOUND: int = 40401

router = APIRouter(prefix="/api/v1", tags=["knowledge-import"])


# --------------------------------------------------------------------------- 响应模型与信封


class ImportAcceptedData(BaseModel):
    """202 受理回执：任务号 + 初始状态（前端凭 job_id 轮询进度）。"""

    job_id: str = Field(description="导入任务号（后续进度/报告查询的主键）")
    status: str = Field(description="受理时任务状态（恒为 queued）")


class ImportStatusData(BaseModel):
    """任务进度视图（SR-KB-021 §5 任务状态端点的 data 载荷）。"""

    job_id: str
    status: str = Field(description="任务状态：queued=排队 | running=导入中 | done=完成 | failed=失败（可续跑）")
    progress_pct: int = Field(ge=0, le=100, description="进度百分比（0~100）")
    total_rows: int = Field(description="数据行总数（不含表头）")
    processed_rows: int = Field(description="已处理行水位")
    valid_rows: int = Field(description="成功导入行数")
    error_rows: int = Field(description="错误行数（含查重跳过）")
    dup_rows: int = Field(description="查重跳过行数")
    error_count: int = Field(description="错误行明细条数（与错误行报告一致）")
    error_report_url: str | None = Field(description="错误行报告下载地址（无错误行时为空）")
    kb_type: str
    file_name: str
    import_mode: str
    target_status: str
    error_detail: str = Field(description="任务级失败原因（done 时空串）")
    created_by: str
    created_at: str
    updated_at: str


def _trace_id(request: Request) -> str:
    """trace_id（04-接口设计 §1.4）：优先 X-Trace-Id 头，缺失新生成。"""
    return request.headers.get("X-Trace-Id") or uuid4().hex


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封：四键齐全（04-接口设计 §1.3）。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def _error(trace_id: str, http_status: int, code: int, message: str) -> JSONResponse:
    """错误信封响应（保持与统一信封同一结构）。"""
    return JSONResponse(
        status_code=http_status, content=_envelope(trace_id, code, message, None)
    )


def _get_service(request: Request) -> ImportService:
    """依赖：从 app.state 取导入服务（admin 应用工厂装配注入）。"""
    service = getattr(request.app.state, "import_service", None)
    if service is None:
        raise RuntimeError("导入服务未装配：请在应用工厂注入 app.state.import_service")
    return service


def _file_response(data: bytes, media_type: str, filename: str) -> StreamingResponse:
    """文件流响应：中文文件名走 RFC 5987（filename*），ASCII 名兜底。"""
    ascii_name = "".join(ch if ord(ch) < 128 else "_" for ch in filename)
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
        )
    }
    return StreamingResponse(io.BytesIO(data), media_type=media_type, headers=headers)


# --------------------------------------------------------------------------- 端点
# 注：/import/template 必须先于 /import/{job_id} 声明，避免路径参数吞掉固定子路径。


@router.get("/knowledge/import/template", summary="下载导入模板（FR-KB-021：xlsx 含下拉与填写说明 / csv 列头+示例）")
async def download_import_template(
    request: Request,
    kb_type: str = Query(default="product", description="六大知识库类型"),
    fmt: str = Query(default="xlsx", alias="format", description="模板格式：xlsx | csv"),
) -> StreamingResponse:
    """模板下载（SR-KB-021 §5）：模板列头/下拉/说明与后端校验同源（FR-KB-020 注册表）。"""
    trace_id = _trace_id(request)
    service = _get_service(request)
    try:
        data, media_type, filename = service.template_file(kb_type, fmt)
    except ImportValidationError as exc:
        return _error(trace_id, 400, CODE_BAD_REQUEST, str(exc))
    return _file_response(data, media_type, filename)


@router.post(
    "/knowledge/import",
    status_code=202,
    summary="上传批量导入文件（FR-KB-021：异步任务，毫秒级受理返回，NFR-PER-05）",
)
async def create_import(
    request: Request,
    file: UploadFile = File(description="xlsx/csv 模板文件（列头须与模板一致）"),
    kb_type: str = Form(description="六大知识库类型（product/customer/skill/process/negative_case/training）"),
    import_mode: str = Form(default="skip", description="重复处理策略：skip（跳过，默认）| overwrite（覆盖）"),
    target_status: str = Form(default="draft", description="入库状态：draft（草稿，默认）| published（发布）"),
    created_by: str = Form(default="admin", description="操作人（审计留痕）"),
) -> JSONResponse:
    """上传→四重校验（格式/大小/kb_type/行数，超限 40001 人话报错）→建档
    queued → 异步任务启动 → 202 返回 job_id（重活全在后台任务）。"""
    trace_id = _trace_id(request)
    service = _get_service(request)
    payload = await file.read()
    try:
        job = service.create_job(
            file_name=file.filename or "upload",
            file_bytes=payload,
            kb_type=kb_type,
            import_mode=import_mode,
            target_status=target_status,
            created_by=created_by or "admin",
        )
    except ImportValidationError as exc:
        return _error(trace_id, 400, CODE_BAD_REQUEST, str(exc))
    service.submit(job.job_id)
    # 让出一步：后台任务即刻进入 running（协作式调度；大文件按分片继续推进）
    await asyncio.sleep(0)
    return JSONResponse(
        status_code=202,
        content=_envelope(
            trace_id, 0, "已受理：文件校验通过，导入任务已创建",
            ImportAcceptedData(job_id=job.job_id, status=job.status).model_dump(),
        ),
    )


@router.get("/knowledge/import/{job_id}", summary="查询导入任务进度与结果")
async def get_import_status(request: Request, job_id: str) -> JSONResponse:
    """任务进度：状态/百分比/四类计数 + 错误行报告下载引用（有错误行时）。"""
    trace_id = _trace_id(request)
    service = _get_service(request)
    try:
        job = service.job_store.require(job_id)
    except JobNotFoundError:
        return _error(trace_id, 404, CODE_NOT_FOUND, f"导入任务不存在：{job_id}")
    if job.status in (STATUS_QUEUED, STATUS_RUNNING):
        # 协作让步：给后台任务一个调度片段（分片边界推进），轮询即前进
        await asyncio.sleep(0)
    view = service.status_view(job)
    view["error_report_url"] = (
        f"/api/v1/knowledge/import/{job_id}/errors" if job.errors else None
    )
    return JSONResponse(
        status_code=200,
        content=_envelope(trace_id, 0, "成功", ImportStatusData(**view).model_dump()),
    )


@router.get("/knowledge/import/{job_id}/errors", summary="下载错误行报告（xlsx：错误明细 + 原始数据标注）")
async def download_error_report(request: Request, job_id: str) -> StreamingResponse:
    """错误行报告 xlsx 下载（SR-KB-021 §6.3）：行号/列/原因/修改建议 +
    原始数据标注 sheet；写出前防公式注入。任务未完成时返回当前已发现的错误行。"""
    trace_id = _trace_id(request)
    service = _get_service(request)
    try:
        job = service.job_store.require(job_id)
    except JobNotFoundError:
        return _error(trace_id, 404, CODE_NOT_FOUND, f"导入任务不存在：{job_id}")
    data = service.error_report_bytes(job)
    return _file_response(data, XLSX_MEDIA_TYPE, f"错误行报告_{job_id[:8]}.xlsx")
