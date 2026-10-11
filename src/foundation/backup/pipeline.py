# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""每日备份流水线编排（NFR-REL-04 数据备份，SR-NFR-REL-04 §6.1-§6.3）。

RPO 说明（SR §6.2，重点）：
    每日基础备份（pg_basebackup 全量）只决定「恢复基线新旧」，**不决定 RPO**；
    真实丢失窗口 = 故障时刻 − 最后一个已归档 WAL 事务 ≈ archive_timeout(5min) +
    上传时延(秒级) → **PG 实际 RPO ≤5 分钟（分钟级）**。误删/误改场景用 PITR
    （recovery_target_time 定到事发前一秒）→ 该场景 **RPO≈0**。Milvus 每日快照
    RPO=24h，但其权威源在 PG（knowledge_chunk 表），必要时经知识流水线重向量
    重建补齐到 PG 水位（BGE-M3 本地部署，无外部配额约束，NFR-SEC-06）。

流水线（低峰窗口触发 → 逐对象执行 → 校验 → 清单 manifest → 告警钩子 → 保留清理）：
    1. 低峰窗口（默认 02:00-05:00，Asia/Shanghai）：窗口外触发直接跳过（skipped_window）；
    2. 防重入：同日核心对象已全部 success 时，重复触发直接返回既有结果（不重复备份）；
    3. 逐对象执行：pg_basebackup → milvus-backup → 配置导出（OSS 素材由 Bucket
       版本化 + 跨区复制持续承担，不进每日流水线，SR §2 矩阵）；命令由
       BackupCommandBuilder 生成**模板**，执行经 CommandExecutor 协议注入——
       生产侧接线 subprocess 执行器，本模块不直接执行任何 shell 命令；
    4. 校验三件套：大小偏差（对昨日成功基线 ±size_tol）/ 条数快照可读 / 可读性抽查清单；
    5. 清单 manifest：逐对象 sha256 + 校验明细，失败对象在 manifest 标记；
    6. 成功/失败告警钩子：失败当日高级告警一次（连续 2 日失败升级在监控侧）；
    7. 保留期清理：过期对象清理 + 清理审计计数（WAL 比基础备份多保留 5 天）。

依据文档：docs/07-实现方案/NFR/SR-NFR-REL-04-数据备份.md §4/§6/§7；
         docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解）。
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal, Protocol

from .planner import BackupKind, BackupPlan, BackupTarget

# --------------------------------------------------------------------------- 执行器协议与基线桩


@dataclass(frozen=True)
class CommandResult:
    """一次命令执行结果（exit_code 语义对齐 shell：0=成功）。"""

    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """命令是否成功（exit_code == 0）。"""
        return self.exit_code == 0


class CommandExecutor(Protocol):
    """命令执行器协议：run(cmd) -> result（生产侧接线 subprocess；本模块只构造命令）。"""

    def run(self, cmd: str) -> CommandResult:
        """执行一条命令并返回结果。"""
        ...  # pragma: no cover


class RecordingExecutor:
    """记录型执行器（单元测试 / 单机演示桩）：记录全部命令，可按子串注入失败。

    - commands：按序记录已执行的命令（断言「其余步骤仍执行」的观测点）；
    - fail_on：命令含任一子串即返回失败（exit_code=1，stderr 给出原因）；
    - stdout_of：命令含某子串时返回定制 stdout（默认回显命令本身的模拟输出，
      作为「备份产物内容」参与校验与 manifest sha256 计算）。
    """

    def __init__(
        self,
        *,
        fail_on: Sequence[str] = (),
        stdout_of: Mapping[str, str] | None = None,
    ) -> None:
        self.fail_on = tuple(fail_on)
        self.stdout_of = dict(stdout_of or {})
        self.commands: list[str] = []

    def run(self, cmd: str) -> CommandResult:
        """执行（记录 + 可选失败注入）。"""
        self.commands.append(cmd)
        for token in self.fail_on:
            if token in cmd:
                return CommandResult(
                    command=cmd,
                    exit_code=1,
                    stderr=f"模拟执行失败：命中注入标记 {token!r}",
                )
        stdout = cmd  # 默认回显：产物内容 = 命令文本（sha256/大小/抽查均基于它）
        for token, text in self.stdout_of.items():
            if token in cmd:
                stdout = text
                break
        return CommandResult(command=cmd, exit_code=0, stdout=stdout)


