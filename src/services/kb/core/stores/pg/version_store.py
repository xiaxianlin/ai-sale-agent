# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# version_store.py —— 版本快照存储 PG 实现（VersionStore 协议，迭代 0.3）。
"""PsycopgVersionStore：``knowledge_item`` + ``knowledge_version`` 两表（03-数据库设计 §3.1）。

与 ``InMemoryVersionStore``（core/versioning/store.py）逐方法行为对齐：

- ``create_item``   ：INSERT knowledge_item（current_version=0 占位），item_id 取
                      uid 的规范 UUID 字符串（也接受数字 id 字符串反查，便于运维）；
- ``replace_item``  ：先按 CAS 口径判存在/版本（不符 → False），再校验线性链
                      ``new_version == expected_version + 1``（违反 → ValueError），
                      最后 ``UPDATE ... WHERE id=? AND current_version=$expected``
                      行级 CAS 收口（并发窗口内他者抢先 → 0 行 → False）；
- ``append_snapshot``：未知条目 → KeyError（模拟外键）；version<1 → ValueError；
                      跳号 → ValueError；``ON CONFLICT (item_id, version) DO NOTHING``
                      承接 UNIQUE 幂等（事件重放 no-op）；
- ``get_snapshot``/``list_snapshots``：快照只读（永不 UPDATE/DELETE），
                      list 按 version 升序。

payload 映射口径（ItemRecord.payload ↔ knowledge_item 业务列）：
- payload 为条目业务五字段 ``{kb_type, title, content, metadata, status}`` 的
  全量自包含字典（与 VersionSnapshot.snapshot_json 同构）；五键之外的关键字
  fail-loud 拒绝（ValueError）——knowledge_item 无承载额外键的列，静默丢弃
  违背「快照自包含可回滚」前提；kb_type 缺失同理拒绝（列 NOT NULL）；
- ``snapshot_json`` 整体落 knowledge_version.snapshot JSONB，任意键无损（深拷贝
  隔离由 JSON 序列化天然保证）。

依据：SR-KB-022 §2/§4（线性链/UNIQUE/CAS 决策）；docs/02-设计文档/03-数据库设计.md §3.1。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg.types.json import Jsonb

from ...versioning.store import ItemRecord, VersionSnapshot
from .connection import PgConnection, ensure_utc, utc_now

#: payload 允许的业务五字段（knowledge_item 业务列集；超出即 ValueError）
_PAYLOAD_FIELDS: frozenset[str] = frozenset({"kb_type", "title", "content", "metadata", "status"})

#: 条目当前态查询列（与 _row_to_item_record 下标对应）
_ITEM_COLS: str = (
    "uid, kb_type, title, content, metadata, status, current_version, "
    "created_by, created_at, updated_by, updated_at"
)


def _payload_to_columns(payload: Mapping[str, Any]) -> tuple[str, str, str, dict[str, Any], str]:
    """业务五字段 payload → (kb_type, title, content, metadata, status) 列值。

    五键之外的关键字 / kb_type 缺失 → ValueError（fail-loud，见模块 docstring）。
    """
    extra = sorted(set(payload) - _PAYLOAD_FIELDS)
    if extra:
        raise ValueError(
            f"payload 含业务五字段之外的键（PG 形态不持久化，fail-loud）: {extra}"
        )
    kb_type = payload.get("kb_type")
    if not kb_type:
        raise ValueError("payload.kb_type 缺失（knowledge_item.kb_type NOT NULL）")
    metadata = payload.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("payload.metadata 必须为对象（JSONB 列约束）")
    return (
        str(kb_type),
        str(payload.get("title", "")),
        str(payload.get("content", "")),
        dict(metadata),
        str(payload.get("status", "draft")),
    )


def _row_to_item_record(row: tuple[Any, ...]) -> ItemRecord:
    """knowledge_item 行 → 冻结 ItemRecord（item_id = uid 规范字符串）。"""
    return ItemRecord(
        item_id=str(row[0]),
        payload={
            "kb_type": row[1],
            "title": row[2],
            "content": row[3],
            "metadata": dict(row[4] or {}),
            "status": row[5],
        },
        current_version=row[6],
        created_by=row[7],
        created_at=ensure_utc(row[8]),
        updated_by=row[9],
        updated_at=ensure_utc(row[10]),
    )


def _row_to_snapshot(row: tuple[Any, ...]) -> VersionSnapshot:
    """knowledge_version 行 → 冻结 VersionSnapshot（snapshot_json 即 JSONB 反序列化产物）。"""
    return VersionSnapshot(
        version=row[0],
        snapshot_json=dict(row[1] or {}),
        change_note=row[2],
        rollback_from=row[3],
        created_at=ensure_utc(row[4]),
        created_by=row[5],
    )


class PsycopgVersionStore:
    """版本存储 PG 实现：复用调用方注入的 PgConnection（一方法一事务）。"""

    def __init__(self, conn: PgConnection) -> None:
        self._conn = conn

    # ── 内部：item_id 解析（uid 字符串优先，数字 id 字符串兜底）────────────

    def _resolve_item_id(self, cur: Any, item_id: str) -> int | None:
        """item_id（uid 字符串或数字 id 字符串）→ knowledge_item.id；未知返回 None。"""
        cur.execute(
            "SELECT id FROM knowledge_item WHERE uid::text = %s", (item_id,)
        )
        row = cur.fetchone()
        if row is None and item_id.isdigit():
            cur.execute("SELECT id FROM knowledge_item WHERE id = %s", (int(item_id),))
            row = cur.fetchone()
        return int(row[0]) if row is not None else None

    # ── 条目当前态（knowledge_item）────────────────────────────────────────

    def create_item(self, payload: Mapping[str, Any], created_by: str) -> ItemRecord:
        """登记新条目：current_version=0 占位，首版快照由调用方经 replace+append 写入。"""
        kb_type, title, content, metadata, status = _payload_to_columns(payload)
        with self._conn.transaction() as cur:
            cur.execute(
                "INSERT INTO knowledge_item (kb_type, title, content, metadata, status, "
                "current_version, created_by, updated_by) VALUES (%s,%s,%s,%s,%s,0,%s,%s) "
                f"RETURNING {_ITEM_COLS}",
                (kb_type, title, content, Jsonb(metadata), status, created_by, created_by),
            )
            row = cur.fetchone()
        assert row is not None  # RETURNING 必有行
        return _row_to_item_record(row)

    def get_item(self, item_id: str) -> ItemRecord | None:
        """读条目当前态；不存在返回 None（payload 由业务列重建，读取不污染）。"""
        with self._conn.transaction() as cur:
            rowid = self._resolve_item_id(cur, item_id)
            if rowid is None:
                return None
            cur.execute(
                f"SELECT {_ITEM_COLS} FROM knowledge_item WHERE id = %s", (rowid,)
            )
            row = cur.fetchone()
        return _row_to_item_record(row) if row is not None else None

    def replace_item(
        self,
        item_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int,
        new_version: int,
        updated_by: str,
    ) -> bool:
        """CAS 改写条目当前态与版本指针；条目不存在/版本不匹配返回 False。

        线性链不变量 ``new_version == expected_version + 1`` 违反时 ValueError
        （数据结构错误不应被静默吞没，与内存实现同款口径）。
        """
        kb_type, title, content, metadata, status = _payload_to_columns(payload)
        with self._conn.transaction() as cur:
            rowid = self._resolve_item_id(cur, item_id)
            if rowid is None:
                return False
            cur.execute(
                "SELECT current_version FROM knowledge_item WHERE id = %s", (rowid,)
            )
            row = cur.fetchone()
            if row is None or row[0] != expected_version:
                return False
            if new_version != expected_version + 1:
                raise ValueError(f"版本号必须线性递增: V{expected_version} → V{new_version} 非法")
            cur.execute(
                "UPDATE knowledge_item SET kb_type=%s, title=%s, content=%s, metadata=%s, "
                "status=%s, current_version=%s, updated_by=%s, updated_at=%s "
                "WHERE id = %s AND current_version = %s",
                (
                    kb_type,
                    title,
                    content,
                    Jsonb(metadata),
                    status,
                    new_version,
                    updated_by,
                    utc_now(),
                    rowid,
                    expected_version,
                ),
            )
            return cur.rowcount == 1

    # ── 历史快照链（knowledge_version）─────────────────────────────────────

    def append_snapshot(self, item_id: str, snapshot: VersionSnapshot) -> None:
        """追加历史快照：未知条目 KeyError / 跳号 ValueError / UNIQUE 冲突 no-op。"""
        with self._conn.transaction() as cur:
            rowid = self._resolve_item_id(cur, item_id)
            if rowid is None:
                raise KeyError(f"条目不存在（item_id 外键约束）: {item_id}")
            if snapshot.version < 1:
                raise ValueError(f"版本号必须 ≥ 1: {snapshot.version}")
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) FROM knowledge_version WHERE item_id = %s",
                (rowid,),
            )
            chain_len = int(cur.fetchone()[0])
            if snapshot.version <= chain_len:
                return  # UNIQUE(item_id, version) 冲突即忽略（DO NOTHING）
            if snapshot.version != chain_len + 1:
                raise ValueError(
                    f"版本号必须线性递增: 链长 {chain_len} 拒收 V{snapshot.version}"
                )
            cur.execute(
                "INSERT INTO knowledge_version (item_id, version, snapshot, change_note, "
                "rollback_from, created_at, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (item_id, version) DO NOTHING",
                (
                    rowid,
                    snapshot.version,
                    Jsonb(dict(snapshot.snapshot_json)),
                    snapshot.change_note,
                    snapshot.rollback_from,
                    ensure_utc(snapshot.created_at) or utc_now(),
                    snapshot.created_by,
                ),
            )

    def get_snapshot(self, item_id: str, version: int) -> VersionSnapshot | None:
        """读单个历史快照；不存在返回 None。"""
        with self._conn.transaction() as cur:
            rowid = self._resolve_item_id(cur, item_id)
            if rowid is None:
                return None
            cur.execute(
                "SELECT version, snapshot, change_note, rollback_from, created_at, created_by "
                "FROM knowledge_version WHERE item_id = %s AND version = %s",
                (rowid, version),
            )
            row = cur.fetchone()
        return _row_to_snapshot(row) if row is not None else None

    def list_snapshots(self, item_id: str) -> list[VersionSnapshot]:
        """按版本号升序列出全部历史快照；无快照返回空列表。"""
        with self._conn.transaction() as cur:
            rowid = self._resolve_item_id(cur, item_id)
            if rowid is None:
                return []
            cur.execute(
                "SELECT version, snapshot, change_note, rollback_from, created_at, created_by "
                "FROM knowledge_version WHERE item_id = %s ORDER BY version ASC",
                (rowid,),
            )
            rows = cur.fetchall()
        return [_row_to_snapshot(row) for row in rows]
