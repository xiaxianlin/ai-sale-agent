# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""备份对象矩阵模型（NFR-REL-04 数据备份，SR-NFR-REL-04 §2 备份对象矩阵 / §7 配置）。

备份对象矩阵（与 03-数据库设计 §6 逐行一致，本模块为其可执行模型）：

| 对象 kind | 权威性       | 方式                                   | 频率                | 保留               |
|-----------|--------------|----------------------------------------|-------------------|------------------|
| pg        | 唯一权威源   | pg_basebackup 每日全量 + WAL 连续归档   | 每日全量 + 实时 WAL | ≥30 天             |
| milvus    | 可由 PG 重建 | milvus-backup → OSS                    | 每日                | ≥30 天             |
| oss       | 原始权威资产 | Bucket 版本化 + 跨区复制                | 持续                | 永久（不受30天约束）|
| config    | —            | app_config 逻辑导出 + 密钥/KMS 引用清单 | 每日                | ≥30 天             |

硬约束：任何**具体**保留天数 <30 一律拒绝（SRS §11.2「保留 ≥30 天」验收下限）；
「永久保留」（retention_days=None）仅允许 OSS 素材行（知识资产口径）。

依据文档：docs/07-实现方案/NFR/SR-NFR-REL-04-数据备份.md §2/§7；
         docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BackupKind = Literal["pg", "milvus", "oss", "config"]
"""备份对象四分类（SR §4 backup_job_run.object 的闭枚举：pg_base/pg_wal 归并 pg 维度）。"""

MIN_RETENTION_DAYS: int = 30
"""保留期下限（SRS §11.2 NFR-REL-04：保留 ≥30 天；§7 backup.retention_days 取值下限）。"""

BACKUP_KIND_CN: dict[str, str] = {
    "pg": "PostgreSQL（业务库全量）",
    "milvus": "Milvus（knowledge_chunks 集合）",
    "oss": "OSS 素材（知识原始文件）",
    "config": "配置与密钥引用",
}


class BackupPlanError(ValueError):
    """备份计划校验失败（保留期 <30 天 / kind 重复 / 永久保留用于非 OSS 对象等）。"""


class BackupTarget(BaseModel):
    """单个备份对象的矩阵行：kind + 方式 + 频率 + 保留期 + 加密标记。

    - retention_days：None 表示永久保留（仅 OSS 素材允许，知识资产不受 30 天约束）；
      任何具体值必须 ≥30（MIN_RETENTION_DAYS），否则构造期即拒绝；
    - encrypted：备份介质加密（SSE-KMS 服务端加密，SR §8 备份安全性），默认开启。
    """

    model_config = ConfigDict(frozen=True)

    kind: BackupKind
    method: str = Field(description="执行方式（命令模板/机制说明，与 SR §2 矩阵「方式」列一致）")
    frequency: str = Field(description="备份频率：每日全量 / 每日 / 持续")
    retention_days: int | None = Field(
        default=None,
        description="保留天数；None=永久（仅 OSS）；具体值必须 ≥30",
    )
    encrypted: bool = True
    rpo_note: str = Field(default="", description="实际 RPO 说明（SR §1 验收口径表 RPO 列）")

    @model_validator(mode="after")
    def _check_retention(self) -> "BackupTarget":
        """保留期硬校验：具体值 ≥30；永久（None）仅限 OSS 素材行。"""
        if self.retention_days is None:
            if self.kind != "oss":
                raise BackupPlanError(
                    f"备份对象 {self.kind} 的保留期不得为永久（仅 OSS 素材允许永久保留）"
                )
            return self
        if self.retention_days < MIN_RETENTION_DAYS:
            raise BackupPlanError(
                f"备份对象 {self.kind} 保留期 {self.retention_days} 天低于下限 "
                f"{MIN_RETENTION_DAYS} 天（NFR-REL-04：保留 ≥30 天，拒绝）"
            )
        return self

    @property
    def kind_cn(self) -> str:
        """对象中文名（运维页/报告展示用）。"""
        return BACKUP_KIND_CN.get(self.kind, self.kind)

    @property
    def permanent(self) -> bool:
        """是否永久保留（知识资产口径，不参与保留期清理）。"""
        return self.retention_days is None


class BackupPlan(BaseModel):
    """备份计划：对象矩阵全集（每日流水线与保留清理的消费方，SR §6.1/§6.3）。"""

    model_config = ConfigDict(frozen=True)

    targets: tuple[BackupTarget, ...] = Field(default_factory=tuple)
    wal_retention_extra_days: int = 5
    """WAL 比基础备份多保留的天数（SR §4/§7：保证任意基线可回放，默认 +5 → 35 天）。"""

    def validate_plan(self) -> "BackupPlan":
        """计划级校验（保留期 <30 拒绝 + kind 唯一），返回自身便于链式调用。

        - 每行保留期校验在 BackupTarget 构造期已强制，此处二次显式校验
          （防止经 model_construct 等旁路构造的非法行混入流水线）；
        - kind 唯一：同一对象出现两行会使台账 (run_date, object) 唯一约束失义。
        """
        kinds: list[str] = []
        for t in self.targets:
            if t.retention_days is not None and t.retention_days < MIN_RETENTION_DAYS:
                raise BackupPlanError(
                    f"备份对象 {t.kind} 保留期 {t.retention_days} 天 < {MIN_RETENTION_DAYS} 天，"
                    "计划校验拒绝（NFR-REL-04 保留 ≥30 天）"
                )
            if t.kind in kinds:
                raise BackupPlanError(f"备份对象 kind 重复：{t.kind}（台账 (run_date, object) 唯一约束要求）")
            kinds.append(t.kind)
        return self

    def target_of(self, kind: BackupKind) -> BackupTarget | None:
        """按 kind 取矩阵行（缺失返回 None）。"""
        for t in self.targets:
            if t.kind == kind:
                return t
        return None

    @property
    def daily_kinds(self) -> tuple[BackupKind, ...]:
        """进入每日流水线的对象（OSS 素材由版本化+跨区复制持续承担，SR §2 矩阵）。"""
        return tuple(t.kind for t in self.targets if t.kind != "oss")  # type: ignore[misc]


def default_matrix() -> tuple[BackupTarget, ...]:
    """默认备份对象矩阵（与 SR-NFR-REL-04 §2 表逐行一致）。"""
    return (
        BackupTarget(
            kind="pg",
            method="pg_basebackup 每日全量 + WAL 连续归档（archive_command 直传 OSS）",
            frequency="每日全量（低峰窗口）+ 实时 WAL",
            retention_days=30,
            encrypted=True,
            rpo_note="≤5min（WAL 连续归档；误删场景 PITR 可达 ≈0）",
        ),
        BackupTarget(
            kind="milvus",
            method="milvus-backup → OSS",
            frequency="每日",
            retention_days=30,
            encrypted=True,
            rpo_note="24h（兜底可由 PG 权威源经知识流水线重建补齐）",
        ),
        BackupTarget(
            kind="oss",
            method="Bucket 版本化 + 跨区复制",
            frequency="持续",
            retention_days=None,
            encrypted=True,
            rpo_note="≈0（版本化对象 + 跨区副本）",
        ),
        BackupTarget(
            kind="config",
            method="app_config 逻辑导出 + secrets/KMS 引用清单（不含密钥本体）",
            frequency="每日",
            retention_days=30,
            encrypted=True,
            rpo_note="24h",
        ),
    )


def default_plan() -> BackupPlan:
    """默认备份计划（默认矩阵 + WAL 多保留 5 天），构造即通过校验。"""
    return BackupPlan(targets=default_matrix()).validate_plan()
