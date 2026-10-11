# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果报告导出 API 集成用例（FR-TR-009，SR-TR-009 §5/§9；TDD 阶段①，迭代 1.3）。

端点：POST /api/v1/training/reports/batch/{batch_id}、POST .../reports/trainee/{id}、
GET .../reports/tasks/{uid}、GET .../reports/tasks/{uid}/download。
验收口径映射：统一信封（04 §1.3）；错误码 40404 不存在 / 40902 幂等冲突与未就绪 /
40001 未注册格式；异步任务状态机 queued→done + 下载引用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from services.training.api.report_api import (
    BATCH_EXPORT_PATH,
    TASK_DOWNLOAD_PATH,
    TASK_PATH,
    TRAINEE_EXPORT_PATH,
    create_report_app,
)
from services.training.core.board.report import (
    ExportTaskManager,
    ExportTaskStore,
    MarkdownRenderer,
    ReportAssembler,
)

from .test_board_fakes import FakeReportSource, envelope_ok
from .test_report_export import MinimalXlsxRenderer

BATCH_ID = 1
TRAINEE_ID = 101


def make_source() -> FakeReportSource:
    """报告数据源（3 人批次 + 个人 101，同 test_report_export 口径）。"""
    return FakeReportSource(
        batches={BATCH_ID: {"batch_id": BATCH_ID, "name": "2026Q4 新人第 3 期", "baseline_manual_days": 90}},
        members={BATCH_ID: [
            {"id": 1, "trainee_id": 101, "name": "张三", "status": "certified", "cycle_days": 40,
             "cert_status": "certified", "radar_current": {}, "ex_n": 1, "exam_pass_n": 1},
            {"id": 2, "trainee_id": 102, "name": "李四", "status": "in_training", "cycle_days": None,
             "cert_status": "uncertified", "radar_current": {}, "ex_n": 0, "exam_pass_n": 0},
            {"id": 3, "trainee_id": 103, "name": "王五", "status": "dropped", "cycle_days": None,
             "cert_status": "uncertified", "radar_current": {}, "ex_n": 0, "exam_pass_n": 0},
        ]},
        radar_means={BATCH_ID: {"initial": {}, "current": {}}},
        gaps={BATCH_ID: [{"gap_theme": "价格异议", "count": 3}]},
        individuals={TRAINEE_ID: {
            "profile": {"trainee_id": TRAINEE_ID, "name": "张三", "cert_status": "certified"},
            "timeline": [{"date": "2026-10-01", "kind": "drill", "summary": "演练"}],
            "scores": [
                {"date": "2026-10-01", "kind": "exam", "title": "结业考试", "score": 88.0, "passed": True}
            ],
            "radar_evolution": [{"stage": "latest", "date": "2026-10-01", "radar": {"product": 75.0}}],
            "cycle": {"joined_at": "2026-08-01", "certified_at": "2026-09-15", "cycle_days": 45},
        }},
    )


def make_client(
    tmp_path: Path, *, deferred: bool = False, renderers: dict[str, Any] | None = None
) -> TestClient:
    """报告 API 应用（缺省内联执行器=提交即完成；deferred=True 停在 queued）。"""
    source = make_source()
    manager = ExportTaskManager(
        store=ExportTaskStore(),
        assembler=ReportAssembler(source),
        renderers=renderers if renderers is not None
        else {"md": MarkdownRenderer(), "xlsx": MinimalXlsxRenderer()},
        output_dir=tmp_path / "exports",
        executor=(lambda job: None) if deferred else None,
    )
    return TestClient(create_report_app(assembler=ReportAssembler(source), task_manager=manager))


def test_batch_export_done_with_download_ref(tmp_path: Path) -> None:
    """批次导出：POST → code 0 + task_uid + 状态 done + 下载引用。"""
    resp = make_client(tmp_path).post(BATCH_EXPORT_PATH.format(batch_id=BATCH_ID))
    assert resp.status_code == 200
    body = resp.json()
    envelope_ok(body)
    data = body["data"]
    assert data["dimension"] == "batch"
    assert data["status"] == "done"
    assert data["download_ref"] and data["task_uid"] in data["download_ref"]