def sha256_hex(content: str | bytes) -> str:
    """SHA-256 十六进制摘要（manifest 逐项校验和；str 按 UTF-8 编码）。"""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- 命令模板构造器（不执行）


@dataclass(frozen=True)
class BackupConfig:
    """备份流水线配置（app_config scope='backup' 的内存形态，SR §7；凭证一律注入不落码）。"""

    pg_host: str = "127.0.0.1"
    pg_user: str = "backup_user"
    pg_db: str = "aisale"
    milvus_addr: str = "127.0.0.1:19530"
    stage_dir: str = "/data/backup-stage"  # 本地暂存目录（上传 OSS 前落盘，水位保护见 §8）
    bucket: str = "{{BACKUP_BUCKET}}"  # 独立备份域 Bucket（与生产素材 Bucket 分离）
    kms_id: str = "{{KMS_ID}}"  # SSE-KMS 密钥 ID（占位符，secrets 注入）
    backup_prefix: str = "backup"
    size_tol: float = 0.30  # 大小偏差容忍（§7 backup.verify.size_tol）
    max_retry: int = 2  # 单步重试次数（§7 backup.max_retry）
    window_start: str = "02:00"  # 低峰窗口起（§7 backup.window_cron 02:30 起的全窗口）
    window_end: str = "05:00"  # 低峰窗口止（业务低峰 02:00-05:00）


