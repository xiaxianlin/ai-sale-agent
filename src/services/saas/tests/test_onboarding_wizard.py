# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""开通向导单元测试（FR-SAAS-002，SR-SAAS-002 §6.1/§6.3/§6.4/§9；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：创建预分配、合法四步流转、时区×语言值域、激活物化（模板复制/预估）、
  幂等激活重放同结果；
- 边界：企业名 64/65、账号数恰等上限/超一、素材空列表与他租户前缀、预估恰等配额；
- 异常：非 configuring 态修改、缺步骤激活、差异项缺失、非法迁移+重复激活、
  凭证落库密文无明文回显、未知 uid；
- 并发：同企业名恰一胜；同幂等键并发激活恰一个任务；
- 降级：预估超配额 42901 fail-fast（无任务、状态不前进、提示升配额）、
  failed 回退重试闭环。

验收口径映射：SRS §16.6「新租户纯配置接入演练（无代码变更）」；UC-13。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-002-配置化部署.md §6.1、§6.3、§6.4、§9
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.saas.core.onboarding.wizard import (
    DEFAULT_QUOTA,
    OnboardingError,
    OnboardingWizard,
    QuotaExceeded,
    QuotaPolicy,
)

# --------------------------------------------------------------------------- 标准装配


def valid_company(name: str = "演练租户A") -> dict:
    """第①步合法载荷（SR §6.5 演练口径）。"""
    return {
        "name": name,
        "brand": {"logo": "https://oss.example.com/tenants/1/brand/logo.png",
                  "primary": "#1E63E9"},
        "timezone": "Asia/Shanghai",
        "language": "zh-CN",
    }


def valid_materials(tenant_id: int, sizes: tuple[int, ...] = (1024, 2048)) -> list[dict]:
    """第②步合法素材登记（OSS 直传 t:{tenant}/raw/ 前缀）。"""
    return [{"oss_key": f"t:{tenant_id}/raw/mat-{i}.pdf", "size": s, "format": "pdf"}
            for i, s in enumerate(sizes)]


def valid_accounts(n: int = 1) -> dict:
    """第③步合法账号参数（凭证为占位明文，提交即加密）。"""
    return {"accounts": [{"platform": "alibaba", "username": f"acc-{i}",
                          "credential": f"secret-{i}-0123456789"} for i in range(n)]}


def valid_strategy() -> dict:
    """第④步策略参数：模板预填 + 差异项显式覆盖。"""
    return {
        "strategy.grade.thresholds": {"A": 80, "B": 60},
        "risk.max_ops_per_day": 200,
        "strategy.followup.cadence.A": "1h",
        "kb.top_k": 5,
    }


def fill_three_steps(wizard: OnboardingWizard, uid: str) -> None:
    """完成①②③步（不含④）。"""
    ob = wizard.store.get(uid)
    wizard.submit_step(uid, "company_info", valid_company())
    wizard.submit_step(uid, "kb_init", {"materials": valid_materials(ob.tenant_id)})
    wizard.submit_step(uid, "accounts", valid_accounts())


# ───────────────────────── 等价类：创建 / 四步流转 / 激活物化 / 幂等 ─────────────────────────


