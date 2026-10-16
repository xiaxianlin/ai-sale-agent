# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""检索质量评测管理端点（FR-KB-016 / SR-KB-016 §5、§6.2，迭代 0.3）。

以 APIRouter 交付（不改 services/kb/app.py，与 admin 包其他路由同一交付形态）：
- POST /api/v1/knowledge/evaluate        触发评测任务（异步状态机，立即返回 job_id）；
- GET  /api/v1/knowledge/evaluate/{job_id} 查询运行（结果 + 报告下载引用）。

异步状态机（pending → running → done | failed，单向不可逆）：
- POST 校验内联评测集（重复 case_id / 非法 kb_type 等 → 40001 统一信封）后
  提交后台线程执行（生产为任务队列 + eval_run 表持久化，本迭代以内存任务
  注册表承接同一契约，重启丢失可接受——评测可重放）；
- 执行体：EvalRunner（search_fn 由装配方注入，测试为确定性桩、生产为
  RetrievalPipeline 包装）→ aggregate → classify_misses → evaluate_gate →
  报告落盘（md + xlsx）→ 终态；
- GET 返回任务快照：终态携带 metrics / gate / 报告文件路径与下载引用 URI
  （下载端点随报告导出迭代接入，先以引用形式透出）。

响应遵循 04-接口设计 §1.3 统一信封 {code, message, data, trace_id}；错误码：
40001 参数校验失败（含评测集语义校验）、40401 任务不存在。
"""
from __future__ import annotations

from foundation.core.envelope import envelope

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.kb.core.evaluation import (
    DEFAULT_CONCURRENCY,
    TOP_K,
    AttributionContext,
    DatasetError,
    DimensionMetrics,
    EvalCase,
    EvalDataset,
    EvalRunner,
    EvaluationReport,
    KB_TYPES,
    LatencyStats,
    RunMetrics,
    classify_misses,
    evaluate_gate,
    now_iso,
    write_markdown,
    write_xlsx,
)
from services.kb.core.evaluation import aggregate as aggregate_metrics

#: 触发评测端点（SR-KB-016 §5；报告下载引用的 URI 前缀）
EVAL_PATH: str = "/api/v1/knowledge/evaluate"
#: 运行查询端点（绝对路径）
EVAL_JOB_PATH: str = "/api/v1/knowledge/evaluate/{job_id}"

#: 路由相对路径（router 已带 /api/v1/knowledge 前缀，装饰器用相对形态）
_ROUTE_TRIGGER: str = "/evaluate"
_ROUTE_JOB: str = "/evaluate/{job_id}"

#: 参数校验失败业务码（04-接口设计 §1.3，与 api/routes.py 同码表）
CODE_BAD_REQUEST: int = 40001
#: 资源不存在业务码（任务未找到）
CODE_NOT_FOUND: int = 40401

#: 任务状态机取值（单向：pending → running → done | failed）
JOB_PENDING: str = "pending"
JOB_RUNNING: str = "running"
JOB_DONE: str = "done"
JOB_FAILED: str = "failed"
JOB_TERMINAL_STATES: frozenset[str] = frozenset({JOB_DONE, JOB_FAILED})

#: 触发方式枚举（SR-KB-016 §4 eval_run.trigger）
TRIGGER_CHOICES: tuple[str, ...] = ("manual", "ci", "pipeline_gate")

#: 报告输出缺省目录（报告 md/xlsx 同前缀落盘；生产挂卷持久化）
DEFAULT_REPORT_DIR: str = "out/eval_reports"


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（CR A2 全面委托化：foundation 单点实现）。"""
    return envelope(code, message, data, trace_id)


def _request_trace_id(request: Request) -> str:
    """trace_id 解析（04 §1.4 口径）：优先 X-Trace-Id 头，缺失新生成 uuid。"""
    return request.headers.get("X-Trace-Id") or uuid.uuid4().hex


# --------------------------------------------------------------------------- 请求模型