class BackupCommandBuilder:
    """备份命令模板构造器：只生成命令字符串，**不执行**（执行经 CommandExecutor 注入）。

    模板与 SR §6.1/§6.2 一致：
    - pg：pg_basebackup 非独占模式 + spread checkpoint（对在线业务影响最小），
      -Xs 使备份自洽（含所需 WAL）；
    - milvus：milvus-backup 按集合备份（03 §4.1：单 collection knowledge_chunks）；
    - OSS：ossutil cp + --sse-kms 服务端加密（§8 备份安全性）；WAL 经
      archive_command 实时直传（%p=本地段路径，%f=段文件名，at-least-once）。
    """

    def __init__(self, config: BackupConfig) -> None:
        self.cfg = config

    # ── 对象键布局（SR §4 OSS 备份域布局）─────────────────────────────────

    def object_key(self, kind: BackupKind, run_date: date) -> str:
        """备份对象在 OSS 备份域的对象键（前缀/生命周期对齐 SR §4 布局表）。"""
        stamp = run_date.strftime("%Y%m%d")
        if kind == "pg":
            return f"{self.cfg.backup_prefix}/pg/base/{stamp}/base.tar"
        if kind == "milvus":
            return f"{self.cfg.backup_prefix}/milvus/{stamp}/backup_{stamp}"
        if kind == "config":
            return f"{self.cfg.backup_prefix}/config/{stamp}/config_export.csv"
        return f"{self.cfg.backup_prefix}/{kind}/{stamp}/"  # pragma: no cover - oss 不进每日流水线

    # ── 命令模板 ──────────────────────────────────────────────────────────

    def build(self, kind: BackupKind, run_date: date) -> str:
        """按对象类型生成当日备份命令模板（不执行）。"""
        if kind == "pg":
            return self.pg_basebackup(run_date)
        if kind == "milvus":
            return self.milvus_backup(run_date)
        if kind == "config":
            return self.config_export(run_date)
        raise ValueError(f"对象类型 {kind} 不进每日备份流水线（OSS 素材由版本化+跨区复制持续承担）")

    def pg_basebackup(self, run_date: date) -> str:
        """PG 每日全量命令模板：非独占 + spread checkpoint + -Xs 自洽（SR §6.1）。"""
        stage = f"{self.cfg.stage_dir}/pg/{run_date:%Y%m%d}"
        return (
            f"pg_basebackup -D {stage} -Fp -Xs -P --checkpoint=spread "
            f"-h {self.cfg.pg_host} -U {self.cfg.pg_user}"
        )

    def pg_wal_archive_command(self) -> str:
        """archive_command 模板（postgresql.conf 用）：WAL 段实时直传 OSS（SR §6.2）。

        %p=本地 WAL 段路径，%f=段文件名；ossutil 返回非 0 时 PG 原地重试，
        保证 WAL 不丢（at-least-once）——RPO 收敛到分钟级的关键。
        """
        return (
            f"ossutil cp %p oss://{self.cfg.bucket}/{self.cfg.backup_prefix}/pg/wal/%f "
            f"--sse-kms {self.cfg.kms_id}"
        )

    def milvus_backup(self, run_date: date) -> str:
        """Milvus 集合备份命令模板（备份根目录指向本地暂存，由上传步骤统一加密上 OSS）。"""
        name = f"backup_{run_date:%Y%m%d}"
        return (
            f"milvus-backup create -n {name} --milvus.address {self.cfg.milvus_addr} "
            f"--backup-root {self.cfg.stage_dir}/milvus"
        )

    def config_export(self, run_date: date) -> str:
        """配置逻辑导出命令模板：app_config 导出 + 密钥/KMS 引用清单（不含密钥本体，决策③）。

        密钥本体按 NFR-SEC-04 由 KMS 信封加密存于业务库，备份介质零密钥。
        """
        out = f"{self.cfg.stage_dir}/config/{run_date:%Y%m%d}/config_export.csv"
        return (
            f'psql -h {self.cfg.pg_host} -U {self.cfg.pg_user} -d {self.cfg.pg_db} '
            f'-Atc "COPY (SELECT scope, key, value, version FROM app_config ORDER BY 1, 2) '
            f'TO STDOUT WITH CSV HEADER" > {out}'
        )

    def oss_sync(self, src: str, dest_prefix: str) -> str:
        """OSS 同步命令模板：SSE-KMS 服务端加密 + 私有读（§6.3/§8）。"""
        return (
            f"ossutil cp -r {src} oss://{self.cfg.bucket}/{dest_prefix} "
            f"--sse-kms {self.cfg.kms_id}"
        )

    def upload_command(self, kind: BackupKind, run_date: date) -> str:
        """暂存目录 → OSS 备份域的上传命令模板（WAL 无需此步：archive_command 已实时在 OSS）。"""
        stamp = run_date.strftime("%Y%m%d")
        if kind == "pg":
            dest = f"{self.cfg.backup_prefix}/pg/base/{stamp}/"
            return self.oss_sync(f"{self.cfg.stage_dir}/pg/{stamp}", dest)
        if kind == "milvus":
            return self.oss_sync(f"{self.cfg.stage_dir}/milvus", f"{self.cfg.backup_prefix}/milvus/{stamp}/")
        dest = f"{self.cfg.backup_prefix}/config/{stamp}/"
        return self.oss_sync(f"{self.cfg.stage_dir}/config/{stamp}", dest)


# --------------------------------------------------------------------------- 校验 / 台账 / 告警（SR §6.3/§6.4）


@dataclass(frozen=True)
class VerifyCheck:
    """单项校验结果（名称 / 通过与否 / 明细）。"""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class VerifyResult:
    """校验三件套结果：大小偏差 / 条数快照 / 可读性抽查（§6.3「备份不可用必须今天发现」）。"""

    checks: tuple[VerifyCheck, ...]

    @property
    def ok(self) -> bool:
        """全部检查通过。"""
        return all(c.passed for c in self.checks)


