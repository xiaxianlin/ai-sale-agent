# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""知识库初始化长任务单元测试（FR-SAAS-002，SR-SAAS-002 §6.2/§9；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：全量成功→done→active、进度百分比计算、瞬时故障退避重试后成功；
- 边界：恰在重试上限次失败、零素材任务直接完成、重试恰重置 failed 文件；
- 异常：确定性故障不重试直落失败、非 failed 态调重试 40001、未知任务 40401；
- 并发：入队幂等（UNIQUE(task,oss_key)，重复 start 不翻倍）；
- 降级：部分失败→任务与向导齐落 failed（可重试回退态），done 文件不重跑。

验收口径映射：SRS §16.6「知识库初始化可观测可重试」；UC-13。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-002-配置化部署.md §6.2、§9
"""

from __future__ import annotations

import pytest

from services.saas.core.onboarding.kb_init import (
    DeterministicError,
    KbInitService,
    TransientError,
)
from services.saas.core.onboarding.wizard import OnboardingError, OnboardingWizard


def make_ready_wizard(materials: int = 3) -> tuple[OnboardingWizard, str]:
    """装配已激活（initializing）的向导与初始化任务。"""
    wizard = OnboardingWizard()
    uid = wizard.create().uid
    ob = wizard.store.get(uid)
    wizard.submit_step(uid, "company_info", {
        "name": "初始化演练租户", "brand": {"logo": "https://oss.example.com/tenants/1/b/l.png",
                                            "primary": "#1E63E9"},
        "timezone": "Asia/Shanghai", "language": "zh-CN"})
    wizard.submit_step(uid, "kb_init", {
        "materials": [{"oss_key": f"t:{ob.tenant_id}/raw/f-{i}.pdf", "size": 1024,
                       "format": "pdf"} for i in range(materials)]})
    wizard.submit_step(uid, "accounts", {
        "accounts": [{"platform": "alibaba", "username": "acc", "credential": "cred-123456"}]})
    wizard.submit_step(uid, "strategy", {
        "strategy.grade.thresholds": {"A": 80}, "risk.max_ops_per_day": 100,
        "strategy.followup.cadence.A": "1h"})
    wizard.activate(uid, idem_key="kbinit-test")
    return wizard, uid


class TestEquivalence:
    def test_all_done_transits_wizard_to_active(self) -> None:
        """全部文件成功 → 任务 done、向导 initializing→active（租户可用门打开）。"""
        wizard, uid = make_ready_wizard()
        task = wizard.kb_init.task_of(uid)
        final = wizard.kb_init.run_task(task.uid, lambda key, fmt: None)
        assert final == "done"
        assert wizard.store.get(uid).status == "active"
        prog = wizard.kb_init.progress(task.uid)
        assert prog["done_files"] == 3 and prog["failed_files"] == 0
        assert prog["percent"] == 100.0

    def test_progress_percent_partial(self) -> None:
        """进度可查：done 2/3 → percent 66.7（保留 1 位）。"""
        wizard, uid = make_ready_wizard()
        task = wizard.kb_init.task_of(uid)
        keys = [f.oss_key for f in task.files]

        def two_of_three(oss_key: str, fmt: str) -> None:
            if oss_key == keys[2]:
                raise DeterministicError("跳过")

        wizard.kb_init.run_task(task.uid, two_of_three)
        prog = wizard.kb_init.progress(task.uid)
        assert prog["percent"] == 66.7

    def test_transient_retry_then_success(self) -> None:
        """连续 2 次 TransientError 后成功 → attempts=3 且最终 done（退避 30/60）。"""
        wizard, uid = make_ready_wizard(materials=1)
        task = wizard.kb_init.task_of(uid)
        key = task.files[0].oss_key
        flips = {"n": 0}

        def flaky(oss_key: str, fmt: str) -> None:
            if oss_key == key:
                flips["n"] += 1
                if flips["n"] <= 2:
                    raise TransientError("embedding 抖动")

        final = wizard.kb_init.run_task(task.uid, flaky)
        assert final == "done"
        assert task.files[0].attempts == 3
        assert task.files[0].delays == [30, 60]  # 指数退避基数 30（SR §6.2）


# ───────────────────────── 边界：恰等重试上限 / 零素材 / 重置恰 failed ─────────────────────────


class TestBoundary:
    def test_fail_at_retry_max_goes_failed(self) -> None:
        """连续 3 次（恰等 kb_retry_max=3）TransientError → 该文件 failed。"""
        wizard, uid = make_ready_wizard(materials=1)
        task = wizard.kb_init.task_of(uid)
        key = task.files[0].oss_key

        def always_transient(oss_key: str, fmt: str) -> None:
            raise TransientError("持续抖动")

        final = wizard.kb_init.run_task(task.uid, always_transient)
        assert final == "failed"
        assert task.files[0].attempts == 3  # 恰等上限，不再重试
        assert task.files[0].status == "failed"
        assert "持续抖动" in (task.files[0].last_error or "")

    def test_empty_materials_rejected_no_task(self) -> None:
        """零素材边界：第②步即拒绝（空列表 40001），不产生初始化任务。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        with pytest.raises(OnboardingError):
            wizard.submit_step(uid, "kb_init", {"materials": []})
        with pytest.raises(OnboardingError) as ei:
            wizard.kb_init.task_of(uid)
        assert ei.value.code == 40401  # 未建任何任务

    def test_retry_resets_only_failed_files(self) -> None:
        """重试恰重置 failed 文件：done 文件不重跑（不重复入队）。"""
        wizard, uid = make_ready_wizard(materials=2)
        task = wizard.kb_init.task_of(uid)
        keys = [f.oss_key for f in task.files]

        def fail_second(oss_key: str, fmt: str) -> None:
            if oss_key == keys[1]:
                raise DeterministicError("格式不支持")

        wizard.kb_init.run_task(task.uid, fail_second)
        reset = wizard.kb_init.retry_failed(uid)
        assert reset == 1
        assert task.files[0].status == "done"  # done 不重跑
        assert task.files[1].status == "queued"
        assert wizard.store.get(uid).status == "initializing"  # failed→initializing（SR §6.2）
        final = wizard.kb_init.run_task(task.uid, lambda k, f: None)
        assert final == "done"
        assert task.files[0].attempts == 1  # 首文件未二次执行


# ───────────────────────── 异常：确定性失败 / 非法重试 / 未知任务 ─────────────────────────


class TestExceptions:
    def test_deterministic_error_no_retry(self) -> None:
        """确定性故障（格式不支持）：不重试直落 failed（attempts=1 无退避记录）。"""
        wizard, uid = make_ready_wizard(materials=1)
        task = wizard.kb_init.task_of(uid)
        key = task.files[0].oss_key

        def bad_format(oss_key: str, fmt: str) -> None:
            if oss_key == key:
                raise DeterministicError("格式不支持")

        final = wizard.kb_init.run_task(task.uid, bad_format)
        assert final == "failed"
        assert task.files[0].attempts == 1
        assert task.files[0].delays == []

    def test_retry_kb_rejected_and_unknown_task_not_found(self) -> None:
        """异常入口：非 failed 态调 retry_kb → 40001；未知 uid 查任务 → 40401。"""
        wizard = OnboardingWizard()
        with pytest.raises(OnboardingError) as ei:
            wizard.retry_kb(wizard.create().uid)
        assert ei.value.code == 40001
        service = KbInitService(on_transit=lambda *a, **k: None)
        with pytest.raises(OnboardingError) as ei2:
            service.task_of("no-such-uid")
        assert ei2.value.code == 40401


# ───────────────────────── 并发/降级：入队幂等 / 部分失败齐落 failed ─────────────────────────


class TestConcurrencyAndDegrade:
    def test_enqueue_idempotent_unique_task_oss_key(self) -> None:
        """UNIQUE(task,oss_key)：同向导重复 start 文件不翻倍（入队幂等）。"""
        wizard, uid = make_ready_wizard(materials=2)
        task = wizard.kb_init.task_of(uid)
        ob = wizard.store.get(uid)
        wizard.kb_init.start(ob)  # 重复入队
        assert len(task.files) == 2

    def test_partial_failure_lands_failed_state(self) -> None:
        """部分失败：任务与向导齐落 failed（可重试回退态），告警语义字段留痕。"""
        wizard, uid = make_ready_wizard(materials=2)
        task = wizard.kb_init.task_of(uid)
        keys = [f.oss_key for f in task.files]

        def one_fails(oss_key: str, fmt: str) -> None:
            if oss_key == keys[0]:
                raise DeterministicError("图片页数超限")

        final = wizard.kb_init.run_task(task.uid, one_fails)
        assert final == "failed"
        assert task.status == "failed"
        assert wizard.store.get(uid).status == "failed"
        hist = wizard.store.get(uid).state_hist
        assert hist[-1]["to"] == "failed" and hist[-1]["by"] == "system"