class EvalCaseIn(BaseModel):
    """内联用例模型（SR-KB-016 §5「内联上传临时集」的用例行）。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=1, max_length=64, description="用例编号（集内唯一）")
    query: str = Field(..., min_length=1, max_length=512, description="查询文本（真实业务问法）")
    expected_chunk_ids: list[int] = Field(..., min_length=1, description="期望 Top5 命中的块主键集合")
    kb_type: str = Field(..., description="知识库类型（六大库枚举）")
    dimensions: dict[str, str] = Field(default_factory=dict, description="自定义维度标签")
    filters: dict[str, list[str]] | None = Field(None, description="可选结构化过滤（白名单维度）")

    @field_validator("kb_type")
    @classmethod
    def _check_kb_type(cls, value: str) -> str:
        """kb_type 枚举校验（40001 定位信息）。"""
        if value not in KB_TYPES:
            raise ValueError(f"kb_type 含未知类型: {value!r}（合法值 {list(KB_TYPES)}）")
        return value

    def to_core_case(self) -> EvalCase:
        """转核心用例模型（dimension_groups/expected 语义由核心层承接）。"""
        return EvalCase(
            case_id=self.case_id,
            query=self.query,
            expected_chunk_ids=tuple(self.expected_chunk_ids),
            kb_type=self.kb_type,
            dimensions=dict(self.dimensions),
            filters=dict(self.filters) if self.filters is not None else None,
        )


class EvalDatasetIn(BaseModel):
    """内联评测集模型（≤500 条不入库的临时集，SR-KB-016 §5）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=64, description="评测集名称")
    version: int = Field(..., ge=1, description="版本号（同名递增）")
    cases: list[EvalCaseIn] = Field(..., min_length=1, max_length=500, description="用例列表")
    status: str = Field("draft", description="评测集状态（draft/reviewing/frozen/retired）")


class DimensionMetricsIn(BaseModel):
    """基线分维度指标模型。"""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(..., ge=1)
    hit_count: int = Field(..., ge=0)
    top5_acc: float = Field(..., ge=0.0, le=1.0)
    mrr: float = Field(0.0, ge=0.0)


class BaselineIn(BaseModel):
    """基线指标模型（最近一次 gate=pass 运行的聚合快照）。"""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(..., ge=1)
    hit_count: int = Field(..., ge=0)
    top5_acc: float = Field(..., ge=0.0, le=1.0)
    mrr: float = Field(0.0, ge=0.0)
    per_dimension: dict[str, DimensionMetricsIn] = Field(default_factory=dict)


class EvaluateRequest(BaseModel):
    """触发评测请求（SR-KB-016 §5 入参的迭代 0.3 子集：内联数据集 + 门禁参数）。"""

    model_config = ConfigDict(extra="forbid")

    dataset: EvalDatasetIn
    top_k: int = Field(TOP_K, ge=1, le=10, description="命中判定窗口（Top5 口径勿改）")
    concurrency: int = Field(
        DEFAULT_CONCURRENCY, ge=1, le=32, description="批量评测并发（KB_EVAL_CONCURRENCY）"
    )
    threshold_pp: float = Field(2.0, ge=0.0, le=10.0, description="相对基线最大回落（pp）")
    floor_pct: float | None = Field(
        None, ge=0.0, le=100.0, description="绝对门槛（百分比；None=仅相对门槛）"
    )
    trigger: str = Field("manual", description="触发方式（manual/ci/pipeline_gate）")
    baseline: BaselineIn | None = Field(None, description="基线指标（缺省=首次运行口径）")
    baseline_label: str = Field("", max_length=128, description="基线标注（报告展示）")

    @field_validator("trigger")
    @classmethod
    def _check_trigger(cls, value: str) -> str:
        """触发方式枚举校验。"""
        if value not in TRIGGER_CHOICES:
            raise ValueError(f"trigger 含未知取值: {value!r}（合法值 {list(TRIGGER_CHOICES)}）")
        return value


def _to_dataset(payload: EvalDatasetIn) -> EvalDataset:
    """请求模型 → 核心评测集（语义校验：重复 case_id 等由 DatasetError 承载）。"""
    return EvalDataset(
        name=payload.name,
        version=payload.version,
        status=payload.status,
        cases=[
            case.to_core_case()
            for case in payload.cases
        ],
    )


def _to_run_metrics(payload: BaselineIn) -> RunMetrics:
    """基线请求模型 → 核心聚合指标（延迟字段缺省 0：基线不参与延迟判定）。"""
    return RunMetrics(
        n=payload.n,
        hit_count=payload.hit_count,
        top5_acc=payload.top5_acc,
        mrr=payload.mrr,
        latency=LatencyStats(),
        per_dimension={
            key: DimensionMetrics(
                key=key, n=dim.n, hit_count=dim.hit_count, top5_acc=dim.top5_acc, mrr=dim.mrr
            )
            for key, dim in payload.per_dimension.items()
        },
    )


# --------------------------------------------------------------------------- 任务模型与注册表


@dataclass
class EvalJob:
    """评测任务（内存态；生产对应 eval_run 行）。字段读写均在注册表锁内。"""

    job_id: str
    status: str
    created_at: str
    case_count: int
    finished_at: str | None = None
    metrics: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    report: dict[str, str] | None = None
    error: str | None = None