class TestEquivalence:
    def test_create_predistributes_tenant_and_configuring(self) -> None:
        """创建向导：预分配 tenant_id（OSS 前缀/隔离键需稳定 id），初始 configuring。"""
        wizard = OnboardingWizard()
        ob = wizard.create(plan="standard")
        assert ob.status == "configuring" and ob.tenant_id > 0 and ob.plan == "standard"

    def test_four_legal_steps_complete(self) -> None:
        """合法四步依次提交 → 全部完成（all_done + steps_done 齐全）。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        ob = wizard.store.get(uid)
        assert wizard.submit_step(uid, "company_info", valid_company()).all_done is False
        wizard.submit_step(uid, "kb_init", {"materials": valid_materials(ob.tenant_id)})
        wizard.submit_step(uid, "accounts", valid_accounts())
        last = wizard.submit_step(uid, "strategy", valid_strategy())
        assert last.all_done is True
        assert sorted(last.steps_done) == sorted(wizard.STEPS)

    def test_company_info_legal_value_domains(self) -> None:
        """合法时区×语言组合等价类全部接受（Asia/Shanghai×zh-CN 与 UTC×en）。"""
        wizard = OnboardingWizard()
        for i, (tz, lang) in enumerate((("Asia/Shanghai", "zh-CN"), ("UTC", "en"))):
            uid = wizard.create().uid
            payload = valid_company(f"值域租户{i}")
            payload["timezone"], payload["language"] = tz, lang
            assert wizard.submit_step(uid, "company_info", payload).step == "company_info"

    def test_activate_materializes_and_triggers_kb_task(self) -> None:
        """激活：物化模板复制（覆盖值生效）+ 触发初始化任务 + 状态 initializing。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        fill_three_steps(wizard, uid)
        wizard.submit_step(uid, "strategy", valid_strategy())
        result = wizard.activate(uid, idem_key="act-001")
        assert result.status == "initializing" and result.kb_task_uid
        assert result.cfg_rows == len(wizard.templates.entries)
        assert wizard.store.get(uid).status == "initializing"
        rows = {r.cfg_key: r.cfg_value for r in wizard.templates.copied_rows(result.tenant_id)}
        assert rows["risk.max_ops_per_day"] == 200  # 差异项覆盖值
        assert rows["kb.top_k"] == 5  # 默认键被覆盖

    def test_activate_idempotent_replay_same_result(self) -> None:
        """同 Idempotency-Key 重放：返回首次结果且不重复建任务。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        fill_three_steps(wizard, uid)
        wizard.submit_step(uid, "strategy", valid_strategy())
        first = wizard.activate(uid, idem_key="act-idem")
        replay = wizard.activate(uid, idem_key="act-idem")
        assert (first.kb_task_uid, first.tenant_id) == (replay.kb_task_uid, replay.tenant_id)
        assert len(wizard.kb_init.tasks) == 1


# ───────────────────────── 边界：长度 / 数量 / 前缀 / 配额恰等 ─────────────────────────


class TestBoundary:
    def test_company_name_length_edge_64_ok_65_rejected(self) -> None:
        """企业名 64 字符合法、65 字符拒绝（40001）。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        wizard.submit_step(uid, "company_info", valid_company("企" * 64))
        wizard2 = OnboardingWizard()
        uid2 = wizard2.create().uid
        with pytest.raises(OnboardingError) as ei:
            wizard2.submit_step(uid2, "company_info", valid_company("企" * 65))
        assert ei.value.code == 40001

    def test_accounts_edge_at_quota_ok_over_rejected(self) -> None:
        """账号数恰等套餐上限（4）合法，超一（5）拒绝。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        ob = wizard.store.get(uid)
        wizard.submit_step(uid, "company_info", valid_company())
        wizard.submit_step(uid, "kb_init", {"materials": valid_materials(ob.tenant_id)})
        wizard.submit_step(uid, "accounts", valid_accounts(DEFAULT_QUOTA.max_accounts))
        with pytest.raises(OnboardingError) as ei:
            wizard.submit_step(uid, "accounts", valid_accounts(DEFAULT_QUOTA.max_accounts + 1))
        assert ei.value.code == 40001

    def test_materials_empty_and_foreign_prefix_rejected(self) -> None:
        """素材边界：空列表拒绝；他租户 oss 前缀（t:999/…）拒绝（NFR-SEC-07 隔离）。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        with pytest.raises(OnboardingError):
            wizard.submit_step(uid, "kb_init", {"materials": []})
        with pytest.raises(OnboardingError) as ei:
            wizard.submit_step(uid, "kb_init",
                               {"materials": [{"oss_key": "t:999/raw/x.pdf",
                                               "size": 10, "format": "pdf"}]})
        assert ei.value.code == 40001

    def test_estimate_exactly_at_quota_passes(self) -> None:
        """配额恰等边界不阻断：预估结果恰等套餐上限（storage/chunks 全等）→ 激活成功；
        上限各减一点 → 42901 拒绝（差一点即拒绝）。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        fill_three_steps(wizard, uid)
        ob = wizard.store.get(uid)
        materials = valid_materials(ob.tenant_id, sizes=(1024**2,))  # 1MiB 素材
        wizard.submit_step(uid, "kb_init", {"materials": materials})
        wizard.submit_step(uid, "strategy", valid_strategy())
        est = wizard.estimate(materials)
        # 恰等配额：storage 与 chunks 均取预估全等值 → 通过
        wizard.quota = QuotaPolicy(plan="standard",
                                   storage_gb=est.vec_gb + est.oss_gb,
                                   kb_max_chunks=est.chunks, max_accounts=4)
        assert wizard.activate(uid, idem_key="act-edge").status == "initializing"
        # 差一点即拒绝：storage 上限略低
        wizard2 = OnboardingWizard(
            quota_policy=QuotaPolicy(plan="standard",
                                     storage_gb=est.vec_gb + est.oss_gb - 1e-9,
                                     kb_max_chunks=est.chunks, max_accounts=4))
        uid2 = wizard2.create().uid
        ob2 = wizard2.store.get(uid2)
        wizard2.submit_step(uid2, "company_info", valid_company("恰差租户"))
        wizard2.submit_step(uid2, "kb_init", {"materials": materials})
        wizard2.submit_step(uid2, "accounts", valid_accounts())
        wizard2.submit_step(uid2, "strategy", valid_strategy())
        with pytest.raises(QuotaExceeded):
            wizard2.activate(uid2, idem_key="act-edge-2")


# ───────────────────────── 异常：状态守卫 / 缺步骤 / 差异项 / 密文 ─────────────────────────


class TestExceptions:
    def test_step_state_guard_initializing_rejects_edit(self) -> None:
        """initializing 态提交 company-info → 40001 拒绝修改（仅 configuring 可编辑）。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        fill_three_steps(wizard, uid)
        wizard.submit_step(uid, "strategy", valid_strategy())
        wizard.activate(uid, idem_key="act-003")
        with pytest.raises(OnboardingError) as ei:
            wizard.submit_step(uid, "company_info", valid_company("改名租户"))
        assert ei.value.code == 40001

    def test_activate_incomplete_lists_missing(self) -> None:
        """缺步骤激活 → 40001 并列缺失步骤。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        wizard.submit_step(uid, "company_info", valid_company())
        with pytest.raises(OnboardingError) as ei:
            wizard.activate(uid, idem_key="act-004")
        assert ei.value.code == 40001
        assert "kb_init" in ei.value.message and "accounts" in ei.value.message

    def test_required_diff_missing_lists_keys(self) -> None:
        """strategy 缺差异项必填键 → 40001，提示完整差异项清单。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        with pytest.raises(OnboardingError) as ei:
            wizard.submit_step(uid, "strategy", {"kb.top_k": 5})
        assert ei.value.code == 40001
        assert "strategy.grade.thresholds" in ei.value.message
        assert "risk.max_ops_per_day" in ei.value.message

    def test_illegal_transition_and_double_activate_rejected(self) -> None:
        """非法迁移（configuring→active / active→configuring）与重复激活一律 40001，历史不变。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        with pytest.raises(OnboardingError):
            wizard.transit(uid, "configuring", "active", by="ops-admin")
        fill_three_steps(wizard, uid)
        wizard.submit_step(uid, "strategy", valid_strategy())
        wizard.activate(uid, idem_key="act-005")
        with pytest.raises(OnboardingError):
            wizard.activate(uid, idem_key="act-006")
        with pytest.raises(OnboardingError):
            wizard.transit(uid, "active", "configuring", by="ops-admin")

    def test_credential_encrypted_no_plaintext_echo(self) -> None:
        """账号凭证提交即加密：快照与掩码回显均无明文（NFR-SEC-04）。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        wizard.submit_step(uid, "accounts", valid_accounts(1))
        snapshot = json.dumps(wizard.store.get(uid).steps["accounts"], ensure_ascii=False)
        assert "secret-0-0123456789" not in snapshot  # 明文不落库
        masked = wizard.masked_accounts(uid)
        assert masked[0]["credential"].startswith("<enc:")  # 仅掩码
        assert "secret-0" not in masked[0]["credential"]