def verify_backup(
    *,
    kind: str,
    content: str,
    baseline_bytes: int | None,
    size_tol: float = 0.30,
    row_counts: Mapping[str, int] | None = None,
    spot_check: Sequence[str] = (),
) -> VerifyResult:
    """校验一份备份产物（stub 语境产物 = 命令输出文本；生产为暂存目录逐文件校验）。

    - 大小偏差：对昨日成功基线 |size−baseline|/baseline > size_tol 判失败（疑似半截备份）；
      首日无基线时记「基线建立」通过；
    - 条数快照：row_counts 提供时须非空且值均为非负整数（备份时点核心表行数，
      行数级比对下沉到季度演练，§6.3 注 3）；未提供时记跳过；
    - 可读性抽查：spot_check 清单中每个文件名须出现在产物内容中（内容可读且完整）；
      空清单记跳过。
    """
    checks: list[VerifyCheck] = []
    size = len(content.encode("utf-8"))
    if baseline_bytes is None:
        checks.append(VerifyCheck("大小偏差", True, f"首日基线建立（{size} 字节）"))
    else:
        delta = abs(size - baseline_bytes) / baseline_bytes if baseline_bytes > 0 else 1.0
        checks.append(
            VerifyCheck(
                "大小偏差",
                delta <= size_tol,
                f"{size} 字节 vs 基线 {baseline_bytes} 字节，偏差 {delta:.1%}（容忍 {size_tol:.0%}）",
            )
        )
    if row_counts is None:
        checks.append(VerifyCheck("条数快照", True, "跳过（未配置行数快照提供器，比对下沉季度演练）"))
    else:
        ok_counts = bool(row_counts) and all(isinstance(v, int) and v >= 0 for v in row_counts.values())
        checks.append(
            VerifyCheck("条数快照", ok_counts, f"{kind} 核心表行数快照可读：{dict(row_counts)!r}"[:200])
        )
    if not spot_check:
        checks.append(VerifyCheck("可读性抽查", True, "跳过（无抽查清单）"))
    else:
        missing = [name for name in spot_check if name not in content]
        checks.append(
            VerifyCheck(
                "可读性抽查",
                not missing,
                f"抽查 {len(spot_check)} 项" + (f"，缺失：{missing}" if missing else "，全部可读"),
            )
        )
    return VerifyResult(checks=tuple(checks))


@dataclass
class RunLedger:
    """备份任务台账（backup_job_run 的内存形态）：(run_date, object) → status。

    防重入与成功率监控的数据来源；object 取值：pg_base / milvus / config
    （pg_wal 为 archive_command 实时流，不走每日台账）。
    """

    records: dict[tuple[date, str], str] = field(default_factory=dict)

    def start(self, run_date: date, obj: str) -> None:
        """步骤开始（status=running）。"""
        self.records[(run_date, obj)] = "running"

    def finish(self, run_date: date, obj: str, status: str) -> None:
        """步骤结束（success / failed / skipped）。"""
        self.records[(run_date, obj)] = status

    def status_of(self, run_date: date, obj: str) -> str | None:
        """查询某日某对象状态（无记录返回 None）。"""
        return self.records.get((run_date, obj))

    def all_success(self, run_date: date, objs: Sequence[str]) -> bool:
        """防重入判定：指定对象当日是否已全部 success。"""
        return all(self.status_of(run_date, o) == "success" for o in objs)


class AlertHook(Protocol):
    """告警钩子协议：P2 起走 alert-service POST /api/v1/alerts，P0-P1 直连运维钉钉 webhook。"""

    def send(self, level: str, type: str, title: str, body: str) -> None:
        """发送一条告警（level: high/mid/info）。"""
        ...  # pragma: no cover


@dataclass
class RecordingAlertHook:
    """记录型告警钩子（测试观测用：验证「失败当日高级告警恰一次」）。"""

    events: list[dict[str, str]] = field(default_factory=list)

    def send(self, level: str, type: str, title: str, body: str) -> None:
        """记录告警事件。"""
        self.events.append({"level": level, "type": type, "title": title, "body": body})


# --------------------------------------------------------------------------- 低峰窗口


@dataclass(frozen=True)
class LowPeakWindow:
    """低峰执行窗口（默认 02:00-05:00，时区 Asia/Shanghai；§7 backup.window_minutes 配套）。"""

    start: str = "02:00"
    end: str = "05:00"

    def contains(self, moment: time) -> bool:
        """时刻是否落在窗口内（含起点、不含终点）。"""
        h1, m1 = map(int, self.start.split(":"))
        h2, m2 = map(int, self.end.split(":"))
        begin = time(h1, m1)
        finish = time(h2, m2)
        return begin <= moment < finish


# --------------------------------------------------------------------------- manifest 与流水线


