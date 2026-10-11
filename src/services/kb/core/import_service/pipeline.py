# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""批量导入流水线（FR-KB-021 / SR-KB-021 §6.2~§6.3，迭代 0.3）。

纯逻辑层（不依赖 Web 框架与存储实现），主链路：

    上传建档（create_job：格式/大小/kb_type/行数四重校验，人话报错）
      → ImportJob 状态机（queued → running → done/failed；failed 可水位续跑）
      → 流式解析（xlsx read-only / csv dtype=str，逐行 yield，绝不整表载入）
      → 逐行三段校验（① Schema 字段 ② 业务规则 ③ 查重——内存指纹集，
        指纹复用 FR-KB-011 的 md5 规则，导入查重与清洗查重同口径）
      → 有效行转条目创建动作（create_item_hook 鸭子类型注入，不直接依赖存储）
      → 错误行报告（行号/字段/原因/修改建议，xlsx 两个 sheet，防公式注入）
      → 导入留痕（ImportLog + 内存存储：处理量/成功/失败/查重计数）

部分成功策略：坏行只报告不入库、好行照常入库；create_item_hook 抛
TransientImportError 表示瞬时故障——分片内重试 retry_per_chunk 次，仍失败降级为
internal 错误行继续（任务不整单失败）；其它异常 → 任务 failed，processed_rows
水位停留，续跑不重复插入。

偏差标注（对 SR-KB-021 §6.2/§6.3 生产形态）：
- Redis Stream 消费者/XAUTOCLAIM 认领 → 以 asyncio 任务模型 + ImportJobStore
  内存存储承接（接口契约不变，worker 形态随部署迭代接入）；
- 分片 PG 事务边界 → 内存钩子无事务回滚，水位粒度从分片细化到行（每行定稿
  即推进 processed_rows），保证 failed 续跑严格不重复创建；
- 文件内查重缓存（seen 指纹→首行行号）随 Job 对象驻留内存（生产为任务表
  + Redis）。

依据文档：docs/07-实现方案/KB/SR-KB-021-批量导入.md §4、§5、§6.2、§6.3、§7、§9；
         docs/02-设计文档/04-接口设计.md §3.1；
         docs/03-技术文档/02-开发规范.md §3。
"""
from __future__ import annotations

import asyncio
import csv
import inspect
import io
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from openpyxl import Workbook, load_workbook

from services.kb.core.pipeline.cleaning.dedup import md5_fingerprint

from .template import (
    ColumnSpec,
    XLSX_MEDIA_TYPE,
    build_workbook,
    column_specs,
    display_label,
    render_csv_bytes,
    sanitize_cell,
    workbook_to_bytes,
)

# --------------------------------------------------------------- 状态机与错误码

#: 任务状态（SR-KB-021 §4：pending 语义在本迭代命名 queued；failed 可续跑）
STATUS_QUEUED: str = "queued"
STATUS_RUNNING: str = "running"
STATUS_DONE: str = "done"
STATUS_FAILED: str = "failed"

#: 错误行四类错误码（对齐 SR-KB-021 §4 knowledge_import_error.error_code）
ERROR_SCHEMA_VIOLATION: str = "schema_violation"
ERROR_BIZ_RULE: str = "biz_rule"
ERROR_DUPLICATE: str = "duplicate"
ERROR_INTERNAL: str = "internal"

#: 导入留痕的源格式口径（ingest_pipeline_log.source_format：xlsx→excel）
SOURCE_FORMAT_MAP: dict[str, str] = {"xlsx": "excel", "csv": "csv"}

#: 留痕阶段常量（与 ingest_pipeline_log.stage 的 import 扩展枚举一致）
STAGE_IMPORT: str = "import"

# Schema 段长度上限（人话报错；超长多为贴错列）
TITLE_MAX_LENGTH: int = 200
CONTENT_MAX_LENGTH: int = 50000
FIELD_VALUE_MAX_LENGTH: int = 2000


def now_iso() -> str:
    """留痕时间戳：UTC ISO-8601（毫秒精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------- 异常体系


class ImportValidationError(Exception):
    """上传/参数校验失败（人话文案）：API 层映射 400 / 40001（SR-KB-021 §5）。"""


class JobNotFoundError(Exception):
    """导入任务不存在：API 层映射 404 / 40401。"""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"导入任务不存在：{job_id}")
        self.job_id = job_id


class TransientImportError(Exception):
    """条目创建瞬时故障（模拟 DB 抖动）：分片内重试，耗尽后降级 internal 错误行。"""