# ───────────────────────── 并发：同企业名恰一胜 / 同幂等键恰一任务 ─────────────────────────


class TestConcurrency:
    def test_concurrent_same_company_name_exactly_one_wins(self) -> None:
        """并发同企业名注册：恰一个向导成功，其余 40001（唯一性约束）。"""
        wizard = OnboardingWizard()
        uids = [wizard.create().uid for _ in range(4)]

        def claim(uid: str):
            try:
                wizard.submit_step(uid, "company_info", valid_company("同名企业"))
                return uid
            except OnboardingError:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = [w for w in pool.map(claim, uids) if w is not None]
        assert len(winners) == 1
        assert wizard.store.get(winners[0]).company_name == "同名企业"

    def test_concurrent_activate_same_idem_key_single_task(self) -> None:
        """并发同幂等键激活：恰创建一个初始化任务、结果一致。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        fill_three_steps(wizard, uid)
        wizard.submit_step(uid, "strategy", valid_strategy())
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: wizard.activate(uid, "act-race"), range(5)))
        assert len({r.kb_task_uid for r in results}) == 1
        assert len(wizard.kb_init.tasks) == 1


# ───────────────────────── 降级：配额 fail-fast / failed 回退闭环 ─────────────────────────


class TestDegradation:
    def test_estimate_over_quota_blocks_before_task(self) -> None:
        """预估 1.5GB 超配额 1GB → 42901 阻断：不产生任务、状态不前进、提示升配额。"""
        wizard = OnboardingWizard()
        uid = wizard.create().uid
        fill_three_steps(wizard, uid)
        ob = wizard.store.get(uid)
        big = [{"oss_key": f"t:{ob.tenant_id}/raw/big.bin", "size": int(1.5 * 2**30),
                "format": "pdf"}]
        wizard.submit_step(uid, "kb_init", {"materials": big})
        wizard.submit_step(uid, "strategy", valid_strategy())
        with pytest.raises(QuotaExceeded) as ei:
            wizard.activate(uid, idem_key="act-429")
        assert ei.value.code == 42901 and "升配额" in ei.value.message
        assert wizard.store.get(uid).status == "configuring"
        assert wizard.kb_init.tasks == {}

    def test_failed_state_retry_path_recovers_to_active(self) -> None:
        """failed 回退态：重试仅补跑 failed 文件重回 initializing，修复后 active。"""
        from services.saas.core.onboarding.kb_init import DeterministicError

        wizard = OnboardingWizard()
        kb = wizard.kb_init
        uid = wizard.create().uid
        fill_three_steps(wizard, uid)
        wizard.submit_step(uid, "strategy", valid_strategy())
        wizard.activate(uid, idem_key="act-007")
        task = kb.task_of(uid)

        def failing_pipeline(oss_key: str, fmt: str) -> None:
            if oss_key.endswith("mat-1.pdf"):
                raise DeterministicError("格式不支持")

        kb.run_task(task.uid, failing_pipeline)
        assert wizard.store.get(uid).status == "failed"
        assert wizard.retry_kb(uid) == 1  # 仅重置 failed 文件
        assert wizard.store.get(uid).status == "initializing"
        kb.run_task(task.uid, lambda k, f: None)
        assert wizard.store.get(uid).status == "active"