@dataclass(frozen=True)
class BackupArtifact:
    """单个备份对象的成功产物（manifest 逐项：对象键 + 大小 + sha256 + 校验明细）。"""

    kind: str
    object_key: str
    size_bytes: int
    sha256: str
    command: str
    checks: tuple[VerifyCheck, ...]


@dataclass(frozen=True)
class StepFailure:
    """单个备份对象的失败记录（manifest 标记 + 台账 failed + 告警正文素材）。"""

    kind: str
    command: str
    reason: str
    attempts: int


ManifestStatus = Literal["success", "failed", "skipped", "skipped_window"]
"""manifest 状态：success 全部成功 / failed 部分或全部失败 / skipped 防重入跳过 / skipped_window 窗口外跳过。"""


@dataclass
class BackupManifest:
    """备份清单 manifest：逐项 sha256 + 失败标记（交付与演练比对的锚点，SR §6.1）。"""

    run_date: date
    status: ManifestStatus
    artifacts: tuple[BackupArtifact, ...] = ()
    failures: tuple[StepFailure, ...] = ()
    window: str = ""  # 执行窗口说明（低峰窗口或窗口外跳过原因）
    note: str = ""  # 备注（防重入跳过 / OSS 持续机制说明等）

    @property
    def ok(self) -> bool:
        """本次运行是否整体成功。"""
        return self.status == "success"

    def artifact_of(self, kind: str) -> BackupArtifact | None:
        """按对象类型取产物（缺失返回 None）。"""
        for a in self.artifacts:
            if a.kind == kind:
                return a
        return None

    def summary(self) -> dict[str, Any]:
        """审计摘要（动作入审计日志哈希链的 detail 形态，NFR-SEC-03）。"""
        return {
            "run_date": self.run_date.isoformat(),
            "status": self.status,
            "objects": [
                {"kind": a.kind, "object_key": a.object_key, "sha256": a.sha256, "size_bytes": a.size_bytes}
                for a in self.artifacts
            ],
            "failures": [{"kind": f.kind, "reason": f.reason} for f in self.failures],
        }


class BackupReentryError(RuntimeError):
    """备份流水线并发防重入拒绝（2026-10-10 TDD 审计缺陷修复：check-then-act 窗口收口）。"""


