"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-03 审计日志（哈希链）单元测试。

覆盖（SR-NFR-SEC-03 §9 测试要点的进程内等价形态）：
- 规范化序列化确定性（键序无关 / 数值字符串化）与哈希敏感性；
- 链完整性：首条 GENESIS、prev_hash 串联、千条全量重算通过；
- 篡改检出（row_tampered）/ 删除检出（gap_or_delete）/ 伪造注入检出（chain_break）；
- 对账结果自身入链（audit_reconcile）+ 断链高级告警路由；
- 并发追加不分叉（线程锁串行化：id 唯一连续、全链校验通过）；
- event_id 幂等去重；result 字段闭枚举校验；PG 存储仅留接口。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import ValidationError

from foundation.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditEventType,
    AuditRow,
    AuditStorage,
    AuditWriter,
    InMemoryAuditStorage,
    PostgresAuditStorage,
    canonical_payload,
    compute_content_digest,
    compute_row_hash,
)


def _row(**overrides: Any) -> dict[str, Any]:
    """构造参与哈希计算的最小行字典（九个规范化字段）。"""
    row: dict[str, Any] = {
        "tenant_id": 1,
        "actor": "u1",
        "action_type": "login",
        "target": "auth",
        "result": "success",
        "detail": {},
        "trace_id": "",
        "ip": None,
        "created_at": "2026-10-09T02:00:00.000000+00:00",
    }
    row.update(overrides)
    return row


def _writer_with(n: int) -> tuple[AuditWriter, InMemoryAuditStorage]:
    """构造已写入 n 条知识修改事件的写入器与存储（白盒引用供篡改用例）。"""
    storage = InMemoryAuditStorage()
    writer = AuditWriter(storage)
    for i in range(n):
        writer.append(
            AuditEvent(actor=f"u{i}", action_type=AuditEventType.KB_CHANGE, target=f"knowledge_item:{i}")
        )
    return writer, storage


# --------------------------------------------------------------------------- 哈希计算（SR §6.2）


class TestHasher:
    def test_canonical_serialization_deterministic(self) -> None:
        """同内容、键序打乱：canonical_payload 逐字节一致（SR §9）。"""
        a = _row(detail={"k1": "v1", "k2": "v2"})
        b = _row(detail={"k2": "v2", "k1": "v1"})
        b = {k: b[k] for k in reversed(list(b))}  # 顶层键序同样打乱
        assert canonical_payload(a) == canonical_payload(b)
        assert compute_row_hash(GENESIS_HASH, a) == compute_row_hash(GENESIS_HASH, b)
        assert compute_content_digest(a) == compute_content_digest(b)

    def test_detail_numeric_stringized(self) -> None:
        """detail 数值统一字符串化：100 与 "100" 哈希一致（规避 jsonb 数值重解析差异）。"""
        a = _row(detail={"amount": 100, "qty": 1.5})
        b = _row(detail={"amount": "100", "qty": "1.5"})
        assert canonical_payload(a) == canonical_payload(b)

    def test_row_hash_sensitivity(self) -> None:
        """内容或前驱变化 → row_hash 变化（链上任何改动都会传播）。"""
        base = _row()
        assert compute_row_hash(GENESIS_HASH, base) != compute_row_hash(GENESIS_HASH, _row(actor="u2"))
        assert compute_row_hash(GENESIS_HASH, base) != compute_row_hash("ab" * 32, base)

    def test_genesis_value(self) -> None:
        """创世 prev_hash 为 64 个 '0'（SR §6.3）。"""
        assert GENESIS_HASH == "0" * 64


# --------------------------------------------------------------------------- 链完整性（SR §6.3）


