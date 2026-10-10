# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# store.py —— 版本快照存储端口与内存实现（FR-KB-022 版本控制，迭代 0.3）。
"""版本存储端口（Protocol）与内存实现，数据形态对齐 03-数据库设计 §3.1 两张表。

    knowledge_item    → ItemRecord（业务字段 payload + current_version 权威指针）
    knowledge_version → VersionSnapshot 链（item_id → 按版本号升序的快照列表）

约束口径（SR-KB-022 §2/§4 线性链决策）：
- 版本号线性单调递增：新版本号恒为「链长 + 1」，禁止分叉与跳号（出现空洞即
  ValueError，fail-closed）；本迭代内存形态以连续下标表达 UNIQUE(item_id, version)；
- UNIQUE(item_id, version) 幂等：同 (item_id, version) 重复写入为 no-op，吸收上游
  事件重放（对应 SQL 的 ``ON CONFLICT (item_id, version) DO NOTHING``）；
- 快照仅追加、永不 UPDATE/DELETE：存储层不提供任何改写历史快照的入口——回滚由
  service 层以「引用旧快照的新版本」形式追加（SR-KB-022 §6.3）；
- 深拷贝隔离：写入与读取均对 snapshot_json 做 deepcopy，调用方持有 dict 的后续
  演化不污染历史快照（「回滚后 v1/v2 快照逐字节不变」的前提）；
- replace_item 为 CAS 语义（expected_version 不匹配返回 False）：模拟 SQL
  ``UPDATE knowledge_item ... WHERE id=$1 AND current_version=$2`` 的双重乐观锁，
  内存实现以全局锁保证原子性，生产实现由 PG 行锁承接，接口语义不变。

实现方案：docs/07-实现方案/KB/SR-KB-022-版本控制.md §4、§6.1
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


def utc_now() -> datetime:
    """UTC 时间戳（store 缺省时钟；生产 PG 形态为列缺省 now()）。"""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class VersionSnapshot:
    """一个历史版本的全量快照（knowledge_version 单行的内存形态）。

    snapshot_json 为条目业务五字段全量自包含快照
    {kb_type, title, content, metadata, status}，对比/回滚不依赖前序版本；
    rollback_from 为细化新增列（SR-KB-022 §4）：None=正常编辑产生，
    非 None=该版本由回滚生成、指向被回滚到的版本号（界面「回滚自 Vn」徽标）。
    """

    version: int
    snapshot_json: dict[str, Any]
    change_note: str = ""
    created_at: datetime = field(default_factory=utc_now)
    created_by: str = ""
    rollback_from: int | None = None


@dataclass(frozen=True)
class ItemRecord:
    """知识条目当前态（knowledge_item 业务字段子集 + 版本链权威指针）。

    current_version 是线性版本链的权威指针（SR-KB-022 §2：消除并发分叉源），
    与快照链尾版本恒一致（由 service 写路径的 CAS + 追加顺序保证）。
    """

    item_id: str
    payload: dict[str, Any]
    current_version: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


@runtime_checkable
class VersionStore(Protocol):
    """版本存储端口：PG 实现与内存实现共同满足的协议（鸭子类型注入 service）。"""

    def create_item(self, payload: Mapping[str, Any], created_by: str) -> ItemRecord:
        """登记新条目（current_version=0 占位，首版快照由调用方追加）。"""
        ...

    def get_item(self, item_id: str) -> ItemRecord | None:
        """读条目当前态；不存在返回 None（payload 深拷贝，读取不污染）。"""
        ...

    def replace_item(
        self,
        item_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int,
        new_version: int,
        updated_by: str,
    ) -> bool:
        """CAS 改写条目当前态与版本指针；版本不匹配/条目不存在返回 False。"""
        ...

    def append_snapshot(self, item_id: str, snapshot: VersionSnapshot) -> None:
        """追加历史快照（UNIQUE(item,version) 幂等；跳号/未知条目显式失败）。"""
        ...

    def get_snapshot(self, item_id: str, version: int) -> VersionSnapshot | None:
        """读单个历史快照；不存在返回 None（snapshot_json 深拷贝）。"""
        ...

    def list_snapshots(self, item_id: str) -> list[VersionSnapshot]:
        """按版本号升序列出全部历史快照；无快照返回空列表。"""
        ...


class InMemoryVersionStore:
    """内存实现：dict + 全局锁，模拟行级 CAS、唯一约束与外键约束。

    迭代 0.3 的测试/联调桩；生产换 PG 实现（knowledge_item +
    knowledge_version 两表事务），端口协议不变，service 零改动。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 条目内部行（可变）：item_id → 字段字典（ItemRecord 的可变底座）
        self._items: dict[str, dict[str, Any]] = {}
        # 版本链：item_id → 按版本号升序的快照列表（下标 = version - 1，连续无空洞）
        self._versions: dict[str, list[VersionSnapshot]] = {}

    # ── 条目当前态（knowledge_item）──────────────────────────────────────────

    def create_item(self, payload: Mapping[str, Any], created_by: str) -> ItemRecord:
        """登记新条目：current_version=0 占位，首版由调用方经 replace+append 写入。

        与 PG 形态对齐：knowledge_item 先落行、knowledge_version 随保存事务追加；
        item_id 取 uuid4 hex（对应 uid UUID 列的内存形态）。
        """
        with self._lock:
            item_id = uuid.uuid4().hex
            now = utc_now()
            self._items[item_id] = {
                "payload": deepcopy(dict(payload)),
                "current_version": 0,
                "created_by": created_by,
                "created_at": now,
                "updated_by": created_by,
                "updated_at": now,
            }
            return self._record(item_id)

    def get_item(self, item_id: str) -> ItemRecord | None:
        """读条目当前态（payload 深拷贝，调用方修改不影响存储）。"""
        with self._lock:
            return self._record(item_id) if item_id in self._items else None

    def replace_item(
        self,
        item_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int,
        new_version: int,
        updated_by: str,
    ) -> bool:
        """CAS 改写条目：current_version 必须等于 expected_version 方生效。

        同时校验线性链不变量 new_version == expected_version + 1（违反即
        ValueError——数据结构错误不应被静默吞没，区别于并发布尔的 False）。
        """
        with self._lock:
            row = self._items.get(item_id)
            if row is None or row["current_version"] != expected_version:
                return False
            if new_version != expected_version + 1:
                raise ValueError(
                    f"版本号必须线性递增: V{expected_version} → V{new_version} 非法"
                )
            row["payload"] = deepcopy(dict(payload))
            row["current_version"] = new_version
            row["updated_by"] = updated_by
            row["updated_at"] = utc_now()
            return True

    # ── 历史快照链（knowledge_version）───────────────────────────────────────

    def append_snapshot(self, item_id: str, snapshot: VersionSnapshot) -> None:
        """追加历史快照，三重约束（对应 UNIQUE / 线性链 / FK）：

        - version < 1 → ValueError（非法版本号）；
        - version 已在链内（≤ 链长，链连续）→ no-op（UNIQUE 幂等，吸收事件重放）；
        - version > 链长 + 1 → ValueError（跳号，版本链出现空洞）；
        - item_id 未登记 → KeyError（模拟 item_id 外键约束）。
        """
        with self._lock:
            if item_id not in self._items:
                raise KeyError(f"条目不存在（item_id 外键约束）: {item_id}")
            if snapshot.version < 1:
                raise ValueError(f"版本号必须 ≥ 1: {snapshot.version}")
            chain = self._versions.setdefault(item_id, [])
            if snapshot.version <= len(chain):
                return  # UNIQUE(item_id, version) 冲突即忽略（DO NOTHING）
            if snapshot.version != len(chain) + 1:
                raise ValueError(
                    f"版本号必须线性递增: 链长 {len(chain)} 拒收 V{snapshot.version}"
                )
            chain.append(
                replace(snapshot, snapshot_json=deepcopy(snapshot.snapshot_json))
            )

    def get_snapshot(self, item_id: str, version: int) -> VersionSnapshot | None:
        """读单版快照（snapshot_json 深拷贝，读取不污染历史）。"""
        with self._lock:
            chain = self._versions.get(item_id, [])
            if 1 <= version <= len(chain):
                snap = chain[version - 1]
                return replace(snap, snapshot_json=deepcopy(snap.snapshot_json))
            return None

    def list_snapshots(self, item_id: str) -> list[VersionSnapshot]:
        """按版本号升序列出全部快照（各自深拷贝）。"""
        with self._lock:
            return [
                replace(snap, snapshot_json=deepcopy(snap.snapshot_json))
                for snap in self._versions.get(item_id, [])
            ]

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    def _record(self, item_id: str) -> ItemRecord:
        """由内部可变行构造冻结 ItemRecord（payload 深拷贝）。"""
        row = self._items[item_id]
        return ItemRecord(
            item_id=item_id,
            payload=deepcopy(row["payload"]),
            current_version=row["current_version"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )
