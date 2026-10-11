# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""TDD 缺陷修复钉子（2026-10-10 平台域审计报告的两个并发缺陷）。

按开发规范 §9.1 红→绿流程：先固化失败测试，再修复实现。
1. KeyRegistry.verify 的 last_used_at 整行回写与并发 rotate 丢失更新（轮换被回滚）；
2. BackupRunner.run 无并发防重入锁（同日双触发重复备份）。
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import pytest

from foundation.apikey.keys import hash_key
from foundation.apikey.keys import InMemoryKeyStore, KeyRegistry
from foundation.backup.pipeline import BackupPlan, BackupRunner, RecordingExecutor
from foundation.backup.planner import default_plan


def _fixed_now() -> datetime:
    return datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)  # 低峰窗口内


class TestVerifyVsRotateLostUpdate:
    """缺陷 1：verify 的整行 save 会把并发 rotate 写回的新哈希滚回旧哈希。"""

    def test_verify_concurrent_rotate_hash_not_rolled_back(self) -> None:
        store = InMemoryKeyStore()
        registry = KeyRegistry(store=store, clock=_fixed_now)
        from foundation.apikey.keys import QuotaConfig
        issued = registry.issue(
            consumer="acme", quota=QuotaConfig(qps=5, daily=100), ip_whitelist=[], operator="op"
        )
        plain_old = issued.api_key
        state = {"rotated": False}
        original_find = store.find_by_key_hash

        def find_with_interleaved_rotate(key_hash: str):
            record = original_find(key_hash)
            # 确定性交错：verify 读到（轮换前）记录后、落盘 save 之前，并发 rotate 落地
            if record is not None and not state["rotated"]:
                state["rotated"] = True
                registry.rotate("acme", operator="op")
            return record

        store.find_by_key_hash = find_with_interleaved_rotate  # type: ignore[method-assign]
        registry.verify(plain_old, client_ip=None)

        record = store.find_by_consumer("acme")
        assert record is not None
        # 修复口径：verify 只更新 last_used_at，api_key_hash 保持为轮换后的新哈希
        assert record.api_key_hash != original_find(hash_key(plain_old)), (
            "verify 落盘不得把 api_key_hash 回滚为旧哈希"
        )


class TestBackupRunReentryLock:
    """缺陷 2：run 的 check-then-act 防重入存在并发窗口，双线程同日触发重复备份。"""

    def test_concurrent_run_reentry_second_rejected(self) -> None:
        class SlowExecutor(RecordingExecutor):
            def __init__(self) -> None:
                super().__init__()
                self._gate = threading.Event()

            def run(self, cmd: str):
                self._gate.wait(timeout=2)  # 拉长执行窗口制造并发重叠
                return super().run(cmd)

        runner = BackupRunner(
            plan=default_plan(),
            executor=SlowExecutor(),
        )
        errors: list[Exception] = []
        started = threading.Barrier(2)

        def worker() -> None:
            try:
                started.wait(timeout=2)
                runner.run(date(2026, 10, 10), force=True)
            except Exception as exc:  # noqa: BLE001  第二个触发应被防重入拒绝
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # 两个并发 run：恰好一个成功、另一个被防重入拒绝（不产生两份重复备份）
        assert len(errors) == 1, f"应恰有一个并发触发被拒绝，实际 {len(errors)} 个：{errors}"