# --------------------------------------------------------------- 行级校验结构


@dataclass(frozen=True)
class RowIssue:
    """单行校验问题（三段校验前两段的产物）：列中文名 + 错误码 + 原因 + 修改建议。"""

    field_label: str
    error_code: str
    message: str
    suggestion: str = ""


#: 业务规则校验器签名：行数据（key→值）→ 问题（None 表示通过）
BizValidator = Callable[[Mapping[str, str]], "RowIssue | None"]


def _builtin_newline_rule(row: Mapping[str, str]) -> RowIssue | None:
    """内置业务规则：标题不得包含换行（Excel 单元格内换行破坏模板口径与列表展示）。"""
    if "\n" in row.get("title", "") or "\r" in row.get("title", ""):
        return RowIssue("标题*", ERROR_BIZ_RULE, "标题不能包含换行符", "请在同一行内填写标题；多段内容请放入「正文」列")
    return None


def _length_limit(spec: ColumnSpec) -> int:
    """按字段键取长度上限：标题/正文专用上限，其余字段通用上限。"""
    if spec.key == "title":
        return TITLE_MAX_LENGTH
    if spec.key == "content":
        return CONTENT_MAX_LENGTH
    return FIELD_VALUE_MAX_LENGTH


def validate_row(
    mapped: Mapping[str, str],
    specs: Sequence[ColumnSpec],
    biz_validators: Sequence[BizValidator] = (),
) -> RowIssue | None:
    """逐行三段校验的前两段（查重为第三段，由流水线按指纹集判定）。

    ① Schema 字段段：必填缺失 / 超出长度上限 / 取值不在选项内 → schema_violation；
    ② 业务规则段：内置规则 + 注入的库特定规则 → biz_rule。
    返回首个问题（首个错误即可定位修改），通过返回 None。
    """
    for spec in specs:
        value = str(mapped.get(spec.key, "") or "").strip()
        if not value:
            if spec.required:
                return RowIssue(
                    display_label(spec), ERROR_SCHEMA_VIOLATION,
                    f"「{spec.label}」为必填项", "请填写该列（表头带 * 号的列均必填）",
                )
            continue
        limit = _length_limit(spec)
        if len(value) > limit:
            return RowIssue(
                display_label(spec), ERROR_SCHEMA_VIOLATION,
                f"「{spec.label}」长度 {len(value)} 超过上限 {limit}",
                "请精简内容，或将长文本放入「正文」列",
            )
        if spec.options and value not in spec.options:
            return RowIssue(
                display_label(spec), ERROR_SCHEMA_VIOLATION,
                f"「{spec.label}」取值「{value}」不在允许范围内",
                "可选值：" + " / ".join(spec.options) + "（模板该列已内置下拉选择）",
            )
    for rule in (_builtin_newline_rule, *biz_validators):
        issue = rule(mapped)
        if issue is not None:
            return issue
    return None


def dedup_fields_for(kb_type: str, schema_provider: Any) -> tuple[str, ...]:
    """查重字段集：恒含 title/content，注册表可经 dedup_fields(kb_type) 追加库特定键。"""
    merged: list[str] = ["title", "content"]
    resolver = getattr(schema_provider, "dedup_fields", None)
    if callable(resolver):
        try:
            extra = tuple(str(key) for key in resolver(kb_type))
        except Exception:  # 注册表未提供时静默回退基础字段（鸭子类型容错）
            extra = ()
        merged.extend(key for key in extra if key not in merged)
    return tuple(merged)


def dedup_fingerprint(kb_type: str, mapped: Mapping[str, str], keys: Sequence[str]) -> str:
    """查重指纹：kb_type + 查重字段（去首尾空白）拼接后取 md5。

    复用 FR-KB-011 清洗去重的 md5 规则（normalize_for_md5 去首尾空白），导入
    查重与库内清洗查重同一权威键口径（「联动 md5 规则」）。
    """
    parts = [kb_type] + [str(mapped.get(key, "") or "").strip() for key in keys]
    return md5_fingerprint("\x1f".join(parts))


# --------------------------------------------------------------- 文件解析（流式）