class EvalJobStore:
    """内存任务注册表（线程安全）：状态机流转 + 快照读取。

    状态单向流转（pending → running → done/failed），终态不可再变更；
    生产形态为 eval_run 表 + 任务队列，本迭代以进程内注册表承接。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, EvalJob] = {}

    def create(self, job_id: str, case_count: int, created_at: str) -> EvalJob:
        """登记新任务（pending）。"""
        job = EvalJob(
            job_id=job_id, status=JOB_PENDING, created_at=created_at, case_count=case_count
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def transition(self, job_id: str, status: str) -> None:
        """状态流转（终态后拒绝再流转，防御并发误写）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in JOB_TERMINAL_STATES:
                return
            job.status = status

    def finish(
        self,
        job_id: str,
        *,
        metrics: dict[str, Any],
        gate: dict[str, Any],
        report: dict[str, str],
        finished_at: str,
    ) -> None:
        """写终态结果（done）：指标/门禁/报告引用与状态一次性提交。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in JOB_TERMINAL_STATES:
                return
            job.metrics = metrics
            job.gate = gate
            job.report = report
            job.finished_at = finished_at
            job.status = JOB_DONE

    def fail(self, job_id: str, error: str, finished_at: str) -> None:
        """写终态结果（failed）：错误信息留痕。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status in JOB_TERMINAL_STATES:
                return
            job.error = error
            job.finished_at = finished_at
            job.status = JOB_FAILED

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        """任务快照（锁内拷贝，供响应组装；未登记返回 None）。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "job_id": job.job_id,
                "status": job.status,
                "created_at": job.created_at,
                "case_count": job.case_count,
                "finished_at": job.finished_at,
                "metrics": job.metrics,
                "gate": job.gate,
                "report": job.report,
                "error": job.error,
            }


class EvalAdmin:
    """评测管理服务：注入 search_fn 与缺省门禁参数，承载任务执行。

    search_fn 为被测检索链路的鸭子类型包装（query/kb_types/filters/top_k →
    results[{chunk_id, score}]；生产传 RetrievalPipeline 包装，测试传桩）。
    """

    def __init__(
        self,
        search_fn: Any,
        *,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        attribution_context: AttributionContext | None = None,
    ) -> None:
        self._search_fn = search_fn
        self._report_dir = Path(report_dir)
        self._attribution_context = attribution_context
        self.store = EvalJobStore()

    def submit(
        self,
        dataset: EvalDataset,
        baseline: RunMetrics | None,
        *,
        top_k: int,
        concurrency: int,
        threshold_pp: float,
        floor_pct: float | None,
        trigger: str,
        baseline_label: str,
    ) -> str:
        """登记任务并启动后台线程执行（POST 立即返回，前端/CI 轮询 GET）。"""
        job_id = uuid.uuid4().hex
        self.store.create(job_id, dataset.case_count, now_iso())
        worker = threading.Thread(
            target=self._execute,
            name=f"kb-eval-{job_id[:8]}",
            daemon=True,
            kwargs={
                "job_id": job_id,
                "dataset": dataset,
                "baseline": baseline,
                "top_k": top_k,
                "concurrency": concurrency,
                "threshold_pp": threshold_pp,
                "floor_pct": floor_pct,
                "trigger": trigger,
                "baseline_label": baseline_label,
            },
        )
        worker.start()
        return job_id

    def _execute(  # noqa: PLR0913  # 状态机参数集（与 submit 一一对应）
        self,
        job_id: str,
        dataset: EvalDataset,
        baseline: RunMetrics | None,
        top_k: int,
        concurrency: int,
        threshold_pp: float,
        floor_pct: float | None,
        trigger: str,
        baseline_label: str,
    ) -> None:
        """执行体：跑批 → 聚合 → 归因 → 门禁 → 报告落盘 → 终态。"""
        self.store.transition(job_id, JOB_RUNNING)
        try:
            runner = EvalRunner(self._search_fn, concurrency=concurrency, top_k=top_k)
            results = runner.run(dataset.cases)
            metrics = aggregate_metrics(results, dataset.cases)
            attributions = classify_misses(
                results, dataset.cases, context=self._attribution_context
            )
            gate = evaluate_gate(metrics, baseline, threshold_pp, floor_pct=floor_pct)
            report = EvaluationReport.build(
                run_id=job_id,
                dataset=dataset,
                results=results,
                metrics=metrics,
                attributions=attributions,
                baseline=baseline,
                baseline_label=baseline_label,
                gate=gate,
                trigger=trigger,
            )
            markdown_path = write_markdown(report, self._report_dir / f"{job_id}.md")
            xlsx_path = write_xlsx(report, self._report_dir / f"{job_id}.xlsx")
        except Exception as exc:  # noqa: BLE001  # 任务级兜底：任何异常落 failed 终态
            self.store.fail(job_id, f"{type(exc).__name__}: {exc}", now_iso())
            return
        self.store.finish(
            job_id,
            metrics=metrics.to_dict(),
            gate=gate.to_dict(),
            report={
                "markdown_path": str(markdown_path),
                "xlsx_path": str(xlsx_path),
                "download_markdown": f"{EVAL_PATH}/{job_id}/report?fmt=md",
                "download_xlsx": f"{EVAL_PATH}/{job_id}/report?fmt=xlsx",
            },
            finished_at=now_iso(),
        )


# --------------------------------------------------------------------------- 路由

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge-admin-eval"])


def install_evaluation(
    app: FastAPI,
    search_fn: Any,
    *,
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    attribution_context: AttributionContext | None = None,
) -> EvalAdmin:
    """装配评测端点：注册路由 + 注入 EvalAdmin（挂 app.state.eval_admin）。

    与 admin 包 configure_admin 同一交付形态（不改 services/kb/app.py）；
    幂等：重复调用只重挂 state（FastAPI include_router 会去重校验由调用方保证，
    本函数每次都会追加路由——与 configure_admin 的不幂等口径一致）。
    """
    admin = EvalAdmin(search_fn, report_dir=report_dir, attribution_context=attribution_context)
    app.state.eval_admin = admin
    app.include_router(router)
    return admin


def get_eval_admin(request: Request) -> EvalAdmin:
    """依赖：从 app.state 取评测管理服务（install_evaluation 注入）。"""
    admin = getattr(request.app.state, "eval_admin", None)
    if admin is None:
        raise RuntimeError("评测管理服务未装配：install_evaluation 负责注入 app.state.eval_admin")
    return admin  # type: ignore[no-any-return]


@router.post(_ROUTE_TRIGGER, summary="触发检索质量评测（FR-KB-016，异步任务）")
async def trigger_evaluation(
    body: EvaluateRequest,
    request: Request,
    admin: EvalAdmin = Depends(get_eval_admin),
) -> JSONResponse:
    """触发评测任务：语义校验（40001）→ 登记并后台执行 → 返回 job_id 与初始状态。"""
    trace_id = _request_trace_id(request)
    try:
        dataset = _to_dataset(body.dataset)
    except DatasetError as exc:
        return JSONResponse(
            status_code=400,
            content=_envelope(trace_id, CODE_BAD_REQUEST, f"评测集校验失败: {exc}", None),
        )
    baseline = _to_run_metrics(body.baseline) if body.baseline is not None else None
    job_id = admin.submit(
        dataset,
        baseline,
        top_k=body.top_k,
        concurrency=body.concurrency,
        threshold_pp=body.threshold_pp,
        floor_pct=body.floor_pct,
        trigger=body.trigger,
        baseline_label=body.baseline_label,
    )
    return JSONResponse(
        status_code=200,
        content=_envelope(
            trace_id, 0, "ok",
            {"job_id": job_id, "status": JOB_PENDING, "case_count": dataset.case_count},
        ),
    )


@router.get(_ROUTE_JOB, summary="查询评测运行（结果 + 报告下载引用）")
async def get_evaluation_job(job_id: str, request: Request) -> JSONResponse:
    """查询任务快照：done 携带 metrics/gate/report；failed 携带 error；未知 40401。"""
    trace_id = _request_trace_id(request)
    admin = get_eval_admin(request)
    snapshot = admin.store.snapshot(job_id)
    if snapshot is None:
        return JSONResponse(
            status_code=404,
            content=_envelope(trace_id, CODE_NOT_FOUND, f"评测任务不存在: {job_id}", None),
        )
    data = {
        "job_id": snapshot["job_id"],
        "status": snapshot["status"],
        "case_count": snapshot["case_count"],
        "created_at": snapshot["created_at"],
        "finished_at": snapshot["finished_at"],
    }
    if snapshot["status"] == JOB_DONE:
        data["metrics"] = snapshot["metrics"]
        data["gate"] = snapshot["gate"]
        data["report"] = snapshot["report"]
    elif snapshot["status"] == JOB_FAILED:
        data["error"] = snapshot["error"]
    return JSONResponse(status_code=200, content=_envelope(trace_id, 0, "ok", data))