class BackupRunner:
    """每日备份流水线编排器（executor 注入；命令只构造不执行）。

    编排：低峰窗口判定 → 防重入 → 逐对象（备份命令 + 上传命令）→ 校验三件套 →
    manifest（逐项 sha256）→ 成功/失败告警钩子。历史 manifest 兼作次日大小校验基线。
    """

    def __init__(
        self,
        plan: BackupPlan,
        executor: CommandExecutor,
        *,
        config: BackupConfig | None = None,
        builder: BackupCommandBuilder | None = None,
        alerter: AlertHook | None = None,
        ledger: RunLedger | None = None,
        history: Sequence[BackupManifest] = (),
        row_counts_provider: Callable[[], dict[str, int]] | None = None,
        spot_checks: Mapping[str, Sequence[str]] | None = None,
        notify_on_success: bool = False,
    ) -> None:
        self.plan = plan.validate_plan()
        self.executor = executor
        self.config = config or BackupConfig()
        self.builder = builder or BackupCommandBuilder(self.config)
        self.alerter = alerter
        self.ledger = ledger if ledger is not None else RunLedger()
        self.history: list[BackupManifest] = list(history)
        self.row_counts_provider = row_counts_provider  # PG 条数快照提供器（演练比对基线）
        self.spot_checks = dict(spot_checks or {})
        self.notify_on_success = notify_on_success  # §7 backup.alert.on_success 默认 false
        self.window = LowPeakWindow(self.config.window_start, self.config.window_end)

    # ── 基线（昨日成功产物大小，§6.3 大小偏差校验参照）────────────────────

    def _baseline(self, kind: str) -> int | None:
        """从历史 manifest 取该对象最近一次成功产物的大小（无则 None=首日基线建立）。"""
        for manifest in reversed(self.history):
            if manifest.status != "success":
                continue
            artifact = manifest.artifact_of(kind)
            if artifact is not None:
                return artifact.size_bytes
        return None

    # ── 主流程 ────────────────────────────────────────────────────────────

    def run(self, run_date: date, *, now: datetime | None = None, force: bool = False) -> BackupManifest:
        """执行当日流水线（进程内防重入锁：并发双触发恰一执行，其余拒绝）。"""
        lock = getattr(self, "_run_lock", None)
        if lock is None:  # 惰性初始化：兼容未走 __init__ 的既有测试桩
            lock = threading.Lock()
            self._run_lock = lock
        if not lock.acquire(blocking=False):
            raise BackupReentryError(
                f"备份任务已在执行中（run_date={run_date}），请勿并发触发；如需人工补跑请等待当前任务结束"
            )
        try:
            return self._run_unlocked(run_date, now=now, force=force)
        finally:
            lock.release()

    def _run_unlocked(self, run_date: date, *, now: datetime | None = None, force: bool = False) -> BackupManifest:
        """执行当日流水线原逻辑（run 持锁后进入；now 提供且在低峰窗口外时跳过；force 用于人工补跑绕过窗口）。"""
        window_label = f"低峰窗口 {self.window.start}-{self.window.end}（Asia/Shanghai）"
        if now is not None and not force and not self.window.contains(now.time()):
            skipped = BackupManifest(
                run_date=run_date,
                status="skipped_window",
                window=window_label,
                note=f"触发时刻 {now.time():%H:%M} 在低峰窗口外，跳过（人工补跑走 force=True）",
            )
            self.history.append(skipped)
            return skipped

        daily_kinds = self.plan.daily_kinds
        # 防重入：同日核心对象已全部 success → 直接返回既有结果（SR §9 流水线_防重入）
        if self.ledger.all_success(run_date, [self._object_name(k) for k in daily_kinds]):
            for manifest in reversed(self.history):
                if manifest.run_date == run_date and manifest.status == "success":
                    return manifest
            return BackupManifest(
                run_date=run_date, status="skipped", window=window_label, note="同日已成功，防重入跳过"
            )

        artifacts: list[BackupArtifact] = []
        failures: list[StepFailure] = []
        for kind in daily_kinds:
            artifact, failure = self._backup_object(kind, run_date)
            if artifact is not None:
                artifacts.append(artifact)
            if failure is not None:
                failures.append(failure)

        status: ManifestStatus = "failed" if failures else "success"
        manifest = BackupManifest(
            run_date=run_date,
            status=status,
            artifacts=tuple(artifacts),
            failures=tuple(failures),
            window=window_label,
            note="OSS 素材由 Bucket 版本化 + 跨区复制持续承担，不进每日流水线（SR §2 矩阵）",
        )
        self.history.append(manifest)

        # 成功/失败告警钩子：失败当日高级告警恰一次（连续 2 日失败升级在监控侧，§6.4）
        if self.alerter is not None:
            if status == "failed":
                digest = "; ".join(f"{f.kind}: {f.reason}" for f in failures)
                self.alerter.send(
                    level="high",
                    type="backup_failed",
                    title=f"每日备份失败 {run_date}",
                    body=digest,
                )
            elif self.notify_on_success:
                self.alerter.send(
                    level="info",
                    type="backup_success",
                    title=f"每日备份成功 {run_date}",
                    body=f"对象 {len(artifacts)} 项，manifest 逐项 sha256 已登记",
                )
        return manifest

    def _object_name(self, kind: BackupKind) -> str:
        """台账对象名（pg → pg_base，对齐 backup_job_run.object 枚举）。"""
        return "pg_base" if kind == "pg" else kind

    def _backup_object(
        self, kind: BackupKind, run_date: date
    ) -> tuple[BackupArtifact | None, StepFailure | None]:
        """单对象步骤：备份命令 → 上传命令（均经 executor）→ 校验三件套。

        失败重试至多 max_retry 次（指数退避在 subprocess 执行器侧实现）；
        最终失败返回 StepFailure（互不依赖的后续对象仍会执行——多备一分是一分）。
        """
        obj = self._object_name(kind)
        target: BackupTarget | None = self.plan.target_of(kind)
        baseline = self._baseline(self._object_name(kind))
        row_counts: dict[str, int] | None = None
        if kind == "pg" and self.row_counts_provider is not None:
            row_counts = self.row_counts_provider()
        spot_check = self.spot_checks.get(obj, ())

        last_reason = ""
        last_command = ""
        for attempt in range(self.config.max_retry + 1):
            self.ledger.start(run_date, obj)
            commands = [
                self.builder.build(kind, run_date),
                self.builder.upload_command(kind, run_date),
            ]
            for cmd in commands:
                result = self.executor.run(cmd)
                last_command = cmd
                if not result.ok:
                    last_reason = f"命令失败（exit={result.exit_code}）：{result.stderr[:120]}"
                    break
            else:
                content = result.stdout  # 产物内容（stub 语境 = 最终上传命令输出）
                verified = verify_backup(
                    kind=obj,
                    content=content,
                    baseline_bytes=baseline,
                    size_tol=self.config.size_tol,
                    row_counts=row_counts,
                    spot_check=spot_check,
                )
                if verified.ok:
                    self.ledger.finish(run_date, obj, "success")
                    return (
                        BackupArtifact(
                            kind=obj,
                            object_key=self.builder.object_key(kind, run_date),
                            size_bytes=len(content.encode("utf-8")),
                            sha256=sha256_hex(content),
                            command=last_command,
                            checks=verified.checks,
                        ),
                        None,
                    )
                last_reason = "；".join(f"{c.name}未过（{c.detail}）" for c in verified.checks if not c.passed)
            self.ledger.finish(run_date, obj, "failed")
        failure = StepFailure(
            kind=obj, command=last_command, reason=last_reason,
            attempts=self.config.max_retry + 1,
        )
        return None, failure


