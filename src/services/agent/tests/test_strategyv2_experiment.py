# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 A/B 分桶实验单元测试（FR-SC-003 重点，SR-SC-003 §6.5/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（分桶创建/启停/处理组变体解析/分桶效果统计）/
边界（单元素分桶、比例相同 z=0、样本恰好达 min_sample）/ 异常（分桶键缺失、账号
重叠、空分桶、非法 metric、分桶外账号，均 40001）/ 幂等（重复记录去重）/ 降级
（样本不足给「继续观察」而非结论；draft/paused 实验不生效）。

实现方案：docs/07-实现方案/SC/SR-SC-003-销售策略引擎.md §6.5、§9
"""

from __future__ import annotations

import pytest

from services.agent.core.strategyv2.experiment import AbSplitter, ExperimentError


def make_splitter(**kw: object) -> AbSplitter:
    """标准分桶器（min_sample 注入便于边界测试）。"""
    base: dict = dict(min_sample=10)
    base.update(kw)
    return AbSplitter(**base)  # type: ignore[arg-type]


def make_buckets() -> dict[str, tuple[int, ...]]:
    """标准分桶：对照 [1,3,5] / 处理 [2,4,6]（§6.5 示例形态）。"""
    return {"control": (1, 3, 5), "treatment": (2, 4, 6)}


def started_splitter(**kw: object) -> tuple[AbSplitter, int]:
    """带 running 实验的分桶器（变体：A 级节奏 [1,2,3]）。"""
    splitter = make_splitter(**kw)
    exp = splitter.create("cadence_a_对比", "followup_reply_rate", make_buckets(),
                          {"treatment": {"strategy.followup.cadence.A": [1, 2, 3]}})
    splitter.start(exp.exp_id)
    return splitter, exp.exp_id


class TestCreateAndResolve:
    """等价类：创建/启停/变体解析链。"""

    def test_create_start_conclude_lifecycle(self) -> None:
        """生命周期：draft→running→concluded + 结论写入（审计链可查）。"""
        splitter = make_splitter()
        exp = splitter.create("exp", "followup_reply_rate", make_buckets(), {})
        assert splitter.experiment_of(exp.exp_id).status == "draft"
        splitter.start(exp.exp_id)
        assert splitter.experiment_of(exp.exp_id).status == "running"
        splitter.conclude(exp.exp_id, "处理组回复率显著更高，全量推广 A 级新节奏")
        row = splitter.experiment_of(exp.exp_id)
        assert row.status == "concluded" and "显著" in row.conclusion

    def test_resolve_overrides_treatment_only(self) -> None:
        """配置解析链：处理组命中变体；对照组 None 走常规值（§6.5）。"""
        splitter, exp_id = started_splitter()
        assert splitter.resolve("strategy.followup.cadence.A", account_id=2) == [1, 2, 3]
        assert splitter.resolve("strategy.followup.cadence.A", account_id=1) is None

    def test_resolve_ignores_draft_paused_and_whitelist(self) -> None:
        """降级/安全：draft 与 paused 实验不生效；白名单外键不生效。"""
        splitter = make_splitter()
        exp = splitter.create("exp", "followup_reply_rate", make_buckets(),
                              {"treatment": {"strategy.followup.cadence.A": [1, 2, 3]}})
        assert splitter.resolve("strategy.followup.cadence.A", account_id=2) is None   # draft
        splitter.start(exp.exp_id)
        splitter.pause(exp.exp_id)
        assert splitter.resolve("strategy.followup.cadence.A", account_id=2) is None   # paused
        splitter.start(exp.exp_id)
        assert splitter.resolve("cluster.hb.timeout_s", account_id=2) is None          # 非白名单


class TestValidation:
    """异常：分桶与 metric 合法性（40001）。"""

    def test_invalid_buckets_rejected(self) -> None:
        """非法分桶四态（空处理组/缺对照/账号重叠/双空）→ 40001。"""
        bad_list = [
            {"control": (1,), "treatment": ()},
            {"treatment": (1, 2)},
            {"control": (1,), "treatment": (1, 2)},
            {"control": (), "treatment": ()},
        ]
        for buckets in bad_list:
            with pytest.raises(ExperimentError) as ei:
                make_splitter().create("exp", "followup_reply_rate", buckets, {})
            assert ei.value.code == 40001

    def test_invalid_metric_and_unknown_account_rejected(self) -> None:
        """非法 metric → 40001；分桶外账号记录 → 40001（不入组不计数）。"""
        with pytest.raises(ExperimentError):
            make_splitter().create("exp", "made_up_metric", make_buckets(), {})
        splitter, exp_id = started_splitter()
        with pytest.raises(ExperimentError) as ei:
            splitter.record(exp_id, account_id=99, success=True)
        assert ei.value.code == 40001


class TestReportAndStats:
    """效果统计：分桶回复率 + 双比例 z 检验（§6.5 report）。"""

    def test_record_and_bucket_rates(self) -> None:
        """账号组分桶对比：control 0.8 vs treatment 0.2（各分级回复率报表口径）。"""
        splitter, exp_id = started_splitter()
        for acct, hits in ((1, 8), (2, 2)):
            for i in range(10):
                splitter.record(exp_id, account_id=acct, success=i < hits)
        report = splitter.report(exp_id)
        assert report["control"] == {"n": 10, "successes": 8, "rate": pytest.approx(0.8)}
        assert report["treatment"]["rate"] == pytest.approx(0.2)

    def test_significant_vs_continue_decision(self) -> None:
        """|z|≥1.96 且样本足 → significant；样本不足 → 继续观察（降级不出结论）。"""
        splitter, exp_id = started_splitter()
        for acct, hits in ((1, 9), (2, 1)):
            for i in range(10):
                splitter.record(exp_id, account_id=acct, success=i < hits)
        assert splitter.report(exp_id)["decision"] == "significant"
        small, small_id = started_splitter(min_sample=1000)
        small.record(small_id, account_id=1, success=True)
        small.record(small_id, account_id=2, success=False)
        assert small.report(small_id)["decision"] == "continue"

    def test_identical_rates_zero_z_not_significant(self) -> None:
        """边界：两组比例相同 → z≈0、不显著。"""
        splitter, exp_id = started_splitter()
        for acct in (1, 3, 5, 2, 4, 6):
            splitter.record(exp_id, account_id=acct, success=True)
            splitter.record(exp_id, account_id=acct, success=False)
        report = splitter.report(exp_id)
        assert report["z"] == pytest.approx(0.0, abs=1e-9)
        assert report["significant"] is False

    def test_record_dedup_per_conversation(self) -> None:
        """幂等：同会话键重复记录不重复计数（n 不虚增）。"""
        splitter, exp_id = started_splitter()
        splitter.record(exp_id, account_id=1, success=True, conversation_key="k1")
        splitter.record(exp_id, account_id=1, success=True, conversation_key="k1")
        assert splitter.report(exp_id)["control"]["n"] == 1
