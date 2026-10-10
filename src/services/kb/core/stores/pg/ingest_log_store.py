# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# ingest_log_store.py —— 清洗留痕存储 PG 实现（IngestLogStore 协议，迭代 0.3）。
"""PsycopgIngestLogStore：``ingest_pipeline_log`` 表（03-数据库设计 §3.1）。

与 ``InMemoryIngestLogStore``（core/reporting/cleaning_report.py）行为对齐：

- ``append``（协议外写入面，与内存实现同款测试/灌数入口）：追加留痕，
  id 由 IDENTITY 分配（PG 形态不支持显式 id），created_at 缺省取当前 UTC；
- ``iter_logs``：参数化查询（batch_id 精确匹配 + 时间窗 ``[from, to)`` 左闭右开），
  按写入序（id 升序）产出，与内存实现的追加序一致。

record 字段 ↔ 表列的映射取舍（IngestLogRecord 含派生属性，只持久化基础字段）：
- 基础字段直接落列：source_file / source_format / stage / action_detail(JSONB) /
  created_at；id 为 IDENTITY 回填；
- 派生属性（status/batch_id/kb_type，内存形态由 action_detail 约定键推导）：
  写入时反范式展开到同名列（缺失取列缺省 ''/''/'success'）供 idx_ipl_batch
  过滤加速，**读取时一律由 action_detail 重新推导**——列只作索引，不回读；
- ``output``（阶段产物归档键）无独立列：并入 action_detail 的 "output" 键持久化，
  读取时弹回 record.output 并从 action_detail 移除——两侧 record 逐字段等价
  （原始 action_detail 无该键时读回同样无该键，字节口径一致）。

依据：SR-KB-011/SR-KB-024（留痕五要素与 latest 取数口径）；
     docs/02-设计文档/03-数据库设计.md §3.1。
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from ...reporting.cleaning_report import IngestLogRecord
from .connection import PgConnection, ensure_utc, utc_now

#: action_detail 中承载 output 归档键的保留键名（表无独立 output 列的映射取舍）
_OUTPUT_DETAIL_KEY: str = "output"

#: 留痕行查询列（与 _row_to_record 下标对应）
_LOG_COLS: str = "id, source_file, source_format, stage, action_detail, created_at"


def _split_output(detail: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """读侧拆分：action_detail 中的保留键 output 弹出为基础字段（见模块 docstring）。"""
    merged = dict(detail)
    output = str(merged.pop(_OUTPUT_DETAIL_KEY, ""))
    return merged, output


def _row_to_record(row: tuple[Any, ...]) -> IngestLogRecord:
    """ingest_pipeline_log 行 → IngestLogRecord（派生属性交由 record property 推导）。"""
    detail, output = _split_output(dict(row[4] or {}))
    return IngestLogRecord(
        source_file=row[1],
        source_format=row[2],
        stage=row[3],
        action_detail=detail,
        id=int(row[0]),
        created_at=ensure_utc(row[5]),
        output=output,
    )


class PsycopgIngestLogStore:
    """清洗留痕 PG 存储：报告侧只读消费（iter_logs）+ 灌数侧 append（协议外）。"""

    def __init__(self, conn: PgConnection) -> None:
        self._conn = conn

    # ── 写入面（协议外，与内存实现 append/extend 对齐）──────────────────────

    def append(self, record: IngestLogRecord) -> int:
        """追加一条留痕并返回分配的 id（IDENTITY；created_at 缺省取当前 UTC）。

        派生列展开：batch_id/kb_type/status 列取 action_detail 约定键（缺失按
        列缺省 ''/''/'success'），仅服务索引过滤；action_detail JSONB 为权威。
        """
        detail = dict(record.action_detail)
        if record.output:
            detail[_OUTPUT_DETAIL_KEY] = record.output
        batch_id = str(detail.get("batch_id", ""))
        kb_type = str(detail.get("kb_type", ""))
        status = str(detail.get("status", "success"))  # 对齐列缺省（迭代 0.1 约定）
        with self._conn.transaction() as cur:
            cur.execute(
                "INSERT INTO ingest_pipeline_log (batch_id, source_file, source_format, "
                "kb_type, stage, status, action_detail, error, created_at) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,'',%s) RETURNING id",
                (
                    batch_id,
                    record.source_file,
                    record.source_format,
                    kb_type,
                    record.stage,
                    status,
                    Jsonb(detail),
                    ensure_utc(record.created_at) or utc_now(),
                ),
            )
            return int(cur.fetchone()[0])

    def extend(self, records: Iterable[IngestLogRecord]) -> None:
        """批量追加（测试便捷方法，与内存实现同款）。"""
        for record in records:
            self.append(record)

    # ── 读取面（协议：报告聚合的唯一取数入口）──────────────────────────────

    def iter_logs(
        self,
        *,
        batch_id: str | None = None,
        window_from: datetime | None = None,
        window_to: datetime | None = None,
    ) -> Iterable[IngestLogRecord]:
        """按批次号或时间窗 [window_from, window_to) 迭代留痕（写入序，id 升序）。"""
        predicates: list[str] = []
        params: list[Any] = []
        if batch_id is not None:
            predicates.append("batch_id = %s")
            params.append(batch_id)
        if window_from is not None:
            predicates.append("created_at >= %s")
            params.append(ensure_utc(window_from))
        if window_to is not None:
            predicates.append("created_at < %s")
            params.append(ensure_utc(window_to))
        sql = f"SELECT {_LOG_COLS} FROM ingest_pipeline_log"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        sql += " ORDER BY id ASC"
        with self._conn.transaction() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        for row in rows:
            yield _row_to_record(row)