def _cell_to_str(value: Any) -> str:
    """单元格值规整为字符串：None→空串；整数浮点去 ``.0``；日期取 ISO 格式。

    规整只发生在「读取」侧且不丢失语义；csv 读取天然全字符串（'0012' 前导零
    保留，SR-KB-021 §9 test_csv_leading_zero）。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "isoformat"):  # datetime/date
        return value.isoformat(sep=" ")
    return str(value)


def iter_file_rows(file_bytes: bytes, file_format: str) -> Iterator[list[str]]:
    """流式逐行产出（绝不整表载入）：首个 sheet，单元格已规整为字符串。

    xlsx 用 openpyxl read-only 模式（内存占用与行数无关，SR-KB-021 §6.2）；
    csv 先按 utf-8-sig 解码（兼容 Excel 导出 BOM），失败回退 gbk。
    """
    if file_format == "xlsx":
        workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        try:
            sheet = workbook.worksheets[0]
            for row in sheet.iter_rows(values_only=True):
                yield [_cell_to_str(cell) for cell in row]
        finally:
            workbook.close()
        return
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("gbk", errors="replace")
    for row in csv.reader(io.StringIO(text)):
        yield [cell.strip() for cell in row]


def read_header(file_bytes: bytes, file_format: str) -> list[str]:
    """读表头（首个非空行）；空文件返回空列表。"""
    for row in iter_file_rows(file_bytes, file_format):
        if any(cell for cell in row):
            return row
    return []


def iter_data_rows(file_bytes: bytes, file_format: str) -> Iterator[tuple[int, list[str]]]:
    """产出 (物理行号, 单元格列表)：表头为第 1 行，数据行从第 2 行起。

    行号=文件物理行号（用户可直回文件定位，SR-KB-021 §4）；整行空白跳过
    （不算数据行，行号不回收）。
    """
    for row_no, row in enumerate(iter_file_rows(file_bytes, file_format), start=1):
        if row_no == 1:
            continue  # 表头
        if not any(row):
            continue  # 空白行
        yield row_no, row


def count_data_rows(file_bytes: bytes, file_format: str) -> int:
    """数据行计数（建档时预扫一遍，供进度分母与行数上限防护）。"""
    return sum(1 for _ in iter_data_rows(file_bytes, file_format))


def resolve_header_map(header: Sequence[str], specs: Sequence[ColumnSpec]) -> dict[int, str | None]:
    """表头列 → 字段键映射：按中文列名（带/不带 * 号）或英文键匹配。

    未匹配列映射为 None（忽略，不报错——允许用户加备注列）；列缺失与单元格
    留空在校验段统一表现为必填错误（人话定位到列名）。
    """
    lookup: dict[str, str] = {}
    for spec in specs:
        lookup[spec.label] = spec.key
        lookup[display_label(spec)] = spec.key
        lookup[spec.key] = spec.key
    return {
        index: lookup.get(cell.strip())
        for index, cell in enumerate(header)
        if cell.strip()  # 空表头列不参与映射
    }


def map_row(values: Sequence[str], header_map: Mapping[int, str | None]) -> dict[str, str]:
    """单元格行 → 字段键值对（重复列取首个出现的键）。"""
    mapped: dict[str, str] = {}
    for index, key in header_map.items():
        if key is not None and index < len(values) and key not in mapped:
            mapped[key] = values[index]
    return mapped


def raw_snapshot(header: Sequence[str], values: Sequence[str]) -> dict[str, str]:
    """原始行快照（错误报告回看用）：表头文案 → 原值（报告写出前统一防公式注入）。"""
    snapshot: dict[str, str] = {}
    for index, value in enumerate(values):
        label = header[index].strip() if index < len(header) and header[index].strip() else f"列{index + 1}"
        snapshot.setdefault(label, value)
    return snapshot


# --------------------------------------------------------------- 数据模型


@dataclass
class ImportRowError:
    """错误行明细（对齐 SR-KB-021 §4 knowledge_import_error 行结构）。"""

    row_no: int                      # 文件物理行号
    field_label: str                 # 出错列中文名（整行级错误记「整行」）
    error_code: str                  # schema_violation | biz_rule | duplicate | internal
    message: str                     # 原因（中文人话）
    suggestion: str = ""             # 修改建议（中文人话）
    raw_row: dict[str, str] = field(default_factory=dict)  # 原始行快照


@dataclass(frozen=True)
class NewItem:
    """有效行转出的条目创建动作（create_item_hook 的入参契约）。

    hook 返回条目 id（int）；可为同步或 async 函数。overwrite=True 表示
    覆盖导入模式下的库内重复键更新（FR-KB-022 版本快照由 hook 侧承接）。
    """

    kb_type: str
    title: str
    content: str
    metadata: dict[str, str]
    status: str                 # draft | published（target_status）
    fingerprint: str            # 查重指纹（md5 口径）
    job_id: str
    row_no: int
    overwrite: bool = False


@dataclass
class ImportJob:
    """导入任务（内存形态；对齐 SR-KB-021 §4 knowledge_import_task 列）。"""

    job_id: str
    kb_type: str
    file_name: str
    file_bytes: bytes                     # 上传原件（生产形态为 OSS file_ref）
    file_format: str                      # xlsx | csv
    import_mode: str = "skip"             # skip | overwrite
    target_status: str = "draft"          # draft | published
    status: str = STATUS_QUEUED
    total_rows: int = 0                   # 数据行数（不含表头）
    processed_rows: int = 0               # ★ 已处理行水位（断点续跑/幂等锚点）
    valid_rows: int = 0
    error_rows: int = 0
    dup_rows: int = 0
    item_ids: list[int] = field(default_factory=list)
    errors: list[ImportRowError] = field(default_factory=list)
    error_detail: str = ""                # 任务级失败原因（中文人话）
    created_by: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    status_history: list[str] = field(default_factory=lambda: [STATUS_QUEUED])
    seen_first: dict[str, int] = field(default_factory=dict)  # 指纹 → 首个创建行号

    @property
    def progress_pct(self) -> int:
        """进度百分比（0~100）：进度分母为 total_rows。"""
        if self.total_rows <= 0:
            return 100 if self.status == STATUS_DONE else 0
        return min(100, round(self.processed_rows * 100 / self.total_rows))


@dataclass
class ImportLog:
    """导入留痕（对齐 SR-KB-021 §4 ingest_pipeline_log stage=import 记录）。"""

    job_id: str
    kb_type: str
    file_name: str
    source_format: str = "excel"          # excel | csv（ingest_pipeline_log 口径）
    stage: str = STAGE_IMPORT
    total_rows: int = 0                   # 处理量
    valid_rows: int = 0                   # 成功
    error_rows: int = 0                   # 失败
    dup_rows: int = 0                     # 查重跳过计数
    import_mode: str = "skip"
    target_status: str = "draft"
    status: str = STATUS_DONE             # 任务终态（done/failed 均留痕）
    item_ids: list[int] = field(default_factory=list)
    created_by: str = ""
    created_at: str = field(default_factory=now_iso)


@dataclass(frozen=True)
class ImportConfig:
    """导入配置（SR-KB-021 §7 配置项的内存缺省值）。"""

    allowed_formats: tuple[str, ...] = ("xlsx", "csv")
    max_file_mb: int = 20
    max_rows: int = 20000
    chunk_rows: int = 500                 # 分片行数（调度/降级边界）
    default_target_status: str = "draft"
    default_import_mode: str = "skip"
    retry_per_chunk: int = 3              # 瞬时故障分片重试次数


# --------------------------------------------------------------- 内存存储


class ImportJobStore:
    """任务内存存储（线程安全的 job_id → ImportJob 映射；生产为 PG 任务表）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, ImportJob] = {}

    def add(self, job: ImportJob) -> None:
        """建档入册。"""
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> ImportJob | None:
        """按任务号取任务；不存在返回 None。"""
        with self._lock:
            return self._jobs.get(job_id)

    def require(self, job_id: str) -> ImportJob:
        """按任务号取任务；不存在抛 JobNotFoundError（API 404/40401）。"""
        job = self.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def all(self) -> list[ImportJob]:
        """全部任务快照（调试/管理查询）。"""
        with self._lock:
            return list(self._jobs.values())


