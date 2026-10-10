# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# item_store.py —— 知识条目存储 PG 实现（KnowledgeItemStore 协议，迭代 0.3）。
"""PsycopgKnowledgeItemStore：``knowledge_item`` 表（03-数据库设计 §3.1）的真实存储。

与 ``InMemoryKnowledgeItemStore``（api/admin/items.py）逐方法行为对齐：

- ``create``  ：uid 缺省由存储生成（PG 形态为 UUID，读回以规范小写字符串返回），
                uid 冲突抛 ValueError（对应表级 UNIQUE，中文错误）；
- ``save``    ：``UPDATE ... WHERE uid=? AND deleted_at IS NULL [AND current_version=?]``
                单语句原子乐观锁，影响行数 0 → None（版本不符/不存在/已软删）；
                expected_version 缺省跳过版本比对（与内存语义一致）；
- ``soft_delete``：置 deleted_at + 版本 +1 + sync_status='removed'，行不存在/已软删 → None；
- ``query``   ：过滤 + 排序（updated_at 降序、uid 升序决胜）+ 分页；关键词 q 走
                ILIKE 包含匹配（``%``/``_``/``\\`` 转义为字面量，等价内存 casefold 包含）；
- ``iter_all``：按写入序（id 升序）遍历，缺省不含软删行；
- ``set_sync_status``：回写索引同步状态，软删/不存在行静默跳过。

映射口径（record 字段 ↔ 表列）：
- ``metadata`` dict ↔ JSONB 列（psycopg Jsonb 适配，读回 dict）；
- 时间字段 ↔ TIMESTAMPTZ（naive 写入按 UTC 归一，读回恒为 UTC aware）；
- ``uid`` ↔ UUID 列（非法 uid 字符串按「条目不存在」处理 → None，与内存字典未命中一致）；
- 表内 tenant_id/account_id/created_by 为部署维度列，本协议不涉及，走列缺省。

依据：docs/02-设计文档/03-数据库设计.md §3.1；SR-KB-020 §4（软删/乐观锁/分页口径）。
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from ....api.admin.items import (
    SYNC_REMOVED,
    KnowledgeItemRecord,
)
from .connection import PgConnection, ensure_utc, utc_now

#: 查询/RETURNING 统一列清单（与 _row_to_record 下标一一对应）
_COLS: str = (
    "uid, kb_type, title, content, metadata, status, current_version, "
    "sync_status, created_at, updated_at, updated_by, deleted_at"
)


def _parse_uid(uid: str) -> uuid.UUID | None:
    """uid 字符串 → UUID；非法形态返回 None（等价内存实现的字典未命中）。"""
    try:
        return uuid.UUID(uid)
    except (ValueError, AttributeError, TypeError):
        return None


def _escape_like(needle: str) -> str:
    """LIKE/ILIKE 模式字面量化：转义 ``\\``/``%``/``_`` 后包裹两侧 ``%``。

    PG 默认以反斜杠为 LIKE 转义符；字面量化后 ILIKE 包含匹配与内存实现的
    ``q.casefold() in title.casefold()`` 口径一致（ASCII/中文；异体大小写折叠
    差异由调用方避免，管理端检索词不涉及）。
    """
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _row_to_record(row: tuple[Any, ...]) -> KnowledgeItemRecord:
    """knowledge_item 行 → 冻结 record（uid 规范字符串、时间恒 UTC aware）。"""
    return KnowledgeItemRecord(
        kb_type=row[1],
        title=row[2],
        content=row[3],
        metadata=dict(row[4] or {}),
        status=row[5],
        current_version=row[6],
        uid=str(row[0]),
        sync_status=row[7],
        created_at=ensure_utc(row[8]),
        updated_at=ensure_utc(row[9]),
        updated_by=row[10],
        deleted_at=ensure_utc(row[11]),
    )


class PsycopgKnowledgeItemStore:
    """条目存储 PG 实现：复用调用方注入的 PgConnection（一方法一事务）。"""

    def __init__(self, conn: PgConnection) -> None:
        self._conn = conn

    # ── 建档 ────────────────────────────────────────────────────────────────

    def create(self, record: KnowledgeItemRecord) -> KnowledgeItemRecord:
        """建档：uid/时间戳缺省由存储补齐；uid 冲突显式失败（ValueError 中文错误）。"""
        now = utc_now()
        uid_value = _parse_uid(record.uid) if record.uid else uuid.uuid4()
        if record.uid and uid_value is None:
            raise ValueError(f"条目 UID 非法（PG 形态要求 UUID）: {record.uid}")
        params: tuple[Any, ...] = (
            uid_value,
            record.kb_type,
            record.title,
            record.content,
            Jsonb(dict(record.metadata)),
            record.status,
            max(record.current_version, 1),
            record.sync_status,
            ensure_utc(record.created_at) or now,
            ensure_utc(record.updated_at) or now,
            record.updated_by,
            ensure_utc(record.deleted_at),
        )
        try:
            with self._conn.transaction() as cur:
                cur.execute(
                    "INSERT INTO knowledge_item (uid, kb_type, title, content, metadata, "
                    "status, current_version, sync_status, created_at, updated_at, "
                    "updated_by, deleted_at) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
                    params,
                )
                row = cur.fetchone()
        except UniqueViolation as exc:  # uid 撞表级 UNIQUE
            raise ValueError(f"条目 UID 冲突: {uid_value}") from exc
        assert row is not None  # RETURNING 必有行
        return _row_to_record(row)

    # ── 读取 ────────────────────────────────────────────────────────────────

    def get(self, uid: str) -> KnowledgeItemRecord | None:
        """按 uid 取未软删条目；不存在/已软删/uid 非法返回 None。"""
        uid_value = _parse_uid(uid)
        if uid_value is None:
            return None
        with self._conn.transaction() as cur:
            cur.execute(
                f"SELECT {_COLS} FROM knowledge_item WHERE uid = %s AND deleted_at IS NULL",
                (uid_value,),
            )
            row = cur.fetchone()
        return _row_to_record(row) if row is not None else None

    # ── 乐观锁整行覆盖 ──────────────────────────────────────────────────────

    def save(
        self, record: KnowledgeItemRecord, expected_version: int | None = None
    ) -> KnowledgeItemRecord | None:
        """乐观锁保存：UPDATE 的 WHERE 同时限定 uid/未软删/（可选）期望版本。

        单条 UPDATE 由 PG 行锁保证原子：并发双写同一 expected_version 时
        恰一者影响行数为 1，另一者 0 行 → None。created_at 不随保存变化
        （内存语义中候选记录继承自 get()，两侧一致）。
        """
        uid_value = _parse_uid(record.uid)
        if uid_value is None:
            return None
        predicates = ["uid = %s", "deleted_at IS NULL"]
        params: list[Any] = [
            record.kb_type,
            record.title,
            record.content,
            Jsonb(dict(record.metadata)),
            record.status,
            record.current_version,
            record.sync_status,
            ensure_utc(record.updated_at) or utc_now(),
            record.updated_by,
            ensure_utc(record.deleted_at),
            uid_value,
        ]
        if expected_version is not None:
            predicates.append("current_version = %s")
            params.append(expected_version)
        with self._conn.transaction() as cur:
            cur.execute(
                "UPDATE knowledge_item SET kb_type=%s, title=%s, content=%s, metadata=%s, "
                "status=%s, current_version=%s, sync_status=%s, updated_at=%s, "
                "updated_by=%s, deleted_at=%s WHERE " + " AND ".join(predicates)
                + f" RETURNING {_COLS}",
                tuple(params),
            )
            row = cur.fetchone()
        return _row_to_record(row) if row is not None else None

    # ── 软删 ────────────────────────────────────────────────────────────────

    def soft_delete(
        self, uid: str, *, now: datetime | None = None, actor: str = ""
    ) -> KnowledgeItemRecord | None:
        """软删：置 deleted_at、版本 +1、sync_status='removed'；已删/不存在 → None。"""
        uid_value = _parse_uid(uid)
        if uid_value is None:
            return None
        moment = ensure_utc(now) or utc_now()
        with self._conn.transaction() as cur:
            cur.execute(
                "UPDATE knowledge_item SET deleted_at=%s, updated_at=%s, "
                "updated_by=COALESCE(NULLIF(%s, ''), updated_by), "
                f"current_version=current_version+1, sync_status='{SYNC_REMOVED}' "
                "WHERE uid = %s AND deleted_at IS NULL "
                f"RETURNING {_COLS}",
                (moment, moment, actor, uid_value),
            )
            row = cur.fetchone()
        return _row_to_record(row) if row is not None else None

    # ── 过滤分页 ────────────────────────────────────────────────────────────

    def query(
        self,
        *,
        kb_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeItemRecord], int]:
        """过滤 → 排序（updated_at 降序、uid 升序决胜）→ 分页；返回 (当前页行, 总数)。

        UUID 类型的升序与 uid 规范字符串的字典升序一致（小写十六进制逐字节可比），
        与内存实现「uid 升序 + updated_at 降序两遍稳定排序」的最终序完全对齐。
        """
        predicates = ["deleted_at IS NULL"]
        params: list[Any] = []
        if kb_type is not None:
            predicates.append("kb_type = %s")
            params.append(kb_type)
        if status is not None:
            predicates.append("status = %s")
            params.append(status)
        if q:
            pattern = _escape_like(q)
            predicates.append("(title ILIKE %s OR content ILIKE %s)")
            params.extend((pattern, pattern))
        offset = max((page - 1) * page_size, 0)
        with self._conn.transaction() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM knowledge_item WHERE " + " AND ".join(predicates),
                tuple(params),
            )
            total = int(cur.fetchone()[0])
            cur.execute(
                f"SELECT {_COLS} FROM knowledge_item "
                "WHERE " + " AND ".join(predicates)
                + " ORDER BY updated_at DESC, uid ASC LIMIT %s OFFSET %s",
                tuple(params) + (page_size, offset),
            )
            rows = cur.fetchall()
        return [_row_to_record(row) for row in rows], total

    # ── 遍历与索引状态回写 ──────────────────────────────────────────────────

    def iter_all(self, *, include_deleted: bool = False) -> Iterator[KnowledgeItemRecord]:
        """遍历条目（写入序 = id 升序；缺省不含软删行）。"""
        predicate = "" if include_deleted else " WHERE deleted_at IS NULL"
        with self._conn.transaction() as cur:
            cur.execute(f"SELECT {_COLS} FROM knowledge_item{predicate} ORDER BY id ASC")
            rows = cur.fetchall()
        for row in rows:
            yield _row_to_record(row)

    def set_sync_status(self, uid: str, sync_status: str) -> None:
        """回写索引同步状态（行不存在/已软删静默跳过，与内存语义一致）。"""
        uid_value = _parse_uid(uid)
        if uid_value is None:
            return
        with self._conn.transaction() as cur:
            cur.execute(
                "UPDATE knowledge_item SET sync_status = %s WHERE uid = %s AND deleted_at IS NULL",
                (sync_status, uid_value),
            )