# --------------------------------------------------------------------------- 保留期清理（SR §6.3）


@dataclass(frozen=True)
class StoredBackup:
    """已存储的备份对象（GC 扫描输入；OSS 素材永久保留、不进 GC 输入）。"""

    object_key: str
    kind: str  # pg_base | pg_wal | milvus | config（对齐 SR §4 布局）
    run_date: date


@dataclass(frozen=True)
class PurgeReport:
    """保留清理报告（清理审计计数：分类保留/清理数量与明细）。"""

    retention_days: int
    wal_retention_days: int
    today: date
    kept: tuple[StoredBackup, ...]
    purged: tuple[StoredBackup, ...]

    @property
    def purged_count(self) -> int:
        """清理对象总数。"""
        return len(self.purged)

    @property
    def kept_count(self) -> int:
        """保留对象总数。"""
        return len(self.kept)

    def audit_summary(self) -> dict[str, int]:
        """清理审计计数（按对象类型分类的保留/清理数量，入审计日志）。"""
        summary: dict[str, int] = {}
        for item in self.kept:
            summary[f"kept.{item.kind}"] = summary.get(f"kept.{item.kind}", 0) + 1
        for item in self.purged:
            summary[f"purged.{item.kind}"] = summary.get(f"purged.{item.kind}", 0) + 1
        return summary


def purge_expired(
    stored: Sequence[StoredBackup],
    *,
    retention_days: int,
    today: date,
    wal_retention_days: int | None = None,
) -> PurgeReport:
    """保留期清理：过期对象清理 + 审计计数（OSS 生命周期为主责，应用层双保险）。

    过期判定（边界对齐 SR §9 GC_边界）：age = (today − run_date).days；
    - pg_base / milvus / config：age > retention_days 判过期（恰好 30 天**保留**，
      31 天过期——保证「近 30 天每日均有成功备份」的连续性口径不被清理破坏）；
    - pg_wal：age > wal_retention_days（默认 retention+5，比基础备份多 5 天，
      保证任意基线可回放，SR §4/§7）。
    """
    wal_limit = wal_retention_days if wal_retention_days is not None else retention_days + 5
    kept: list[StoredBackup] = []
    purged: list[StoredBackup] = []
    for item in stored:
        age = (today - item.run_date).days
        limit = wal_limit if item.kind == "pg_wal" else retention_days
        (purged if age > limit else kept).append(item)
    return PurgeReport(
        retention_days=retention_days,
        wal_retention_days=wal_limit,
        today=today,
        kept=tuple(kept),
        purged=tuple(purged),
    )