class TestChainAppend:
    def test_first_row_genesis_and_chain_linked(self) -> None:
        writer = AuditWriter(InMemoryAuditStorage())
        for i in range(5):
            writer.append(
                AuditEvent(actor="ai", action_type=AuditEventType.HUMAN_TAKEOVER, target=f"conversation:{i}")
            )
        rows = list(writer.storage.iter_rows())
        assert [r.id for r in rows] == [1, 2, 3, 4, 5]
        assert rows[0].prev_hash == GENESIS_HASH  # 首条前驱为创世值
        for prev, cur in zip(rows, rows[1:]):
            assert cur.prev_hash == prev.row_hash  # 链式串联
        assert all(r.content_digest and r.row_hash for r in rows)
        report = writer.verify_chain()
        assert report.ok
        assert report.checked_until_id == 5
        assert report.chain_head_hash == rows[-1].row_hash

    def test_thousand_rows_full_recompute(self) -> None:
        """1000 条顺序写入后全链重算通过（SR §9 量级项）。"""
        writer = AuditWriter(InMemoryAuditStorage())
        for i in range(1000):
            writer.append(
                AuditEvent(actor="ai", action_type=AuditEventType.KB_CHANGE, target=f"knowledge_item:{i}")
            )
        assert writer.verify_chain().ok

    def test_result_field_validated(self) -> None:
        """result 闭枚举校验（success/denied/failed，SR §4 CHECK 约束的应用侧前移）。"""
        with pytest.raises(ValidationError):
            AuditEvent(actor="u", action_type=AuditEventType.LOGIN, target="auth", result="bogus")

    def test_append_idempotent_event_id(self) -> None:
        """幂等去重：同一 event_id 重复入队只入链一次（at-least-once 消费防重复，SR §6.3）。"""
        writer = AuditWriter(InMemoryAuditStorage())
        first = writer.append(
            AuditEvent(
                actor="u1", action_type=AuditEventType.SENSITIVE_CONFIRM,
                target="price_draft:55", event_id="evt-1",
            )
        )
        writer.append(AuditEvent(actor="u2", action_type=AuditEventType.LOGIN, target="auth"))
        again = writer.append(
            AuditEvent(
                actor="u1", action_type=AuditEventType.SENSITIVE_CONFIRM,
                target="price_draft:55", event_id="evt-1",
            )
        )
        assert first == again == 1
        assert len(list(writer.storage.iter_rows())) == 2  # 未重复入链
        assert writer.verify_chain().ok


# --------------------------------------------------------------------------- 篡改/删除/注入检出（SR §6.4）


class TestTamperDetection:
    def test_reconcile_detects_tamper(self) -> None:
        """篡改任一行内容 → 对账报 row_tampered（SR §9 test_reconcile_detects_tamper）。"""
        writer, storage = _writer_with(3)
        storage.rows[1].detail = {"evil": "rewritten"}  # 白盒篡改第 2 行（模拟绕过 DB 防护）
        report = writer.verify_chain()
        assert not report.ok
        tampered = [p for p in report.problems if p["kind"] == "row_tampered"]
        assert tampered and tampered[0]["id"] == 2

    def test_reconcile_detects_delete(self) -> None:
        """删除中间行 → 对账报 gap_or_delete（SR §9 test_reconcile_detects_delete）。"""
        writer, storage = _writer_with(5)
        storage.rows.pop(2)  # 删除 id=3 的中间行
        report = writer.verify_chain()
        assert not report.ok
        gaps = [p for p in report.problems if p["kind"] == "gap_or_delete"]
        assert gaps and gaps[0]["expect_id"] == 3 and gaps[0]["found_id"] == 4

    def test_reconcile_detects_injection(self) -> None:
        """伪造插入一行（prev_hash 断链）→ 对账报 chain_break（SR §9 test_reconcile_detects_inject）。"""
        writer, storage = _writer_with(3)
        forged = storage.rows[0].model_copy(
            update={"id": 0, "prev_hash": GENESIS_HASH, "row_hash": "f" * 64, "event_id": None}
        )
        storage.append_row(forged)  # 绕过写入器直接入链（prev 与真实链尾不符）
        report = writer.verify_chain()
        assert not report.ok
        assert any(p["kind"] == "chain_break" for p in report.problems)

    def test_tamper_alert_routed(self) -> None:
        """断链即高级告警：alerter 收到失败报告（钉钉/微信路由由部署侧接入，FR-AG-007）。"""
        writer, storage = _writer_with(2)
        storage.rows[0].actor = "mallory"
        alerts: list[Any] = []
        report = writer.run_reconcile(alerter=alerts.append)
        assert not report.ok
        assert alerts == [report]

    def test_reconcile_self_logged_pass(self) -> None:
        """对账结果自身入链（通过侧）：链尾出现 audit_reconcile/success 行（SR §9 test_reconcile_self_logged）。"""
        writer, _ = _writer_with(2)
        report = writer.run_reconcile()
        assert report.ok
        rows = list(writer.storage.iter_rows())
        assert rows[-1].action_type == "audit_reconcile"
        assert rows[-1].result == "success"
        assert rows[-1].actor == "system"
        assert writer.verify_chain().ok  # 对账事件本身可被后续对账校验（防篡改闭环）

    def test_reconcile_self_logged_fail(self) -> None:
        """对账结果自身入链（失败侧）：result=failed 且 problems 进 detail。"""
        writer, storage = _writer_with(2)
        storage.rows[1].target = "knowledge_item:hacked"
        report = writer.run_reconcile()
        assert not report.ok
        rows = list(writer.storage.iter_rows())
        assert rows[-1].action_type == "audit_reconcile" and rows[-1].result == "failed"
        assert rows[-1].detail["problems"]


