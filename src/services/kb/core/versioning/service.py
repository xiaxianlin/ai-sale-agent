# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# service.py —— 版本控制服务（FR-KB-022，SR-KB-022 §6.1/§6.3 的同步内核形态）。
"""VersioningService：版本链唯一写入口 + 读路径（历史/对比）。

    save(item, payload, expected_version)
      → 自动快照：item 当前状态 → 线性新版本（CAS 改指针 + 追加快照 + 审计）
    diff(item, v1, v2)
      → 任意两版本的字段分类对比（diff.diff_snapshots）
    rollback(item, to_version, expected_version, reason)
      → 生成**引用旧快照的新版本**（rollback_from 标记）而非物理覆盖：
        历史快照一经写入永不 UPDATE/DELETE，版本链完整、审计可回放；
        成功后触发 resync_hook（联动 FR-KB-017 增量重索引）与 audit_hook 留痕。

乐观并发（SR-KB-022 §6.4）：写路径携带 expected_version，与条目 current_version
不匹配即 VersionConflict（40901 语义，「条目已被他人修改，请刷新后重试」）；
expected_version 缺省 None 表示跳过前置校验（worker 事件重放路径，存储层 CAS
仍兜底）。并发分叉在数据结构上已消除（线性单调版本号，SR-KB-022 §2）。

依赖注入（Protocol 鸭子类型，均为桩友好）：
    store       VersionStore（版本链存储端口；迭代 0.3 内存实现）
    resync_hook (item_id, action, version) -> None——回滚触发 FR-KB-017
                重索引入队的注入桩；仅目标内容为 published 时触发（SR §6.3），
                幂等键由 FR-KB-017 侧承接
    audit_hook  (event dict) -> None——NFR-SEC-03 审计留痕桩：
                kb_change（每次快照写入）/ kb_rollback（每次回滚），
                事件携带快照 sha256 摘要（哈希链由平台审计组件承接）

迭代 0.3 偏差（生产接入项，见 SR-KB-022 §4/§6.3）：Redis request_id 回滚幂等、
快照体积上限（snapshot_max_kb）、diff 超时降级随数据访问层/配置中心迭代接入；
本内核先行承接版本链/对比/回滚的事务语义。

实现方案：docs/07-实现方案/KB/SR-KB-022-版本控制.md §2、§6.1、§6.3、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from .diff import DiffReport, diff_snapshots
from .store import ItemRecord, VersionSnapshot, VersionStore, utc_now

#: 条目状态合法值（03-数据库设计 §3.1 knowledge_item.status CHECK）
ALLOWED_STATUS: frozenset[str] = frozenset({"draft", "published", "archived"})

#: 回滚后触发重索引的状态集合（SR-KB-022 §6.3：published 经 FR-KB-017 生效；
#: draft 回滚不进检索索引，无需重索引）
RESYNC_STATUSES: frozenset[str] = frozenset({"published"})

#: 审计事件动作名（NFR-SEC-03 联动）
AUDIT_SNAPSHOT: str = "kb_change"    # 每次快照写入（保存/回滚生成的新版本）
AUDIT_ROLLBACK: str = "kb_rollback"  # 每次回滚（与 kb_change 成对出现）

#: 重索引钩子类型：(item_id, 动作, 新版本号) -> None（FR-KB-017 入队桩）
ResyncHook = Callable[[str, str, int], None]
#: 审计钩子类型：审计事件 dict -> None（NFR-SEC-03 留痕桩）
AuditHook = Callable[[dict[str, Any]], None]

#: 回滚传递给 FR-KB-017 的动作名（增量 upsert 通道）
_RESYNC_ACTION_UPSERT: str = "upsert"


# ── 错误体系（业务码沿用 SR-KB-022 §5 / SR-KB-020 已引入口径）──────────────


class VersioningError(Exception):
    """版本域业务异常基类：携带业务码 / HTTP 状态 / 文案（API 层展开统一信封）。"""

    code: int = 50000
    http_status: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: str = message


class InvalidVersion(VersioningError):
    """版本号非法（<1 或类型错误）→ 40001 / HTTP 400。"""

    code = 40001
    http_status = 400


class InvalidItemPayload(VersioningError):
    """条目载荷校验失败（status 越枚举 / title 缺失）→ 40001 / HTTP 400。"""

    code = 40001
    http_status = 400


class ItemNotFound(VersioningError):
    """条目不存在（含软删语义，由存储层承接）→ 40401 / HTTP 404。"""

    code = 40401
    http_status = 404


class SnapshotNotFound(VersioningError):
    """目标版本不存在 → 40401 / HTTP 404。"""

    code = 40401
    http_status = 404


class VersionConflict(VersioningError):
    """乐观并发冲突（expected_version 落后）→ 40901 / HTTP 409。"""

    code = 40901
    http_status = 409


def digest_of(snapshot_json: Mapping[str, Any]) -> str:
    """快照 sha256 摘要（canonical JSON 序列化后哈希，NFR-SEC-03 content_digest）。"""
    canonical = json.dumps(
        snapshot_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── 服务本体 ────────────────────────────────────────────────────────────────


class VersioningService:
    """版本控制服务：save 自动快照 / 两版对比 / 引用式回滚 + 钩子联动。"""

    def __init__(
        self,
        store: VersionStore,
        *,
        resync_hook: ResyncHook | None = None,
        audit_hook: AuditHook | None = None,
    ) -> None:
        self._store = store
        self._resync: ResyncHook = resync_hook or (lambda item_id, action, version: None)
        self._audit: AuditHook = audit_hook or (lambda event: None)

    # ── 读路径（对比/查看为读操作不留审计痕，SR-KB-022 §8）──────────────────

    def get_item(self, item_id: str) -> ItemRecord:
        """读条目当前态；不存在抛 ItemNotFound。"""
        item = self._store.get_item(item_id)
        if item is None:
            raise ItemNotFound(f"条目不存在: {item_id}")
        return item

    def list_versions(self, item_id: str) -> list[VersionSnapshot]:
        """版本历史（按版本号升序；API 层按时间线倒序展示）。"""
        self.get_item(item_id)
        return self._store.list_snapshots(item_id)

    def get_snapshot(self, item_id: str, version: int) -> VersionSnapshot:
        """读单个历史版本快照；版本号非法/不存在显式报错。"""
        self.get_item(item_id)
        if version < 1:
            raise InvalidVersion(f"版本号必须 ≥ 1: {version}")
        snapshot = self._store.get_snapshot(item_id, version)
        if snapshot is None:
            raise SnapshotNotFound(f"版本不存在: V{version}")
        return snapshot

    def diff(self, item_id: str, version_from: int, version_to: int) -> DiffReport:
        """任意两版本对比（服务端计算，只下发结构化差异，NFR-PER-05）。"""
        self.get_item(item_id)
        if version_from < 1 or version_to < 1:
            raise InvalidVersion(
                f"版本号必须 ≥ 1: from={version_from}, to={version_to}"
            )
        snap_a = self.get_snapshot(item_id, version_from)
        snap_b = self.get_snapshot(item_id, version_to)
        return diff_snapshots(
            snap_a.snapshot_json,
            snap_b.snapshot_json,
            from_version=version_from,
            to_version=version_to,
        )

    # ── 写路径：保存（自动快照）─────────────────────────────────────────────

    def create_item(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str,
        change_note: str = "",
    ) -> tuple[ItemRecord, VersionSnapshot]:
        """创建条目并写入首版快照 V1（登记行 + replace + append，与保存同路径）。"""
        self._check_payload(payload)
        record = self._store.create_item(payload, actor)
        try:
            snapshot = self._write_version(
                record.item_id,
                payload,
                new_version=1,
                actor=actor,
                change_note=change_note or "创建条目",
                rollback_from=None,
                expected_version=0,
            )
        except Exception:
            # 首版写失败即放弃整条登记（对齐 PG 事务回滚语义；正常路径不触发）
            raise
        return self.get_item(record.item_id), snapshot

    def save(
        self,
        item_id: str,
        payload: Mapping[str, Any],
        *,
        actor: str,
        change_note: str = "",
        expected_version: int | None = None,
    ) -> VersionSnapshot:
        """保存条目：乐观校验 → CAS 推进版本指针 → 追加线性新版本快照 → 审计。

        expected_version 非 None 且与 current_version 不匹配 → VersionConflict
        （40901「条目已被他人修改，请刷新后重试」），版本链不变。
        """
        self._check_payload(payload)
        item = self.get_item(item_id)
        self._check_expected(item, expected_version)
        return self._write_version(
            item_id,
            payload,
            new_version=item.current_version + 1,
            actor=actor,
            change_note=change_note,
            rollback_from=None,
            expected_version=item.current_version,
        )

    # ── 写路径：回滚（引用旧快照生成新版本）────────────────────────────────

    def rollback(
        self,
        item_id: str,
        to_version: int,
        *,
        actor: str,
        reason: str = "",
        expected_version: int | None = None,
    ) -> VersionSnapshot:
        """回滚：物理上从不覆盖/删除任何历史快照（SR-KB-022 §6.3）。

        流程：乐观校验 → 取目标版本快照（深拷贝，后续演化不污染历史）→
        以 new_version = current + 1 追加**引用旧快照的新版本**
        （rollback_from=to_version，change_note 形如「回滚至 V3：原因」）→
        审计 kb_rollback → 目标内容为 published 时触发 resync_hook 一次
        （FR-KB-017 增量重索引）。
        """
        if to_version < 1:
            raise InvalidVersion(f"回滚目标版本号必须 ≥ 1: {to_version}")
        item = self.get_item(item_id)
        self._check_expected(item, expected_version)
        source = self._store.get_snapshot(item_id, to_version)
        if source is None:
            raise SnapshotNotFound(f"目标版本不存在: V{to_version}")
        snapshot_json = deepcopy(source.snapshot_json)  # 深拷贝旧快照
        change_note = f"回滚至 V{to_version}" + (f"：{reason}" if reason else "")
        new_version = item.current_version + 1
        snapshot = self._write_version(
            item_id,
            snapshot_json,
            new_version=new_version,
            actor=actor,
            change_note=change_note,
            rollback_from=to_version,
            expected_version=item.current_version,
        )
        self._emit_audit(
            actor,
            AUDIT_ROLLBACK,
            item_id,
            new_version,
            snapshot.snapshot_json,
            detail={"to_version": to_version, "reason": reason},
        )
        if snapshot_json.get("status") in RESYNC_STATUSES:
            # 仅已发布内容需要重索引；draft 回滚不进检索索引（SR-KB-022 §6.3）
            self._resync(item_id, _RESYNC_ACTION_UPSERT, new_version)
        return snapshot

    # ── 内部工具 ─────────────────────────────────────────────────────────────

    def _check_payload(self, payload: Mapping[str, Any]) -> None:
        """条目载荷轻量校验（对齐 03-数据库设计 §3.1 的 CHECK/NOT NULL 约束）。"""
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise InvalidItemPayload("title 必填（非空字符串）")
        if not isinstance(payload.get("content"), str):
            raise InvalidItemPayload("content 必填（字符串）")
        status = payload.get("status", "draft")
        if status not in ALLOWED_STATUS:
            raise InvalidItemPayload(
                f"status 非法: {status!r}（合法值 {sorted(ALLOWED_STATUS)}）"
            )

    @staticmethod
    def _check_expected(item: ItemRecord, expected_version: int | None) -> None:
        """乐观并发前置校验：携带的期望版本落后即 40901（SR-KB-020 同口径）。"""
        if expected_version is not None and expected_version != item.current_version:
            raise VersionConflict(
                f"条目已被他人修改（当前 V{item.current_version}，"
                f"期望 V{expected_version}），请刷新后重试"
            )

    def _write_version(
        self,
        item_id: str,
        payload: Mapping[str, Any],
        *,
        new_version: int,
        actor: str,
        change_note: str,
        rollback_from: int | None,
        expected_version: int,
    ) -> VersionSnapshot:
        """版本链唯一写入口：CAS 推指针 + 追加快照 + kb_change 审计。

        存储层 CAS 失败（并发窗口内指针已被推进）同样收敛为 VersionConflict，
        与前置校验构成双重乐观锁（对齐 SQL 的 WHERE current_version=$old）。
        """
        written = self._store.replace_item(
            item_id,
            payload,
            expected_version=expected_version,
            new_version=new_version,
            updated_by=actor,
        )
        if not written:
            raise VersionConflict(
                f"条目已被他人修改（期望 V{expected_version}），请刷新后重试"
            )
        snapshot = VersionSnapshot(
            version=new_version,
            snapshot_json=deepcopy(dict(payload)),
            change_note=change_note,
            created_by=actor,
            rollback_from=rollback_from,
        )
        self._store.append_snapshot(item_id, snapshot)  # UNIQUE 幂等：重放 no-op
        self._emit_audit(actor, AUDIT_SNAPSHOT, item_id, new_version, snapshot.snapshot_json)
        return snapshot

    def _emit_audit(
        self,
        actor: str,
        action: str,
        item_id: str,
        version: int,
        snapshot_json: Mapping[str, Any],
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """组装审计事件并交钩子留痕（NFR-SEC-03；哈希链由平台组件承接）。"""
        event: dict[str, Any] = {
            "actor": actor,
            "action": action,
            "target": f"knowledge_item:{item_id}",
            "version": version,
            "content_digest": digest_of(snapshot_json),
            "occurred_at": utc_now().isoformat(timespec="milliseconds"),
        }
        if detail is not None:
            event["detail"] = detail
        self._audit(event)
