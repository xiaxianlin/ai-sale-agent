# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""PG 真实存储层 × 内存实现行为对拍集成测试（容器化部署联调，迭代 0.3 收口）。

同一操作序列分别驱动内存实现与 psycopg PG 实现（本地容器 postgres:16，
deploy/compose/compose.yaml），断言结果逐字段一致：

- 条目 CRUD / 乐观锁冲突（并发双写恰一胜）/ 软删后不可见 / 分页排序
  （KnowledgeItemStore 协议，api/admin/items.py）；
- 版本链与回滚快照：线性追加 / CAS / UNIQUE 幂等 / 逐字节不变
  （VersionStore 协议，core/versioning/store.py）；并跑 VersioningService
  端到端对拍（service 零改动换 PG 存储）；
- 日志迭代过滤：批次 / 时间窗 / 聚合器同口径快照
  （IngestLogStore 协议，core/reporting/cleaning_report.py）；
- apply_migrations 幂等重放。

运行条件：环境变量 KB_PG_DSN（缺省 postgresql://kb:kb_local_dev@localhost:15432/kb，
凭证与 deploy/compose/compose.yaml 一致）；不可连接时整文件跳过（CI 不受影响）。
隔离：每条用例开头 TRUNCATE 三表并 RESTART IDENTITY（IDENTITY 序号归零，
与内存实现的自增 id 对拍口径一致）。
"""
from __future__ import annotations

import sys
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import os  # noqa: E402

PG_DSN = os.environ.get(
    "KB_PG_DSN", "postgresql://kb:kb_local_dev@localhost:15432/kb"
)
MIGRATIONS_DIR = _REPO_ROOT / "migrations"

from services.kb.api.admin.items import (  # noqa: E402
    InMemoryKnowledgeItemStore,
    KnowledgeItemRecord,
)
from services.kb.core.reporting.cleaning_report import (  # noqa: E402
    CleanReportAggregator,
    InMemoryIngestLogStore,
    IngestLogRecord,
    ReportScope,
)
from services.kb.core.stores.pg import (  # noqa: E402
    PgConnection,
    PsycopgIngestLogStore,
    PsycopgKnowledgeItemStore,
    PsycopgVersionStore,
    apply_migrations,
)
from services.kb.core.versioning import VersioningService  # noqa: E402
from services.kb.core.versioning.store import (  # noqa: E402
    InMemoryVersionStore,
    ItemRecord,
    VersionSnapshot,
)


def _pg_unavailable_reason() -> str:
    """连接探针：3 秒超时；返回空串=可用，否则为中文跳过原因。"""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - 环境分支
        return f"psycopg 未安装（{exc}）——PG 集成测试跳过"
    try:
        with psycopg.connect(PG_DSN, connect_timeout=3):
            return ""
    except Exception as exc:  # noqa: BLE001 - 任何连接失败均整文件跳过
        return f"PostgreSQL 不可连接（{PG_DSN}）：{type(exc).__name__}: {exc}"


_PG_SKIP_REASON: str = _pg_unavailable_reason()

pytestmark = pytest.mark.skipif(
    bool(_PG_SKIP_REASON), reason=_PG_SKIP_REASON or "PostgreSQL 可连接"
)


# ── 测试脚手架 ──────────────────────────────────────────────────────────────


@pytest.fixture()
def conn() -> Any:
    """每条用例一条 PG 连接；开头 TRUNCATE 三表（RESTART IDENTITY）保证隔离。"""
    with PgConnection(PG_DSN) as connection:
        with connection.transaction() as cur:
            cur.execute(
                "TRUNCATE TABLE knowledge_item, knowledge_version, "
                "ingest_pipeline_log RESTART IDENTITY"
            )
        yield connection


_T0 = datetime(2026, 1, 5, 8, 0, 0, tzinfo=timezone.utc)


def _ts(minute: int) -> datetime:
    """确定性时间戳：T0 + minute 分钟（微秒为 0，TIMESTAMPTZ 无损往返）。"""
    return _T0 + timedelta(minutes=minute)


def _iso(moment: datetime | None) -> str | None:
    """时间字段的可比较形态（None 透传；aware 恒为 UTC 表示）。"""
    return None if moment is None else moment.astimezone(timezone.utc).isoformat()


def _item_d(record: KnowledgeItemRecord) -> dict[str, Any]:
    """条目 record 的对拍视图（全字段；uid/时间统一规范形态）。"""
    return {
        "uid": str(record.uid),
        "kb_type": record.kb_type,
        "title": record.title,
        "content": record.content,
        "metadata": record.metadata,
        "status": record.status,
        "current_version": record.current_version,
        "sync_status": record.sync_status,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "updated_by": record.updated_by,
        "deleted_at": _iso(record.deleted_at),
    }


def _snap_d(snapshot: VersionSnapshot) -> dict[str, Any]:
    """快照的全量对拍视图（含 created_at，用例均显式给定）。"""
    return {
        "version": snapshot.version,
        "snapshot_json": snapshot.snapshot_json,
        "change_note": snapshot.change_note,
        "created_at": _iso(snapshot.created_at),
        "created_by": snapshot.created_by,
        "rollback_from": snapshot.rollback_from,
    }


def _snap_core(snapshot: VersionSnapshot) -> dict[str, Any]:
    """快照对拍视图（不含 created_at——服务路径快照时间取写入时刻，两侧相差微秒）。"""
    return {k: v for k, v in _snap_d(snapshot).items() if k != "created_at"}


def _rec_core(record: ItemRecord) -> dict[str, Any]:
    """ItemRecord 对拍视图（不含 item_id/时间——各自生成、口径见模块 docstring）。"""
    return {
        "payload": record.payload,
        "current_version": record.current_version,
        "created_by": record.created_by,
        "updated_by": record.updated_by,
    }


def _log_d(record: IngestLogRecord) -> dict[str, Any]:
    """留痕 record 对拍视图（id 由两侧同序分配：内存自增 / IDENTITY 归零重放）。"""
    return {
        "id": record.id,
        "source_file": record.source_file,
        "source_format": record.source_format,
        "stage": record.stage,
        "action_detail": dict(record.action_detail),
        "created_at": _iso(record.created_at),
        "output": record.output,
        "status": record.status,
        "batch_id": record.batch_id,
        "kb_type": record.kb_type,
    }


# ── 条目存储对拍：CRUD / 乐观锁 / 软删 / 分页排序 ───────────────────────────


def test_item_crud_and_sync_status_parity(conn: PgConnection) -> None:
    """条目 CRUD + 乐观锁 + 同步状态回写：两侧逐字段一致；uid 冲突同款失败。"""
    mem = InMemoryKnowledgeItemStore()
    pg_store = PsycopgKnowledgeItemStore(conn)
    uid = str(uuid.uuid4())
    base: dict[str, Any] = dict(
        kb_type="product",
        title="ST-3200 支架",
        content="# 概述\n工业级铝合金支架。",
        metadata={"sku": "ST-3200", "tags": ["询价", "复购"], "qty": 3, "spec": {"层数": 2}},
        status="draft",
        current_version=1,
        uid=uid,
        sync_status="none",
        created_at=_ts(0),
        updated_at=_ts(0),
        updated_by="alice",
    )
    assert _item_d(mem.create(KnowledgeItemRecord(**base))) == _item_d(
        pg_store.create(KnowledgeItemRecord(**base))
    )
    # uid 冲突：内存 ValueError / PG UNIQUE → ValueError（中文）
    with pytest.raises(ValueError, match="冲突"):
        mem.create(KnowledgeItemRecord(**base))
    with pytest.raises(ValueError, match="冲突"):
        pg_store.create(KnowledgeItemRecord(**base))
    assert _item_d(mem.get(uid)) == _item_d(pg_store.get(uid))
    # 乐观锁保存成功（V1 → V2）
    bumped = {**base, "title": "改后标题", "current_version": 2, "updated_at": _ts(5), "updated_by": "bob"}
    assert _item_d(mem.save(KnowledgeItemRecord(**bumped), expected_version=1)) == _item_d(
        pg_store.save(KnowledgeItemRecord(**bumped), expected_version=1)
    )
    # 过期期望版本 → None（行未变化）
    stale = {**bumped, "title": "陈旧写", "current_version": 3, "updated_at": _ts(6)}
    assert mem.save(KnowledgeItemRecord(**stale), expected_version=1) is None
    assert pg_store.save(KnowledgeItemRecord(**stale), expected_version=1) is None
    # expected_version 缺省：跳过版本比对直写
    blind = {**bumped, "content": "盲写内容", "current_version": 3, "updated_at": _ts(7)}
    assert _item_d(mem.save(KnowledgeItemRecord(**blind))) == _item_d(
        pg_store.save(KnowledgeItemRecord(**blind))
    )
    # 同步状态回写 + 未知 uid
    mem.set_sync_status(uid, "synced")
    pg_store.set_sync_status(uid, "synced")
    assert _item_d(mem.get(uid)) == _item_d(pg_store.get(uid))
    assert mem.get("k-nope") is None and pg_store.get("k-nope") is None


def test_optimistic_lock_concurrent_double_write_parity(conn: PgConnection) -> None:
    """并发双写同一 expected_version：内存与 PG 均恰一胜者、终态版本一致。"""
    mem = InMemoryKnowledgeItemStore()

    def race(store_a: Any, store_b: Any, rounds: int = 5) -> list[str]:
        """跑 rounds 轮双写竞速，返回每轮胜者标签；断言恰一胜 + 终态一致。

        内存形态：两线程共享同一存储实例（存储内锁仲裁）；PG 形态：两线程各持
        一条连接的存储实例（数据库行锁仲裁）——各自的真实并发形态。
        """
        winners: list[str] = []
        for i in range(rounds):
            uid = str(uuid.uuid4())
            store_a.create(
                KnowledgeItemRecord(kb_type="skill", title=f"冲突基线{i}", uid=uid,
                                    created_at=_ts(i), updated_at=_ts(i))
            )
            results: dict[str, Any] = {}
            barrier = threading.Barrier(2)

            def writer(tag: str, store: Any) -> None:
                candidate = KnowledgeItemRecord(
                    kb_type="skill", title=f"{tag} 的写入", uid=uid,
                    current_version=2, updated_at=_ts(50),
                )
                barrier.wait(timeout=5)
                results[tag] = store.save(candidate, expected_version=1)

            threads = [
                threading.Thread(target=writer, args=("A", store_a)),
                threading.Thread(target=writer, args=("B", store_b)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            survived = [tag for tag in ("A", "B") if results[tag] is not None]
            assert len(survived) == 1, f"第 {i} 轮应恰一胜者，实得 {results}"
            final = store_a.get(uid)
            assert final is not None and final.current_version == 2
            assert final.title == f"{survived[0]} 的写入"
            winners.append(survived[0])
        return winners

    assert len(race(mem, mem)) == 5
    with PgConnection(PG_DSN) as conn_b:
        assert len(race(PsycopgKnowledgeItemStore(conn), PsycopgKnowledgeItemStore(conn_b))) == 5


def test_soft_delete_visibility_parity(conn: PgConnection) -> None:
    """软删：get/query/iter_all 不可见、版本 +1、同步态 removed；重复删/删后写均 None。"""
    mem = InMemoryKnowledgeItemStore()
    pg_store = PsycopgKnowledgeItemStore(conn)
    uid_keep = str(uuid.uuid4())
    uid_gone = str(uuid.uuid4())
    for store in (mem, pg_store):
        store.create(KnowledgeItemRecord(kb_type="process", title="保留条目", uid=uid_keep,
                                         created_at=_ts(0), updated_at=_ts(0), updated_by="alice"))
        store.create(KnowledgeItemRecord(kb_type="process", title="待删条目", uid=uid_gone,
                                         created_at=_ts(0), updated_at=_ts(1), updated_by="alice"))
    moment = _ts(30)
    mem_deleted = mem.soft_delete(uid_gone, now=moment, actor="carol")
    pg_deleted = pg_store.soft_delete(uid_gone, now=moment, actor="carol")
    assert mem_deleted is not None and pg_deleted is not None
    assert _item_d(mem_deleted) == _item_d(pg_deleted)
    assert mem_deleted.deleted_at == moment and pg_deleted.deleted_at == moment
    # 软删后不可见：get / query / iter_all（缺省）
    assert mem.get(uid_gone) is None and pg_store.get(uid_gone) is None
    mem_rows, mem_total = mem.query(page_size=10)
    pg_rows, pg_total = pg_store.query(page_size=10)
    assert mem_total == pg_total == 1
    assert [r.uid for r in mem_rows] == [r.uid for r in pg_rows] == [uid_keep]
    assert len(list(mem.iter_all())) == len(list(pg_store.iter_all())) == 1
    # include_deleted=True：软删行可见且字段一致
    mem_all = {r.uid: r for r in mem.iter_all(include_deleted=True)}
    pg_all = {r.uid: r for r in pg_store.iter_all(include_deleted=True)}
    assert set(mem_all) == set(pg_all) == {uid_keep, uid_gone}
    assert _item_d(mem_all[uid_gone]) == _item_d(pg_all[uid_gone])
    # 重复软删 / 删后保存 → None；同步状态回写对软删行静默
    assert mem.soft_delete(uid_gone, now=_ts(31)) is None
    assert pg_store.soft_delete(uid_gone, now=_ts(31)) is None
    ghost = KnowledgeItemRecord(kb_type="process", title="删后写", uid=uid_gone,
                                current_version=99, updated_at=_ts(32))
    assert mem.save(ghost, expected_version=None) is None
    assert pg_store.save(ghost, expected_version=None) is None


def test_query_filter_pagination_order_parity(conn: PgConnection) -> None:
    """过滤（kb_type/status/关键词含 LIKE 元字符）+ 排序（updated_at 降序 uid 决胜）+ 分页。"""
    mem = InMemoryKnowledgeItemStore()
    pg_store = PsycopgKnowledgeItemStore(conn)
    uids = [str(uuid.UUID(int=i)) for i in range(1, 8)]  # 确定性升序 uid
    minutes = [10, 10, 20, 30, 30, 40, 5]
    for i, (uid, minute) in enumerate(zip(uids, minutes)):
        title = "Promo 50%_off 特价" if i == 3 else f"条目-{i} 支架"
        record = KnowledgeItemRecord(
            kb_type="product" if i % 2 == 0 else "skill",
            title=title,
            content=f"工业级铝合金支架 {i}",
            status="published" if i in (2, 5) else "draft",
            uid=uid,
            created_at=_ts(0),
            updated_at=_ts(minute),
            updated_by=f"u{i}",
        )
        assert _item_d(mem.create(record)) == _item_d(pg_store.create(record))

    def assert_query_equal(**kwargs: Any) -> None:
        mem_rows, mem_total = mem.query(**kwargs)
        pg_rows, pg_total = pg_store.query(**kwargs)
        assert ([r.uid for r in mem_rows], mem_total) == ([r.uid for r in pg_rows], pg_total)

    # 分页：page 1~4（page_size=3），排序 updated_at 降序 + uid 升序决胜（同刻 30/10 两组）
    for page in (1, 2, 3, 4):
        assert_query_equal(page=page, page_size=3)
    assert_query_equal(page_size=100)
    # 维度过滤
    assert_query_equal(kb_type="product", page_size=10)
    assert_query_equal(status="published", page_size=10)
    assert_query_equal(kb_type="skill", status="draft", page_size=10)
    # 关键词：中文包含 / 大小写不敏感 / LIKE 元字符（% _）按字面匹配 / 零命中
    assert_query_equal(q="支架", page_size=10)
    assert_query_equal(q="promo", page_size=10)
    assert_query_equal(q="50%_o", page_size=10)
    assert_query_equal(q="不存在的词", page_size=10)


# ── 版本存储对拍：版本链 / CAS / 幂等 / 回滚快照 ────────────────────────────


def _version_payloads() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """三个版本的业务五字段载荷（含嵌套 metadata，覆盖 JSONB 往返）。"""
    v1 = {
        "kb_type": "product",
        "title": "ST-3200 支架",
        "content": "# 概述\n工业级铝合金支架。",
        "metadata": {"sku": "ST-3200", "tags": ["询价", "复购"], "spec": {"层数": 2}},
        "status": "published",
    }
    v2 = {**v1, "title": "ST-3200 支架 V2", "metadata": {**v1["metadata"], "qty": 9}}
    v3 = {**v2, "content": "# 概述\n第三代产品。", "status": "draft"}
    return v1, v2, v3


def test_version_chain_and_rollback_snapshots_parity(conn: PgConnection) -> None:
    """版本链线性追加 / CAS 冲突 / UNIQUE 幂等 / 回滚引用式快照逐字节不变。"""
    mem = InMemoryVersionStore()
    pg_store = PsycopgVersionStore(conn)
    v1, v2, v3 = _version_payloads()
    mem_item = mem.create_item(v1, "alice")
    pg_item = pg_store.create_item(v1, "alice")
    assert _rec_core(mem_item) == _rec_core(pg_item)  # current_version=0 占位 + payload
    mem_id, pg_id = mem_item.item_id, pg_item.item_id
    snap1 = VersionSnapshot(version=1, snapshot_json=dict(v1), change_note="创建条目",
                            created_at=_ts(0), created_by="alice")
    snap2 = VersionSnapshot(version=2, snapshot_json=dict(v2), change_note="第二版",
                            created_at=_ts(1), created_by="bob")
    snap3 = VersionSnapshot(version=3, snapshot_json=dict(v3), change_note="第三版",
                            created_at=_ts(2), created_by="bob")
    for store, item_id in ((mem, mem_id), (pg_store, pg_id)):
        store.append_snapshot(item_id, snap1)
    assert _snap_d(mem.get_snapshot(mem_id, 1)) == _snap_d(pg_store.get_snapshot(pg_id, 1))
    # CAS 推进：V0 → V1 → V2
    assert mem.replace_item(mem_id, v2, expected_version=0, new_version=1, updated_by="bob")
    assert pg_store.replace_item(pg_id, v2, expected_version=0, new_version=1, updated_by="bob")
    for store, item_id in ((mem, mem_id), (pg_store, pg_id)):
        store.append_snapshot(item_id, snap2)
    # CAS 落后（期望 V0，实际 V1）→ False；非线性跳号 → ValueError
    assert mem.replace_item(mem_id, v3, expected_version=0, new_version=1, updated_by="x") is False
    assert pg_store.replace_item(pg_id, v3, expected_version=0, new_version=1, updated_by="x") is False
    with pytest.raises(ValueError, match="线性递增"):
        mem.replace_item(mem_id, v3, expected_version=1, new_version=3, updated_by="x")
    with pytest.raises(ValueError, match="线性递增"):
        pg_store.replace_item(pg_id, v3, expected_version=1, new_version=3, updated_by="x")
    assert mem.replace_item(mem_id, v3, expected_version=1, new_version=2, updated_by="carol")
    assert pg_store.replace_item(pg_id, v3, expected_version=1, new_version=2, updated_by="carol")
    for store, item_id in ((mem, mem_id), (pg_store, pg_id)):
        store.append_snapshot(item_id, snap3)
    assert _rec_core(mem.get_item(mem_id)) == _rec_core(pg_store.get_item(pg_id))
    # UNIQUE(item_id, version) 幂等：重放既有版本为 no-op（链长不变）
    for store, item_id in ((mem, mem_id), (pg_store, pg_id)):
        store.append_snapshot(item_id, snap2)
    assert len(mem.list_snapshots(mem_id)) == len(pg_store.list_snapshots(pg_id)) == 3
    # 跳号 / 非法版本号 / 未知条目：同款异常
    with pytest.raises(ValueError, match="线性递增"):
        mem.append_snapshot(mem_id, VersionSnapshot(version=5, snapshot_json=dict(v3)))
    with pytest.raises(ValueError, match="线性递增"):
        pg_store.append_snapshot(pg_id, VersionSnapshot(version=5, snapshot_json=dict(v3)))
    with pytest.raises(ValueError, match="≥ 1"):
        mem.append_snapshot(mem_id, VersionSnapshot(version=0, snapshot_json=dict(v3)))
    with pytest.raises(ValueError, match="≥ 1"):
        pg_store.append_snapshot(pg_id, VersionSnapshot(version=0, snapshot_json=dict(v3)))
    with pytest.raises(KeyError):
        mem.append_snapshot("ghost-item", snap1)
    with pytest.raises(KeyError):
        pg_store.append_snapshot("ghost-item", snap1)
    # 回滚：V4 引用 V1 快照（引用式，不覆盖历史）；此后 V1 逐字节不变
    snap4 = VersionSnapshot(version=4, snapshot_json=deepcopy(v1), change_note="回滚至 V1",
                            created_at=_ts(9), created_by="carol", rollback_from=1)
    for store, item_id in ((mem, mem_id), (pg_store, pg_id)):
        store.append_snapshot(item_id, snap4)
    mem_chain = [_snap_d(s) for s in mem.list_snapshots(mem_id)]
    pg_chain = [_snap_d(s) for s in pg_store.list_snapshots(pg_id)]
    assert [d["version"] for d in mem_chain] == [d["version"] for d in pg_chain] == [1, 2, 3, 4]
    assert mem_chain == pg_chain  # 版本升序 + 全字段（含 change_note/rollback_from/时间）
    assert mem_chain[0] == _snap_d(snap1)  # 回滚后 V1 快照逐字节不变（内存侧自证）
    assert pg_chain[0] == _snap_d(snap1)   # PG 侧同样逐字节不变


def test_versioning_service_over_pg_store_parity(conn: PgConnection) -> None:
    """VersioningService 端到端对拍：service 零改动换 PG 存储，保存/回滚行为一致。"""
    mem_store = InMemoryVersionStore()
    pg_store = PsycopgVersionStore(conn)
    mem_svc = VersioningService(mem_store)
    pg_svc = VersioningService(pg_store)
    v1, v2, _ = _version_payloads()
    mem_item, mem_v1 = mem_svc.create_item(v1, actor="alice")
    pg_item, pg_v1 = pg_svc.create_item(v1, actor="alice")
    assert _snap_core(mem_v1) == _snap_core(pg_v1)  # V1：创建条目
    mem_v2 = mem_svc.save(mem_item.item_id, v2, actor="bob", expected_version=1)
    pg_v2 = pg_svc.save(pg_item.item_id, v2, actor="bob", expected_version=1)
    assert _snap_core(mem_v2) == _snap_core(pg_v2)  # V2：正常保存
    # 乐观冲突：expected_version 落后 → VersionConflict（40901 语义）两侧一致
    from services.kb.core.versioning import VersionConflict  # noqa: E402 - 就近引用避免顶部噪声

    with pytest.raises(VersionConflict):
        mem_svc.save(mem_item.item_id, v2, actor="late", expected_version=1)
    with pytest.raises(VersionConflict):
        pg_svc.save(pg_item.item_id, v2, actor="late", expected_version=1)
    # 回滚：V3 引用 V1；当前态恢复 v1 载荷；V1 快照保持不变
    mem_rb = mem_svc.rollback(mem_item.item_id, 1, actor="carol", reason="写错版本",
                              expected_version=2)
    pg_rb = pg_svc.rollback(pg_item.item_id, 1, actor="carol", reason="写错版本",
                            expected_version=2)
    assert _snap_core(mem_rb) == _snap_core(pg_rb)
    assert mem_rb.rollback_from == pg_rb.rollback_from == 1
    assert _rec_core(mem_svc.get_item(mem_item.item_id)) == _rec_core(
        pg_svc.get_item(pg_item.item_id)
    )
    assert pg_svc.get_item(pg_item.item_id).payload == v1
    assert _snap_core(mem_store.get_snapshot(mem_item.item_id, 1)) == _snap_core(
        pg_store.get_snapshot(pg_item.item_id, 1)
    )
    assert [s.version for s in pg_store.list_snapshots(pg_item.item_id)] == [1, 2, 3]
    assert [_snap_core(s) for s in mem_store.list_snapshots(mem_item.item_id)] == [
        _snap_core(s) for s in pg_store.list_snapshots(pg_item.item_id)
    ]


# ── 留痕存储对拍：迭代过滤 + 聚合器同口径 ──────────────────────────────────


def _log_records() -> list[IngestLogRecord]:
    """10 条留痕 + 1 条重放：覆盖批次缺失/失败/待审/output 空实/规则集版本。"""
    records: list[IngestLogRecord] = []
    for i in range(10):
        detail: dict[str, Any] = {"blocks_in": 10 + i, "kb_type": ["product", "skill"][i % 2]}
        if i % 4:
            detail["batch_id"] = f"B-{i % 3}"
        if i == 5:
            detail.update({"status": "failed", "error_code": "E_MASK", "error_msg": "规则冲突"})
        if i == 7:
            detail["status"] = "pending"
        if i % 2 == 0:
            detail["mask_ruleset_version"] = "v2026.09"
        records.append(
            IngestLogRecord(
                source_file=f"in/f{i:02d}.pdf",
                source_format=["pdf", "word"][i % 2],
                stage=["parse", "dedup", "mask"][i % 3],
                action_detail=detail,
                created_at=_ts(i + 1),
                output=f"p0/clean-output/{i:02d}.json" if i % 3 else "",
            )
        )
    # 重放：修复 f05 的 mask 失败（同 source_file+stage，id 更大 → latest 口径翻转为成功）
    records.append(
        IngestLogRecord(
            source_file="in/f05.pdf",
            source_format="word",
            stage="mask",
            action_detail={"batch_id": "B-2", "masked_blocks": 4, "status": "success"},
            created_at=_ts(20),
        )
    )
    return records


def test_ingest_log_iteration_filters_parity(conn: PgConnection) -> None:
    """iter_logs 批次/时间窗过滤 + 留痕字段（含 output 归并）逐字段对拍。"""
    mem = InMemoryIngestLogStore()
    pg_store = PsycopgIngestLogStore(conn)
    for record in _log_records():
        mem.append(record)
        pg_store.append(record)

    def assert_logs_equal(**kwargs: Any) -> None:
        assert [_log_d(r) for r in mem.iter_logs(**kwargs)] == [
            _log_d(r) for r in pg_store.iter_logs(**kwargs)
        ]

    assert_logs_equal()  # 全量（写入序 = id 升序；两侧 id 同为 1..11）
    assert_logs_equal(batch_id="B-1")
    assert_logs_equal(batch_id="B-2")
    assert_logs_equal(window_from=_ts(3), window_to=_ts(8))  # [from, to) 左闭右开
    assert_logs_equal(batch_id="B-0", window_from=_ts(1), window_to=_ts(30))  # 组合过滤
    assert_logs_equal(window_from=_ts(30), window_to=_ts(40))  # 空窗（重放行在 _ts(20)）


def test_ingest_log_aggregator_snapshot_parity(conn: PgConnection) -> None:
    """清洗报告聚合器消费两侧 iter_logs：AggSnapshot 全字段一致（含重放 latest 翻转）。"""
    mem = InMemoryIngestLogStore()
    pg_store = PsycopgIngestLogStore(conn)
    for record in _log_records():
        mem.append(record)
        pg_store.append(record)
    for scope in (
        ReportScope(batch_id="B-1"),
        ReportScope(batch_id="B-2"),
        ReportScope(window_from=_ts(1), window_to=_ts(30)),  # 含 f05 失败→重放成功翻转
        ReportScope(window_from=_ts(10), window_to=_ts(30)),
    ):
        snap_mem = CleanReportAggregator(mem).aggregate(scope)
        snap_pg = CleanReportAggregator(pg_store).aggregate(scope)
        assert snap_mem == snap_pg  # totals/by_kb/by_format/file_details/ruleset_version


# ── 迁移执行器 ──────────────────────────────────────────────────────────────


def test_apply_migrations_idempotent() -> None:
    """apply_migrations 按文件名序执行且可幂等重放；三表齐备。"""
    applied_first = apply_migrations(PG_DSN, MIGRATIONS_DIR)
    applied_again = apply_migrations(PG_DSN, MIGRATIONS_DIR)
    assert applied_first == applied_again == ["001_kb_core.sql"]
    with PgConnection(PG_DSN) as connection:
        with connection.transaction() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
            tables = {row[0] for row in cur.fetchall()}
    assert {"knowledge_item", "knowledge_version", "ingest_pipeline_log"} <= tables