# --------------------------------------------------------------------------- 并发串行化（SR §6.3）


class TestConcurrency:
    def test_concurrent_append_no_fork(self) -> None:
        """多线程并发 append：链不分叉、id 唯一连续、全链校验通过（SR §9 test_concurrent_append_no_fork）。"""
        writer = AuditWriter(InMemoryAuditStorage())
        total, workers = 96, 12

        def _produce(worker: int) -> list[int]:
            ids: list[int] = []
            for i in range(total // workers):
                ids.append(
                    writer.append(
                        AuditEvent(
                            actor=f"worker-{worker}",
                            action_type=AuditEventType.LOGIN,
                            target=f"auth:{worker}-{i}",
                        )
                    )
                )
            return ids

        with ThreadPoolExecutor(max_workers=workers) as pool:
            all_ids = [i for ids in pool.map(_produce, range(workers)) for i in ids]
        assert sorted(all_ids) == list(range(1, total + 1))  # id 唯一且连续（无争抢、无空洞）
        assert len(list(writer.storage.iter_rows())) == total
        assert writer.verify_chain().ok  # 无链分叉：每条 prev_hash 均为唯一链尾

    def test_concurrent_unique_events_all_hashed(self) -> None:
        """并发写入的行哈希互不相同（同一 prev_hash 不被两条行复用）。"""
        writer = AuditWriter(InMemoryAuditStorage())

        def _produce(worker: int) -> None:
            for i in range(20):
                writer.append(
                    AuditEvent(actor=f"w{worker}", action_type=AuditEventType.CONFIG_CHANGE,
                               target=f"app_config:{i}")
                )

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(_produce, range(6)))
        rows = list(writer.storage.iter_rows())
        hashes = {r.row_hash for r in rows}
        assert len(hashes) == len(rows) == 120


# --------------------------------------------------------------------------- 存储抽象


class TestStorageContract:
    def test_in_memory_storage_is_audit_storage(self) -> None:
        assert isinstance(InMemoryAuditStorage(), AuditStorage)

    def test_postgres_storage_interface_only(self) -> None:
        """PG 实现留接口：调用即 NotImplementedError（随服务侧迁移脚本落地）。"""
        storage = PostgresAuditStorage(dsn="postgresql://app@localhost/ai_sale")
        assert isinstance(storage, AuditStorage)
        with pytest.raises(NotImplementedError):
            storage.append_row(
                AuditRow(
                    actor="u",
                    action_type="login",
                    target="auth",
                    result="success",
                    created_at="2026-10-09T00:00:00.000000+00:00",
                )
            )

    def test_empty_chain_verify_passes(self) -> None:
        writer = AuditWriter(InMemoryAuditStorage())
        report = writer.verify_chain()
        assert report.ok and report.checked_until_id == 0 and report.chain_head_hash == GENESIS_HASH