def test_batch_export_sections_and_format(tmp_path: Path) -> None:
    """入参透传：sections 勾选与 format=xlsx 均生效（任务记录 + 产物扩展名）。"""
    client = make_client(tmp_path)
    resp = client.post(BATCH_EXPORT_PATH.format(batch_id=BATCH_ID),
                       json={"format": "xlsx", "sections": [2, 3]})
    assert resp.status_code == 200
    task_uid = resp.json()["data"]["task_uid"]
    task = client.get(TASK_PATH.format(task_uid=task_uid)).json()["data"]
    assert task["sections"] == [2, 3]
    assert task["format"] == "xlsx"
    assert (task["file_key"] or "").endswith(".xlsx")


def test_export_unknown_target_40404(tmp_path: Path) -> None:
    """目标不存在：批次/学员未知 → HTTP 404 + code 40404（E_BATCH/E_TRAINEE_NOT_FOUND 语义）。"""
    client = make_client(tmp_path)
    resp_batch = client.post(BATCH_EXPORT_PATH.format(batch_id=99))
    assert resp_batch.status_code == 404
    assert resp_batch.json()["code"] == 40404
    resp_trainee = client.post(TRAINEE_EXPORT_PATH.format(trainee_id=424242))
    assert resp_trainee.status_code == 404
    assert resp_trainee.json()["code"] == 40404


def test_trainee_export_individual(tmp_path: Path) -> None:
    """个人导出：dimension=individual，任务完成携带下载引用。"""
    resp = make_client(tmp_path).post(TRAINEE_EXPORT_PATH.format(trainee_id=TRAINEE_ID))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["dimension"] == "individual"
    assert data["status"] == "done"
    assert data["download_ref"]


def test_task_status_and_download(tmp_path: Path) -> None:
    """任务查询 + 下载：done 任务可下载，内容非空且媒体类型为 Markdown。"""
    client = make_client(tmp_path)
    task_uid = client.post(BATCH_EXPORT_PATH.format(batch_id=BATCH_ID)).json()["data"]["task_uid"]
    status_body = client.get(TASK_PATH.format(task_uid=task_uid)).json()
    envelope_ok(status_body)
    assert status_body["data"]["status"] == "done"
    assert status_body["data"]["file_bytes"] > 0
    download = client.get(TASK_DOWNLOAD_PATH.format(task_uid=task_uid))
    assert download.status_code == 200
    assert "培训效果报告" in download.text
    assert download.headers["content-type"].startswith("text/markdown")


def test_download_before_done_conflict(tmp_path: Path) -> None:
    """下载未完成任务 → HTTP 409 + code 40902（任务未完成，禁止半成品下载）。"""
    client = make_client(tmp_path, deferred=True)
    task_uid = client.post(BATCH_EXPORT_PATH.format(batch_id=BATCH_ID)).json()["data"]["task_uid"]
    download = client.get(TASK_DOWNLOAD_PATH.format(task_uid=task_uid))
    assert download.status_code == 409
    assert download.json()["code"] == 40902


def test_duplicate_submit_running_40902(tmp_path: Path) -> None:
    """幂等提交（API 层）：存在未终态同参任务再次 POST → HTTP 409 + code 40902。"""
    client = make_client(tmp_path, deferred=True)
    first = client.post(BATCH_EXPORT_PATH.format(batch_id=BATCH_ID))
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "queued"  # 延迟执行器：任务排队可见
    second = client.post(BATCH_EXPORT_PATH.format(batch_id=BATCH_ID))
    assert second.status_code == 409
    assert second.json()["code"] == 40902


def test_unknown_format_40001(tmp_path: Path) -> None:
    """未注册渲染格式 → HTTP 400 + code 40001（E_EXPORT_FAILED 前置拦截）。"""
    resp = make_client(tmp_path).post(BATCH_EXPORT_PATH.format(batch_id=BATCH_ID),
                                      json={"format": "pdf"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
