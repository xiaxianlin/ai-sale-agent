# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""知识库清洗报告包（FR-KB-024 数据清洗报告，SR-KB-024 §3 代码落点）。

模块划分（对应 SR-KB-024 §3 app/core/cleanreport/，本迭代合并为单模块交付）：
- IngestLogStore / InMemoryIngestLogStore   留痕数据源（接 0.1 ingest_pipeline_log 语义）
- CleanReportAggregator / AggSnapshot       五要素 + 分库×分格式矩阵聚合（§6.1）
- IssueBuilder / CleanIssue                 遗留问题三分类 + 处置建议 + 幂等同步（§6.2）
- ReportRenderer                            xlsx 三 sheet / md 渲染（§6.3）
- CleanReportService                        触发编排：批次回调 / 每日定时 / 手动（§6.4）

典型用法::

    from services.kb.core.reporting import CleanReportService, InMemoryIngestLogStore

    store = InMemoryIngestLogStore()
    store.append(...)  # 各 stage worker 写留痕（status/batch_id/计数键入 action_detail）
    service = CleanReportService(store)
    report = service.on_batch_complete("B-2026-1008-01")   # 触发一：批次完成回调
    daily = service.on_daily_timer()                        # 触发二：每日定时钩子（02:00 cron）
    xlsx_path, md_text = service.export_files(report)
"""
from .cleaning_report import (
    ALL_STAGES,
    BAD_STATUSES,
    CLASS_CLEAN_CONFLICT,
    CLASS_CN,
    CLASS_MANUAL_REVIEW,
    CLASS_PARSE_FAILED,
    ISSUE_RULES,
    AggSnapshot,
    CleanIssue,
    CleanReportAggregator,
    CleanReportService,
    CleaningReport,
    FileDetail,
    InMemoryIngestLogStore,
    IngestLogRecord,
    IngestLogStore,
    IssueBuilder,
    MAX_DETAIL_ROWS,
    PiiLeakError,
    REPORT_KIND_BATCH,
    REPORT_KIND_DAILY,
    REPORT_KIND_MANUAL,
    STAGE_CORRECT,
    STAGE_DEDUP,
    STAGE_MASK,
    STAGE_PARSE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCESS,
    ReportRenderer,
    ReportScope,
    XLSX_SHEET_NAMES,
    scan_plaintext_pii,
)

__all__ = [
    "ALL_STAGES",
    "BAD_STATUSES",
    "CLASS_CLEAN_CONFLICT",
    "CLASS_CN",
    "CLASS_MANUAL_REVIEW",
    "CLASS_PARSE_FAILED",
    "ISSUE_RULES",
    "AggSnapshot",
    "CleanIssue",
    "CleanReportAggregator",
    "CleanReportService",
    "CleaningReport",
    "FileDetail",
    "InMemoryIngestLogStore",
    "IngestLogRecord",
    "IngestLogStore",
    "IssueBuilder",
    "MAX_DETAIL_ROWS",
    "PiiLeakError",
    "REPORT_KIND_BATCH",
    "REPORT_KIND_DAILY",
    "REPORT_KIND_MANUAL",
    "STAGE_CORRECT",
    "STAGE_DEDUP",
    "STAGE_MASK",
    "STAGE_PARSE",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_SUCCESS",
    "ReportRenderer",
    "ReportScope",
    "XLSX_SHEET_NAMES",
    "scan_plaintext_pii",
]
