# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""备份对象矩阵模型单元测试（NFR-REL-04，SR-NFR-REL-04 §2 矩阵 / §9 计划校验用例）。

覆盖：
- 默认矩阵与 SR §2 表逐行一致（四对象 / 方式 / 频率 / 保留 / 加密）；
- 保留期 <30 拒绝（构造期 pydantic 校验 + 计划级 validate_plan 双保险）；
- 恰好 30 天保留（验收下限边界放行）；
- 永久保留（retention_days=None）仅允许 OSS 素材行；
- kind 闭枚举与计划内 kind 唯一性。

实现方案：docs/07-实现方案/NFR/SR-NFR-REL-04-数据备份.md §2/§7。
"""
from __future__ import annotations

import pydantic
import pytest

from platform.core.backup import (
    MIN_RETENTION_DAYS,
    BackupPlan,
    BackupPlanError,
    BackupTarget,
    default_matrix,
    default_plan,
)

DAY = "每日"


class TestDefaultMatrix:
    """默认备份对象矩阵（与 SR §2 表逐行一致）。"""

    def test_matrix_rows_match_sr(self) -> None:
        plan = default_plan()
        kinds = [t.kind for t in plan.targets]
        assert kinds == ["pg", "milvus", "oss", "config"]  # 四对象齐备
        pg = plan.target_of("pg")
        assert pg is not None
        assert "pg_basebackup" in pg.method and "WAL" in pg.method  # 每日全量 + WAL 连续归档
        assert "每日全量" in pg.frequency and pg.retention_days == 30 and pg.encrypted
        assert "≤5min" in pg.rpo_note  # RPO 说明随矩阵行登记
        milvus = plan.target_of("milvus")
        assert milvus is not None
        assert "milvus-backup" in milvus.method and milvus.retention_days == 30
        oss = plan.target_of("oss")
        assert oss is not None
        assert "版本化" in oss.method and "跨区复制" in oss.method
        assert oss.permanent and oss.retention_days is None and oss.frequency == "持续"
        config = plan.target_of("config")
        assert config is not None
        assert "引用清单" in config.method and "密钥本体" in config.method  # 零密钥介质（决策③）

    def test_daily_kinds_exclude_oss(self) -> None:
        """OSS 素材由版本化+跨区复制持续承担，不进每日流水线。"""
        assert default_plan().daily_kinds == ("pg", "milvus", "config")

    def test_default_matrix_is_valid(self) -> None:
        """默认矩阵可重复构造且通过计划校验（幂等）。"""
        assert default_plan().validate_plan() is not None
        assert len(default_matrix()) == 4


class TestRetentionFloor:
    """保留期下限：任何具体值 <30 一律拒绝（SRS §11.2「保留 ≥30 天」）。"""

    def test_retention_below_floor_rejected(self) -> None:
        for bad in (0, 7, 29):
            with pytest.raises(pydantic.ValidationError) as ei:
                BackupTarget(kind="pg", method="m", frequency=DAY, retention_days=bad)
            assert "30" in str(ei.value)  # 错误信息含下限说明

    def test_retention_floor_boundary_kept(self) -> None:
        """恰好 30 天（含下限）放行——「近 30 天每日均有成功备份」口径的前提。"""
        target = BackupTarget(kind="milvus", method="milvus-backup → OSS", frequency=DAY, retention_days=30)
        assert target.retention_days == MIN_RETENTION_DAYS == 30
        BackupPlan(targets=(target,)).validate_plan()  # 计划级校验同样放行

    def test_plan_level_rejects_bypassed_target(self) -> None:
        """计划级双保险：model_construct 旁路构造的非法行被 validate_plan 拒绝。"""
        smuggled = BackupTarget.model_construct(
            kind="config", method="导出", frequency=DAY, retention_days=10,
            encrypted=True, rpo_note="",
        )
        plan = BackupPlan.model_construct(targets=(smuggled,))  # 双旁路：计划也不走构造校验
        with pytest.raises(BackupPlanError, match="30"):
            plan.validate_plan()


class TestPermanentRetention:
    """永久保留（None）仅允许 OSS 素材（知识资产不受 30 天约束）。"""

    def test_permanent_only_for_oss(self) -> None:
        for kind in ("pg", "milvus", "config"):
            with pytest.raises(pydantic.ValidationError, match="永久"):
                BackupTarget(kind=kind, method="m", frequency=DAY, retention_days=None)
        oss = BackupTarget(kind="oss", method="Bucket 版本化 + 跨区复制", frequency="持续", retention_days=None)
        assert oss.permanent

    def test_kind_is_closed_enum(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            BackupTarget(kind="redis", method="m", frequency=DAY, retention_days=30)


class TestPlanUniqueness:
    """计划内 kind 唯一（台账 (run_date, object) 唯一约束的前提）。"""

    def test_duplicate_kind_rejected(self) -> None:
        a = BackupTarget(kind="pg", method="m1", frequency=DAY, retention_days=30)
        b = BackupTarget(kind="pg", method="m2", frequency=DAY, retention_days=31)
        with pytest.raises(BackupPlanError, match="重复"):
            BackupPlan(targets=(a, b)).validate_plan()

    def test_target_of_missing_returns_none(self) -> None:
        plan = BackupPlan(targets=(BackupTarget(kind="pg", method="m", frequency=DAY, retention_days=30),))
        assert plan.target_of("milvus") is None