class ImportLogStore:
    """导入留痕内存存储（追加账本；生产为 ingest_pipeline_log 表）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._logs: list[ImportLog] = []

    def append(self, log: ImportLog) -> None:
        """追加一条留痕（每次任务终态各写一条：failed 中断与 done 完成均可回溯）。"""
        with self._lock:
            self._logs.append(log)

    def all(self) -> list[ImportLog]:
        """全部留痕快照。"""
        with self._lock:
            return list(self._logs)

    def for_job(self, job_id: str) -> list[ImportLog]:
        """按任务号过滤留痕。"""
        return [log for log in self.all() if log.job_id == job_id]


# --------------------------------------------------------------- 错误行报告


def build_error_report_xlsx(job: ImportJob) -> bytes:
    """错误行报告（SR-KB-021 §6.3）：两个 sheet 的 xlsx 字节流。

    ①「错误明细」：行号/列/错误类型/原因/修改建议（按行号升序）；
    ②「原始数据（错误已标注）」：错误行原始快照 + 导入结果/原因标注列。
    所有字符串单元格写出前过 sanitize_cell（防公式注入：= + - @ 前置单引号）。
    """
    workbook = Workbook()
    detail = workbook.active
    assert detail is not None
    detail.title = "错误明细"
    detail.append(["行号", "列", "错误类型", "原因", "修改建议"])
    ordered = sorted(job.errors, key=lambda error: error.row_no)
    for error in ordered:
        detail.append(
            [
                error.row_no,
                sanitize_cell(error.field_label),
                error.error_code,
                sanitize_cell(error.message),
                sanitize_cell(error.suggestion),
            ]
        )

    raw = workbook.create_sheet("原始数据（错误已标注）")
    columns: list[str] = []
    for error in ordered:  # 列并集保持首次出现顺序（与上传文件列序一致）
        for label in error.raw_row:
            if label not in columns:
                columns.append(label)
    raw.append([sanitize_cell(column) for column in columns] + ["导入结果", "原因"])
    for error in ordered:
        row = [sanitize_cell(error.raw_row.get(column, "")) for column in columns]
        raw.append(row + ["未导入", sanitize_cell(error.message)])
    return workbook_to_bytes(workbook)


# --------------------------------------------------------------- 导入门面（服务层）


class ImportService:
    """导入门面：上传建档 → 异步任务调度 → 状态/错误行/报告/模板查询。

    schema_provider 与 create_item_hook 均为鸭子类型注入（本服务不依赖
    FR-KB-020 注册表与条目存储的具体实现）；existing_fingerprints 为库内
    已存指纹集（查重第三段的库内侧输入，创建成功后自动登记新指纹）。
    """

    def __init__(
        self,
        schema_provider: Any,
        create_item_hook: Callable[[NewItem], "int | asyncio.Future[int]"],
        *,
        config: ImportConfig | None = None,
        job_store: ImportJobStore | None = None,
        log_store: ImportLogStore | None = None,
        existing_fingerprints: set[str] | None = None,
        biz_validators: Sequence[BizValidator] = (),
    ) -> None:
        self.schema_provider = schema_provider
        self.create_item_hook = create_item_hook
        self.config = config if config is not None else ImportConfig()
        self.job_store = job_store if job_store is not None else ImportJobStore()
        self.log_store = log_store if log_store is not None else ImportLogStore()
        self.existing_fingerprints: set[str] = (
            existing_fingerprints if existing_fingerprints is not None else set()
        )
        self.biz_validators: tuple[BizValidator, ...] = tuple(biz_validators)
        self._tasks: set[asyncio.Task[ImportJob]] = set()  # 持引用防 GC（fire-and-forget）

    # ------------------------------------------------ 建档（上传入口）

    def create_job(
        self,
        *,
        file_name: str,
        file_bytes: bytes,
        kb_type: str,
        import_mode: str | None = None,
        target_status: str | None = None,
        created_by: str = "",
    ) -> ImportJob:
        """上传建档：四重校验（人话报错，40001 口径）→ 建 queued 任务入册。

        行数上限防护（xlsx 空读/异常膨胀）与进度分母在此一次预扫完成；
        接口毫秒级返回，重活全在异步任务（NFR-PER-05）。
        """
        file_format = self._check_format(file_name)
        self._check_size(file_bytes)
        self._check_kb_type(kb_type)
        mode = import_mode or self.config.default_import_mode
        target = target_status or self.config.default_target_status
        if mode not in ("skip", "overwrite"):
            raise ImportValidationError("导入模式必须是 skip（跳过重复）或 overwrite（覆盖重复）")
        if target not in ("draft", "published"):
            raise ImportValidationError("入库状态必须是 draft（存为草稿）或 published（直接发布）")
        try:
            total_rows = count_data_rows(file_bytes, file_format)
        except ImportValidationError:
            raise
        except Exception as exc:  # noqa: BLE001  损坏文件（如伪 zip 的 xlsx）统一收敛为人话 40001
            raise ImportValidationError(
                f"文件解析失败，请用模板另存后重新上传（{type(exc).__name__}）"
            ) from exc
        if total_rows == 0:
            raise ImportValidationError("上传文件没有数据行（仅有表头），请按模板填写后再上传")
        if total_rows > self.config.max_rows:
            raise ImportValidationError(
                f"单次导入不超过 {self.config.max_rows} 行，本次 {total_rows} 行，请拆分文件后分批上传"
            )
        job = ImportJob(
            job_id=uuid4().hex,
            kb_type=kb_type,
            file_name=file_name,
            file_bytes=file_bytes,
            file_format=file_format,
            import_mode=mode,
            target_status=target,
            status=STATUS_QUEUED,
            total_rows=total_rows,
            created_by=created_by,
        )
        self.job_store.add(job)
        return job

    def _check_format(self, file_name: str) -> str:
        """扩展名白名单（超集防护由大小/行数上限承接）：返回规整化格式。"""
        extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if extension not in self.config.allowed_formats:
            raise ImportValidationError(
                f"仅支持 {' / '.join(self.config.allowed_formats)} 格式的模板文件，收到「{file_name}」"
            )
        return extension

    def _check_size(self, file_bytes: bytes) -> None:
        """大小上限（超限 40001 人话报错）。"""
        if len(file_bytes) > self.config.max_file_mb * 1024 * 1024:
            raise ImportValidationError(f"文件大小超过 {self.config.max_file_mb}MB 上限，请拆分后分批上传")

    def _check_kb_type(self, kb_type: str) -> None:
        """kb_type 合法性：以注册表为准（未注册即 40001 人话报错）。"""
        try:
            self.schema_provider.fields(kb_type)
        except Exception:
            raise ImportValidationError(
                f"不支持的知识库类型「{kb_type}」，请从六大知识库类型中选择"
            ) from None

    # ------------------------------------------------ 异步任务模型

    def submit(self, job_id: str) -> asyncio.Task[ImportJob]:
        """异步启动任务（fire-and-forget，须在事件循环内调用）。

        持有任务引用防 GC；任务异常不外抛（process 内部已落定为 failed 状态）。
        """
        task = asyncio.get_running_loop().create_task(self.process(job_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def process(self, job_id: str) -> ImportJob:
        """执行/续跑任务（幂等：done 直接返回；running 由既有执行者推进）。

        状态机：queued|failed → running → done|failed；每次终态均写导入留痕。
        """
        job = self.job_store.require(job_id)
        if job.status in (STATUS_DONE, STATUS_RUNNING):
            return job
        self._transition(job, STATUS_RUNNING)
        job.error_detail = ""
        try:
            await self._run_to_completion(job)
        except Exception as exc:  # 任务级失败：人话原因 + 可续跑（水位不回退）
            self._transition(job, STATUS_FAILED)
            job.error_detail = (
                f"导入中断：{exc}。已处理 {job.processed_rows}/{job.total_rows} 行，"
                "修复后可从此处续跑（已导入行不会重复创建）。"
            )
            self._write_log(job)
            return job
        self._transition(job, STATUS_DONE)
        self._write_log(job)
        return job

    async def wait_for(
        self,
        job_id: str,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.01,
    ) -> ImportJob:
        """轮询等待任务终态（测试/管理侧辅助；超时抛 TimeoutError）。"""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            job = self.job_store.require(job_id)
            if job.status in (STATUS_DONE, STATUS_FAILED):
                return job
            await asyncio.sleep(poll_interval_s)
        raise TimeoutError(f"导入任务 {job_id} 在 {timeout_s}s 内未到达终态")

    @staticmethod
    def _transition(job: ImportJob, new_status: str) -> None:
        """状态迁移并留痕（status_history 记录全轨迹）。"""
        job.status = new_status
        job.status_history.append(new_status)
        job.updated_at = now_iso()

    # ------------------------------------------------ 流水线主体

    async def _run_to_completion(self, job: ImportJob) -> None:
        """按 processed_rows 水位续跑：逐分片 处理→定稿→推进水位。"""
        specs = column_specs(job.kb_type, self.schema_provider)
        header = read_header(job.file_bytes, job.file_format)
        header_map = resolve_header_map(header, specs)
        dedup_keys = dedup_fields_for(job.kb_type, self.schema_provider)
        for batch in self._batches_after_watermark(job):
            # 分片边界让出控制权（协作式调度：上传接口即刻返回，任务后台推进）
            await asyncio.sleep(0)
            await self._process_chunk(job, batch, specs, header, header_map, dedup_keys)

    def _batches_after_watermark(self, job: ImportJob) -> Iterator[list[tuple[int, list[str]]]]:
        """水位之后的数据行按 chunk_rows 分片（水位前行已定稿，跳过不重复处理）。

        水位在生成器启动时一次性定格：分片处理会推进 processed_rows，若循环内
        动态读取会把后续分片误当作已处理行再跳一遍（生成器惰性求值陷阱）。
        """
        watermark = job.processed_rows
        batch: list[tuple[int, list[str]]] = []
        skipped = 0
        for row_no, values in iter_data_rows(job.file_bytes, job.file_format):
            if skipped < watermark:
                skipped += 1
                continue
            batch.append((row_no, values))
            if len(batch) >= self.config.chunk_rows:
                yield batch
                batch = []
        if batch:
            yield batch

    async def _process_chunk(
        self,
        job: ImportJob,
        batch: Sequence[tuple[int, list[str]]],
        specs: Sequence[ColumnSpec],
        header: Sequence[str],
        header_map: Mapping[int, str | None],
        dedup_keys: Sequence[str],
    ) -> None:
        """单分片处理：逐行三段校验 → 有效行转创建动作（重试+internal 降级）→ 行级定稿。

        行级定稿（每行推进一次水位）：创建成功即在 _apply_creations 内即时定稿
        （推水位/登记指纹/记条目 id），分片中途系统故障已创建的行不丢失——
        保证 failed 续跑严格不重复创建（见模块 docstring 偏差标注）。
        同分片内的文件查重用本地候选指纹集（pending_first）：仅定稿行才并入
        job.seen_first，降级/中断行不污染后续判重口径。
        """
        # 候选创建动作：(行号, 行数据, 指纹, 创建动作, 原始行快照)
        candidates: list[tuple[int, dict[str, str], str, NewItem, dict[str, str]]] = []
        chunk_errors: list[ImportRowError] = []
        pending_first: dict[str, int] = {}  # 本分片候选指纹 → 行号
        for row_no, values in batch:
            mapped = map_row(values, header_map)
            snapshot = raw_snapshot(header, values)
            issue = validate_row(mapped, specs, self.biz_validators)  # ①② Schema+业务
            if issue is not None:
                chunk_errors.append(
                    ImportRowError(
                        row_no=row_no, field_label=issue.field_label, error_code=issue.error_code,
                        message=issue.message, suggestion=issue.suggestion, raw_row=snapshot,
                    )
                )
                continue
            fingerprint = dedup_fingerprint(job.kb_type, mapped, dedup_keys)  # ③ 查重
            first_row = job.seen_first.get(fingerprint)
            if first_row is None:
                first_row = pending_first.get(fingerprint)
            if first_row is not None:
                chunk_errors.append(self._dup_error(
                    row_no, snapshot, f"与第 {first_row} 行内容重复",
                    "同一文件内重复，请删除其一或修改后重新上传",
                ))
                continue
            if fingerprint in self.existing_fingerprints and job.import_mode == "skip":
                chunk_errors.append(self._dup_error(
                    row_no, snapshot, "系统中已存在相同内容的条目",
                    "如需更新该条目，请选择「覆盖导入」重新上传",
                ))
                continue
            item = NewItem(
                kb_type=job.kb_type,
                title=mapped.get("title", ""),
                content=mapped.get("content", ""),
                metadata={key: value for key, value in mapped.items() if key not in ("title", "content")},
                status=job.target_status,
                fingerprint=fingerprint,
                job_id=job.job_id,
                row_no=row_no,
                overwrite=(fingerprint in self.existing_fingerprints),  # overwrite 模式的库内更新
            )
            candidates.append((row_no, mapped, fingerprint, item, snapshot))
            pending_first[fingerprint] = row_no

        created_ids, leftovers = await self._apply_creations(job, candidates)
        # internal 降级行：从未真正入库，只报告不阻断（任务照常完成）
        for row_no, _mapped, _fingerprint, _item, snapshot in leftovers:
            chunk_errors.append(
                ImportRowError(
                    row_no=row_no, field_label="整行", error_code=ERROR_INTERNAL,
                    message="系统繁忙，本行暂未导入，请稍后重新上传剩余行",
                    suggestion="可直接重试本任务，已导入的行不会重复创建",
                    raw_row=snapshot,
                )
            )
        # 定稿：错误行（校验失败 + 查重 + internal 降级）
        for error in chunk_errors:
            job.errors.append(error)
            job.error_rows += 1
            if error.error_code == ERROR_DUPLICATE:
                job.dup_rows += 1
            job.processed_rows += 1
        job.updated_at = now_iso()
        return created_ids

    async def _apply_creations(
        self,
        job: ImportJob,
        candidates: Sequence[tuple[int, dict[str, str], str, NewItem, dict[str, str]]],
    ) -> tuple[list[int], list[tuple[int, dict[str, str], str, NewItem, dict[str, str]]]]:
        """分片内执行条目创建动作：瞬时故障重试，耗尽返回未完成的候选行。

        每个成功动作**即时定稿**（推水位/登记指纹/记条目 id）——分片中途系统
        故障（非瞬时异常向上抛 → 任务 failed）时已创建的行不丢失，续跑从精确
        水位继续不重复创建。返回 (已创建条目 id 列表, 降级候选列表)。
        """
        created_ids: list[int] = []
        pending = list(candidates)
        for _attempt in range(self.config.retry_per_chunk + 1):
            retry: list[tuple[int, dict[str, str], str, NewItem, dict[str, str]]] = []
            for candidate in pending:
                item = candidate[3]
                try:
                    result = self.create_item_hook(item)
                    if inspect.isawaitable(result):
                        result = await result
                    item_id = int(result)
                except TransientImportError:
                    retry.append(candidate)  # 瞬时故障：本轮结束后统一重试
                    continue
                self._finalize_created(job, item.row_no, item.fingerprint, item_id)
                created_ids.append(item_id)
            pending = retry
            if not pending:
                break
        return created_ids, pending

    def _finalize_created(self, job: ImportJob, row_no: int, fingerprint: str, item_id: int) -> None:
        """创建成功的行即时定稿：条目 id 入账、查重指纹并账、水位推进。"""
        job.item_ids.append(item_id)
        job.seen_first.setdefault(fingerprint, row_no)
        self.existing_fingerprints.add(fingerprint)
        job.valid_rows += 1
        job.processed_rows += 1

    @staticmethod
    def _dup_error(row_no: int, snapshot: dict[str, str], message: str, suggestion: str) -> ImportRowError:
        """构造 duplicate 错误行（查重为行级判定，字段记「整行」）。"""
        return ImportRowError(
            row_no=row_no, field_label="整行", error_code=ERROR_DUPLICATE,
            message=message, suggestion=suggestion, raw_row=snapshot,
        )

    def _write_log(self, job: ImportJob) -> None:
        """任务终态写导入留痕（处理量/成功/失败/查重计数，SR-KB-021 §4）。"""
        self.log_store.append(
            ImportLog(
                job_id=job.job_id,
                kb_type=job.kb_type,
                file_name=job.file_name,
                source_format=SOURCE_FORMAT_MAP.get(job.file_format, job.file_format),
                total_rows=job.total_rows,
                valid_rows=job.valid_rows,
                error_rows=job.error_rows,
                dup_rows=job.dup_rows,
                import_mode=job.import_mode,
                target_status=job.target_status,
                status=job.status,
                item_ids=list(job.item_ids),
                created_by=job.created_by,
            )
        )

    # ------------------------------------------------ 查询与产物

    def status_view(self, job: ImportJob) -> dict[str, Any]:
        """任务进度视图（GET 状态端点的 data 载荷；报告下载引用由路由层拼装）。"""
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress_pct": job.progress_pct,
            "total_rows": job.total_rows,
            "processed_rows": job.processed_rows,
            "valid_rows": job.valid_rows,
            "error_rows": job.error_rows,
            "dup_rows": job.dup_rows,
            "error_count": len(job.errors),
            "kb_type": job.kb_type,
            "file_name": job.file_name,
            "import_mode": job.import_mode,
            "target_status": job.target_status,
            "error_detail": job.error_detail,
            "created_by": job.created_by,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    def error_report_bytes(self, job: ImportJob) -> bytes:
        """生成错误行报告 xlsx 字节流（见 build_error_report_xlsx）。"""
        return build_error_report_xlsx(job)

    def template_file(self, kb_type: str, file_format: str = "xlsx") -> tuple[bytes, str, str]:
        """生成模板下载产物：(字节流, 媒体类型, 中文文件名)。"""
        if file_format not in self.config.allowed_formats:
            raise ImportValidationError(f"模板格式仅支持 {' / '.join(self.config.allowed_formats)}")
        self._check_kb_type(kb_type)
        if file_format == "csv":
            return render_csv_bytes(kb_type, self.schema_provider), "text/csv; charset=utf-8", f"导入模板_{kb_type}.csv"
        return (
            workbook_to_bytes(build_workbook(kb_type, self.schema_provider)),
            XLSX_MEDIA_TYPE,
            f"导入模板_{kb_type}.xlsx",
        )
