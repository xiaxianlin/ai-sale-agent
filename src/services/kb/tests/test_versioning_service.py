# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
"""版本控制服务单元测试（FR-KB-022 §9：快照链/回滚/钩子/乐观并发）。

覆盖（SR-KB-022 §9 测试要点 service 部分）：
- 保存自动快照：版本链线性（1..N 连续）、深拷贝隔离、UNIQUE(item,version) 幂等；
- 回滚 = 引用旧快照的新版本：rollback_from 标记、v1/v2 快照逐字节不变、
  条目内容恢复、resync 触发一次（published）/不触发（draft）、审计成对留痕；
- 回滚到回滚版本：链仍完整、引用标记指向回滚版本本身；
- 乐观并发：expected_version 落后 → 40901 语义且版本链不变；并发保存恰一胜者；
- 错误路径：目标版本/条目不存在、非法载荷、非法版本号。

实现方案：docs/07-实现方案/KB/SR-KB-022-版本控制.md §2、§6.1、§6.3、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import threading
from typing import Any

import pytest

from services.kb.core.versioning import (
    AUDIT_ROLLBACK,
    AUDIT_SNAPSHOT,
    InvalidItemPayload,
    InvalidVersion,
    ItemNotFound,
    SnapshotNotFound,
    VersionConflict,
    VersioningService,
    digest_of,
)
from services.kb.core.versioning.store import InMemoryVersionStore


# ── 测试脚手架 ──────────────────────────────────────────────────────────────


class _Hooks:
    """resync / audit 钩子录音桩（断言触发次数与载荷）。"""

    def __init__(self) -> None:
        self.resync_calls: list[tuple[str, str, int]] = []
        self.audit_events: list[dict[str, Any]] = []

    def resync(self, item_id: str, action: str, version: int) -> None:
        self.resync_calls.append((item_id, action, version))

    def audit(self, event: dict[str, Any]) -> None:
        self.audit_events.append(event)


def _make_service() -> tuple[VersioningService, _Hooks]:
    hooks = _Hooks()
    service = VersioningService(
        InMemoryVersionStore(), resync_hook=hooks.resync, audit_hook=hooks.audit
    )
    return service, hooks


def _payload(**overrides: Any) -> dict[str, Any]:
    """最小合法条目载荷（业务五字段，03-数据库设计 §3.1）。"""
    base: dict[str, Any] = {
        "kb_type": "product",
        "title": "ST-3200 支架",
        "content": "# 概述\n工业级铝合金支架。",
        "metadata": {"sku": "ST-3200", "tags": ["询价"]},
        "status": "published",
    }
    base.update(overrides)
    return base


# ── 保存自动快照：版本链线性 ────────────────────────────────────────────────


def test_save_appends_linear_snapshot_chain() -> None:
    """create→save→save：版本号 1..3 线性递增，快照内容与当时载荷一致。"""
    service, hooks = _make_service()
    item, v1 = service.create_item(_payload(), actor="admin")
    v2 = service.save(
        item.item_id, _payload(title="改后标题"), actor="admin", expected_version=1
    )
    v3 = service.save(
        item.item_id, _payload(title="再改标题"), actor="admin", expected_version=2
    )
    assert (v1.version, v2.version, v3.version) == (1, 2, 3)
    assert v1.rollback_from is None and v2.rollback_from is None
    snapshots = service.list_versions(item.item_id)
    assert [snap.version for snap in snapshots] == [1, 2, 3]
    assert snapshots[0].snapshot_json["title"] == "ST-3200 支架"
    assert snapshots[2].snapshot_json["title"] == "再改标题"
    assert service.get_item(item.item_id).current_version == 3
    # 保存路径只写 kb_change 审计、不触发重索引（resync 仅回滚联动）
    assert [event["action"] for event in hooks.audit_events] == [AUDIT_SNAPSHOT] * 3
    assert hooks.resync_calls == []


def test_snapshot_deep_copy_isolation() -> None:
    """保存后修改调用方持有的载荷（嵌套 dict/list）不污染已存快照。"""
    service, _ = _make_service()
    payload = _payload(metadata={"sku": "ST-3200", "tags": ["询价"]})
    item, _ = service.create_item(payload, actor="admin")
    payload["metadata"]["tags"].append("售后")   # 保存后演化
    payload["title"] = "调用方后来改的"
    stored = service.get_snapshot(item.item_id, 1)
    assert stored.snapshot_json["metadata"]["tags"] == ["询价"]
    assert stored.snapshot_json["title"] == "ST-3200 支架"


def test_store_append_snapshot_unique_idempotent_and_gap_rejected() -> None:
    """存储层约束：UNIQUE(item,version) 重放 no-op；跳号/非法版本/未知条目显式失败。"""
    from services.kb.core.versioning import VersionSnapshot

    store = InMemoryVersionStore()
    record = store.create_item(_payload(), "admin")
    store.replace_item(
        record.item_id, _payload(), expected_version=0, new_version=1, updated_by="admin"
    )
    snap = VersionSnapshot(version=1, snapshot_json=_payload(), created_by="admin")
    store.append_snapshot(record.item_id, snap)
    store.append_snapshot(record.item_id, snap)  # UNIQUE 幂等：重放 no-op
    assert len(store.list_snapshots(record.item_id)) == 1

    with pytest.raises(ValueError, match="线性递增"):
        store.append_snapshot(
            record.item_id, VersionSnapshot(version=3, snapshot_json=_payload())
        )
    with pytest.raises(ValueError, match="≥ 1"):
        store.append_snapshot(
            record.item_id, VersionSnapshot(version=0, snapshot_json=_payload())
        )
    with pytest.raises(KeyError):
        store.append_snapshot("no-such-item", VersionSnapshot(version=1, snapshot_json={}))


# ── 回滚：引用旧快照生成新版本 ─────────────────────────────────────────────


def _seed_two_versions(service: VersioningService) -> str:
    """v1（published，原始标题）→ v2（改标题+加标签），返回 item_id。"""
    item, _ = service.create_item(
        _payload(metadata={"sku": "ST-3200", "tags": ["询价"]}), actor="admin-01"
    )
    service.save(
        item.item_id,
        _payload(
            title="ST-3200 支架（含认证）",
            metadata={"sku": "ST-3200", "tags": ["询价", "售后"]},
        ),
        actor="admin-02",
        expected_version=1,
    )
    return item.item_id


def test_rollback_creates_referencing_new_version_and_resyncs_once() -> None:
    """v1→v2→回滚至 v1：生成 v3（rollback_from=1），历史逐字节不变，resync 恰一次。"""
    service, hooks = _make_service()
    item_id = _seed_two_versions(service)
    v1_before = service.get_snapshot(item_id, 1)
    v2_before = service.get_snapshot(item_id, 2)

    v3 = service.rollback(item_id, 1, actor="admin-01", reason="误改", expected_version=2)

    assert v3.version == 3 and v3.rollback_from == 1
    assert v3.change_note == "回滚至 V1：误改"
    # 条目当前态恢复为 v1 内容；版本链完整（1..3）
    assert service.get_item(item_id).payload["title"] == v1_before.snapshot_json["title"]
    assert [snap.version for snap in service.list_versions(item_id)] == [1, 2, 3]
    # v1/v2 快照逐字节不变（永不覆盖：内容与摘要一致）
    assert service.get_snapshot(item_id, 1).snapshot_json == v1_before.snapshot_json
    assert service.get_snapshot(item_id, 2).snapshot_json == v2_before.snapshot_json
    assert digest_of(service.get_snapshot(item_id, 2).snapshot_json) == digest_of(
        v2_before.snapshot_json
    )
    # resync 恰触发一次（published 目标内容；save 不触发）
    assert hooks.resync_calls == [(item_id, "upsert", 3)]
    # 审计：3 次 kb_change（v1/v2/v3 写入）+ 1 次 kb_rollback
    actions = [event["action"] for event in hooks.audit_events]
    assert actions == [AUDIT_SNAPSHOT, AUDIT_SNAPSHOT, AUDIT_SNAPSHOT, AUDIT_ROLLBACK]
    rollback_event = hooks.audit_events[-1]
    assert rollback_event["actor"] == "admin-01"
    assert rollback_event["detail"] == {"to_version": 1, "reason": "误改"}
    assert len(rollback_event["content_digest"]) == 64  # sha256 摘要留痕


def test_rollback_draft_does_not_resync() -> None:
    """目标内容为 draft：回滚成功但不触发重索引（不进检索索引）。"""
    service, hooks = _make_service()
    item, _ = service.create_item(_payload(status="draft"), actor="admin")
    service.save(item.item_id, _payload(status="draft", title="草稿改"), actor="admin")
    v3 = service.rollback(item.item_id, 1, actor="admin")
    assert v3.version == 3 and hooks.resync_calls == []


def test_rollback_to_rollback_version_keeps_chain_intact() -> None:
    """回滚到回滚版本：v3 为回滚产物，再保存 v4 后回滚至 v3 → v5 引用 v3，链 1..5。"""
    service, hooks = _make_service()
    item_id = _seed_two_versions(service)
    service.rollback(item_id, 1, actor="admin-01")          # v3：回滚自 V1
    service.save(item_id, _payload(title="回滚后再编辑"), actor="admin-02")  # v4
    v3_snapshot = service.get_snapshot(item_id, 3)

    v5 = service.rollback(item_id, 3, actor="admin-03", reason="回到上次回滚点")

    assert v5.version == 5 and v5.rollback_from == 3
    assert [snap.version for snap in service.list_versions(item_id)] == [1, 2, 3, 4, 5]
    # v5 内容 = v3 快照内容（引用旧快照语义对回滚产物同样成立）
    assert service.get_snapshot(item_id, 5).snapshot_json == v3_snapshot.snapshot_json
    assert service.get_item(item_id).payload == v3_snapshot.snapshot_json
    # 两次回滚各触发一次 resync（published）
    assert hooks.resync_calls == [(item_id, "upsert", 3), (item_id, "upsert", 5)]


def test_rollback_restores_metadata_deep_structure() -> None:
    """回滚恢复嵌套结构（dict/list 深拷贝，不与旧快照共享引用）。"""
    service, _ = _make_service()
    item, _ = service.create_item(
        _payload(metadata={"sku": "S1", "tags": ["询价"], "spec": {"weight": 10}}),
        actor="admin",
    )
    service.save(
        item.item_id,
        _payload(metadata={"sku": "S1", "tags": ["询价", "报价"], "spec": {"weight": 20}}),
        actor="admin",
    )
    service.rollback(item.item_id, 1, actor="admin")
    payload = service.get_item(item.item_id).payload
    assert payload["metadata"] == {"sku": "S1", "tags": ["询价"], "spec": {"weight": 10}}
    # 回滚后的条目载荷与 v1 快照内容相等但为独立拷贝（后续演化不污染历史）
    v1 = service.get_snapshot(item.item_id, 1)
    payload["metadata"]["tags"].append("污染探针")
    assert v1.snapshot_json["metadata"]["tags"] == ["询价"]


# ── 乐观并发 ───────────────────────────────────────────────────────────────


def test_save_stale_expected_version_conflicts() -> None:
    """expected_version 落后：40901 语义冲突，条目与版本链不变。"""
    service, _ = _make_service()
    item, _ = service.create_item(_payload(), actor="admin")
    service.save(item.item_id, _payload(title="V2 标题"), actor="user-a", expected_version=1)
    with pytest.raises(VersionConflict, match="已被他人修改"):
        service.save(
            item.item_id, _payload(title="迟到写入"), actor="user-b", expected_version=1
        )
    assert service.get_item(item.item_id).current_version == 2
    assert [snap.version for snap in service.list_versions(item.item_id)] == [1, 2]
    assert service.get_snapshot(item.item_id, 2).snapshot_json["title"] == "V2 标题"


def test_rollback_stale_expected_version_conflicts() -> None:
    """回滚同样受乐观锁约束：expected_version 落后即冲突且不建版。"""
    service, _ = _make_service()
    item_id = _seed_two_versions(service)
    with pytest.raises(VersionConflict, match="已被他人修改"):
        service.rollback(item_id, 1, actor="user-b", expected_version=1)  # 当前已是 V2
    assert service.get_item(item_id).current_version == 2
    assert len(service.list_versions(item_id)) == 2


def test_concurrent_save_exactly_one_winner() -> None:
    """并发保存同携 expected_version=1：CAS 串行化后恰一胜者，链无双写。"""
    service, _ = _make_service()
    item, _ = service.create_item(_payload(), actor="admin")
    outcomes: list[bool] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            service.save(
                item.item_id,
                _payload(title=f"并发标题-{index}"),
                actor=f"user-{index}",
                expected_version=1,
            )
            won = True
        except VersionConflict:
            won = False
        with lock:
            outcomes.append(won)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(outcomes) == 1
    assert service.get_item(item.item_id).current_version == 2
    assert len(service.list_versions(item.item_id)) == 2


def test_save_without_expected_version_allows_worker_replay() -> None:
    """expected_version 缺省：跳过前置校验（worker 重放路径），CAS 仍兜底线性推进。"""
    service, _ = _make_service()
    item, _ = service.create_item(_payload(), actor="worker")
    snapshot = service.save(item.item_id, _payload(title="重放写入"), actor="worker")
    assert snapshot.version == 2


# ── 错误路径与读路径 ───────────────────────────────────────────────────────


def test_rollback_missing_version_and_unknown_item() -> None:
    """目标版本不存在 / 条目不存在 / 版本号非法：显式 40401/40001 语义。"""
    service, _ = _make_service()
    item_id = _seed_two_versions(service)
    with pytest.raises(SnapshotNotFound):
        service.rollback(item_id, 99, actor="admin")
    with pytest.raises(ItemNotFound):
        service.rollback("no-such-item", 1, actor="admin")
    with pytest.raises(InvalidVersion):
        service.rollback(item_id, 0, actor="admin")
    with pytest.raises(InvalidVersion):
        service.diff(item_id, 0, 2)
    assert service.get_item(item_id).current_version == 2  # 链不变


def test_invalid_payload_rejected() -> None:
    """载荷校验（对齐表约束）：缺 title / 非法 status → 40001 语义且不建条目。"""
    service, _ = _make_service()
    with pytest.raises(InvalidItemPayload, match="title"):
        service.create_item(_payload(title=" "), actor="admin")
    with pytest.raises(InvalidItemPayload, match="status"):
        service.create_item(_payload(status="unknown"), actor="admin")


def test_diff_service_versions_injected() -> None:
    """service.diff：任意两版对比，报告携带 from/to 版本号。"""
    service, _ = _make_service()
    item_id = _seed_two_versions(service)
    report = service.diff(item_id, 1, 2)
    assert (report.from_version, report.to_version) == (1, 2)
    kinds = {change.field: change.kind for change in report.changed}
    assert kinds["title"] == "text"
    assert kinds["metadata"] == "tree"
    assert report.is_empty() is False


def test_diff_same_version_empty_report() -> None:
    """同版对比：空报告（幂等读，不报错）。"""
    service, _ = _make_service()
    item_id = _seed_two_versions(service)
    assert service.diff(item_id, 2, 2).is_empty()
