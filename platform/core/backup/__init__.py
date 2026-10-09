# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""platform.core.backup——数据备份基座（NFR-REL-04，SR-NFR-REL-04 §3 代码落点）。

模块划分（对应 SR-NFR-REL-04 §3 services/backup-job/app/，本迭代以平台基座形态交付）：
- planner   备份对象矩阵模型：BackupTarget/BackupPlan + 默认矩阵（§2 表）+ 保留期 <30 拒绝
- pipeline  每日流水线编排：命令模板构造器（不执行）+ executor 注入协议 + 校验三件套
            + manifest 逐项 sha256 + 成功/失败告警钩子 + 保留期清理（§6.1-§6.3）

典型用法::

    from platform.core.backup import BackupRunner, BackupConfig, RecordingExecutor, default_plan

    runner = BackupRunner(
        default_plan(), RecordingExecutor(), config=BackupConfig(bucket="bkt", kms_id="k1")
    )
    manifest = runner.run(date(2026, 10, 9))          # 低峰窗口触发（窗口外可 force 补跑）
    print(manifest.summary())                          # 逐项 sha256 + 状态（审计 detail）
"""
from .pipeline import (
    AlertHook,
    BackupArtifact,
    BackupCommandBuilder,
    BackupConfig,
    BackupManifest,
    BackupRunner,
    CommandExecutor,
    CommandResult,
    LowPeakWindow,
    PurgeReport,
    RecordingAlertHook,
    RecordingExecutor,
    RunLedger,
    StoredBackup,
    StepFailure,
    VerifyCheck,
    VerifyResult,
    purge_expired,
    sha256_hex,
    verify_backup,
)
from .planner import (
    BACKUP_KIND_CN,
    MIN_RETENTION_DAYS,
    BackupKind,
    BackupPlan,
    BackupPlanError,
    BackupTarget,
    default_matrix,
    default_plan,
)

__all__ = [
    "AlertHook",
    "BackupArtifact",
    "BackupCommandBuilder",
    "BackupConfig",
    "BackupManifest",
    "BackupRunner",
    "CommandExecutor",
    "CommandResult",
    "LowPeakWindow",
    "PurgeReport",
    "RecordingAlertHook",
    "RecordingExecutor",
    "RunLedger",
    "StoredBackup",
    "StepFailure",
    "VerifyCheck",
    "VerifyResult",
    "purge_expired",
    "sha256_hex",
    "verify_backup",
    "BACKUP_KIND_CN",
    "MIN_RETENTION_DAYS",
    "BackupKind",
    "BackupPlan",
    "BackupPlanError",
    "BackupTarget",
    "default_matrix",
    "default_plan",
]
