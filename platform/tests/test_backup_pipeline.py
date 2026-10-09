# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""备份流水线单元测试（NFR-REL-04，SR-NFR-REL-04 §6.1-§6.3 / §9 用例表的桩执行器级验证）。

覆盖：
- 命令模板生成正确性（pg_basebackup / milvus-backup / OSS 同步 / WAL archive_command）；
- 流水线成功路径：逐对象执行 + manifest 逐项 sha256 + 台账 success；
- 部分失败：executor 桩注入 milvus 失败 → 告警恰一次 + manifest 标记 + 其余步骤仍执行；
- 校验三件套：大小偏差（缩水备份检出）/ 条数快照 / 可读性抽查清单；
- 防重入：同日已 success 直接跳过、无重复备份；
- 低峰窗口：窗口外跳过、窗口内执行；
- 保留清理边界：恰好 30 天保留、31 天过期；WAL 多保留 5 天。

实现方案：docs/07-实现方案/NFR/SR-NFR-REL-04-数据备份.md §6/§9。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time

import pytest

from platform.core.backup import (
    BackupCommandBuilder,
    BackupConfig,
    BackupRunner,
    LowPeakWindow,
    PurgeReport,
    RecordingAlertHook,
    RecordingExecutor,
    RunLedger,
    StoredBackup,
    default_plan,
    purge_expired,
    sha256_hex,
    verify_backup,
)

DAY1 = date(2026, 10, 9)
DAY2 = date(2026, 10, 10)


def _runner(executor: RecordingExecutor, **kwargs: object) -> BackupRunner:
    """组装默认测试流水线（默认矩阵 + 记录型桩执行器）。"""
    return BackupRunner(default_plan(), executor, **kwargs)  # type: ignore[arg-type]


class TestCommandTemplates:
    """命令模板构造器：只生成不执行（SR §6.1/§6.2 模板口径）。"""

    def test_pg_basebackup_template(self) -> None:
        cfg = BackupConfig(pg_host="10.0.0.2", pg_user="backup_user", stage_dir="/stage")
        cmd = BackupCommandBuilder(cfg).pg_basebackup(DAY1)
        assert cmd.startswith("pg_basebackup ")
        assert "-Xs" in cmd and "--checkpoint=spread" in cmd  # 自洽 + 对在线业务影响最小
        assert "-h 10.0.0.2" in cmd and "-U backup_user" in cmd
        assert "/stage/pg/20261009" in cmd  # 暂存目录按日期分卷

    def test_milvus_backup_template(self) -> None:
        cmd = BackupCommandBuilder(BackupConfig(milvus_addr="10.0.0.3:19530")).milvus_backup(DAY1)
        assert cmd.startswith("milvus-backup create -n backup_20261009 ")
        assert "--milvus.address 10.0.0.3:19530" in cmd

    def test_oss_sync_template_encrypted(self) -> None:
        builder = BackupCommandBuilder(BackupConfig(bucket="bkt-bak", kms_id="kms-1"))
        cmd = builder.oss_sync("/stage/pg/20261009", "backup/pg/base/20261009/")
        assert cmd.startswith("ossutil cp -r ")
        assert "oss://bkt-bak/backup/pg/base/20261009/" in cmd
        assert "--sse-kms kms-1" in cmd  # SSE-KMS 服务端加密（§8）

    def test_wal_archive_command_template(self) -> None:
        """archive_command 模板：%p/%f 占位 + WAL 前缀 + SSE-KMS（RPO 分钟级的关键）。"""
        cmd = BackupCommandBuilder(BackupConfig(bucket="bkt-bak", kms_id="kms-1")).pg_wal_archive_command()
        assert "ossutil cp %p" in cmd and "%f" in cmd
        assert "oss://bkt-bak/backup/pg/wal/%f" in cmd
        assert "--sse-kms kms-1" in cmd

    def test_object_key_layout(self) -> None:
        """OSS 备份域对象键布局（SR §4 布局表）。"""
        b = BackupCommandBuilder(BackupConfig())
        assert b.object_key("pg", DAY1) == "backup/pg/base/20261009/base.tar"
        assert b.object_key("milvus", DAY1) == "backup/milvus/20261009/backup_20261009"
        assert b.object_key("config", DAY1) == "backup/config/20261009/config_export.csv"

    def test_oss_kind_not_in_daily_pipeline(self) -> None:
        with pytest.raises(ValueError, match="版本化"):
            BackupCommandBuilder(BackupConfig()).build("oss", DAY1)

    def test_config_export_no_secrets(self) -> None:
        """配置导出模板只导 app_config 逻辑表，不含密钥本体（零密钥介质，决策③）。"""
        cmd = BackupCommandBuilder(BackupConfig()).config_export(DAY1)
        assert "app_config" in cmd and "secret" not in cmd.lower()


class TestPipelineSuccess:
    """流水线成功路径（SR §9 流水线成功 / manifest 逐项 sha256）。"""

    def test_success_manifest_and_sha256(self) -> None:
        executor = RecordingExecutor()
        runner = _runner(executor, config=BackupConfig(bucket="bkt", kms_id="k1"))
        manifest = runner.run(DAY1)
        assert manifest.status == "success" and manifest.failures == ()
        # 三对象逐项登记，sha256 与产物内容（上传命令输出）逐一对应
        assert [a.kind for a in manifest.artifacts] == ["pg_base", "milvus", "config"]
        builder = BackupCommandBuilder(BackupConfig(bucket="bkt", kms_id="k1"))
        for kind in ("pg", "milvus", "config"):
            artifact = manifest.artifact_of("pg_base" if kind == "pg" else kind)  # type: ignore[arg-type]
            assert artifact is not None
            upload_cmd = builder.upload_command(kind, DAY1)  # type: ignore[arg-type]
            assert artifact.command == upload_cmd
            assert artifact.sha256 == hashlib.sha256(upload_cmd.encode("utf-8")).hexdigest()
            assert artifact.size_bytes == len(upload_cmd.encode("utf-8"))
        # 每对象两条命令（备份 + 上传），共 3×2
        assert len(executor.commands) == 6
        assert any("pg_basebackup" in c for c in executor.commands)
        assert any("milvus-backup" in c for c in executor.commands)
        assert any("psql" in c for c in executor.commands)
        # 台账全部 success（成功率监控数据源）
        assert all(
            runner.ledger.status_of(DAY1, o) == "success" for o in ("pg_base", "milvus", "config")
        )
        # 成功默认不告警（§7 backup.alert.on_success=false）
        hook = RecordingAlertHook()
        _runner(RecordingExecutor(), alerter=hook).run(DAY1)
        assert hook.events == []


class TestPipelinePartialFailure:
    """部分失败：告警 + manifest 标记 + 其余步骤仍执行（SR §6.1 设计要点②）。"""

    def test_milvus_failure_alerts_once_and_others_run(self) -> None:
        executor = RecordingExecutor(fail_on=["milvus-backup"])
        hook = RecordingAlertHook()
        runner = _runner(executor, alerter=hook, config=BackupConfig(max_retry=2))
        manifest = runner.run(DAY1)
        assert manifest.status == "failed"
        assert [f.kind for f in manifest.failures] == ["milvus"]
        assert manifest.failures[0].attempts == 3  # max_retry=2 → 共 3 次尝试
        # 其余互不依赖步骤仍执行：pg 与 config 成功入 manifest
        assert [a.kind for a in manifest.artifacts] == ["pg_base", "config"]
        # 失败对象重试 3 次（每次只到失败命令即止），其余对象各 2 条命令
        milvus_cmds = [c for c in executor.commands if "milvus-backup" in c]
        assert len(milvus_cmds) == 3
        assert any("pg_basebackup" in c for c in executor.commands)
        assert any("psql" in c for c in executor.commands)
        # 高级告警恰一次，type=backup_failed，正文含失败对象
        assert len(hook.events) == 1
        assert hook.events[0]["level"] == "high" and hook.events[0]["type"] == "backup_failed"
        assert "milvus" in hook.events[0]["body"]
        # 台账：milvus failed，其余 success
        assert runner.ledger.status_of(DAY1, "milvus") == "failed"
        assert runner.ledger.status_of(DAY1, "pg_base") == "success"

    def test_no_retry_single_attempt(self) -> None:
        """max_retry=0 时单次失败即入 failures（不重试；重试语义见上例 attempts=3）。"""
        executor = RecordingExecutor(fail_on=["milvus-backup"])
        runner = _runner(executor, config=BackupConfig(max_retry=0))
        manifest = runner.run(DAY1)
        assert manifest.status == "failed"
        assert len([c for c in executor.commands if "milvus-backup" in c]) == 1  # 不重试


class TestVerification:
    """校验三件套：大小偏差 / 条数快照 / 可读性抽查（SR §6.3）。"""

    def test_size_delta_shrink_detected(self) -> None:
        """对昨日成功基线缩水 >30% 判失败（疑似半截备份，SR §9 校验_大小偏差）。"""
        executor_ok = RecordingExecutor()
        runner = _runner(executor_ok)
        assert runner.run(DAY1).status == "success"  # 建立基线
        # 次日产物内容被桩缩水到极小（模拟截断备份）
        shrunk = RecordingExecutor(stdout_of={"ossutil": "x"})
        runner2 = _runner(shrunk, history=runner.history)
        manifest = runner2.run(DAY2)
        assert manifest.status == "failed"
        assert [f.kind for f in manifest.failures] == ["pg_base", "milvus", "config"]
        assert any("大小偏差" in f.reason for f in manifest.failures)

    def test_verify_units(self) -> None:
        # 首日无基线：大小检查记「基线建立」通过
        first = verify_backup(kind="pg_base", content="abc", baseline_bytes=None)
        assert first.ok
        # 偏差在容忍内通过
        ok = verify_backup(kind="pg_base", content="a" * 80, baseline_bytes=100, size_tol=0.30)
        assert ok.ok
        # 缩水 50% 失败
        bad = verify_backup(kind="pg_base", content="a" * 50, baseline_bytes=100, size_tol=0.30)
        assert not bad.ok and bad.checks[0].name == "大小偏差"
        # 条数快照：负数行数判失败
        neg = verify_backup(kind="pg_base", content="x", baseline_bytes=None, row_counts={"t": -1})
        assert not neg.ok
        valid = verify_backup(kind="pg_base", content="x", baseline_bytes=None, row_counts={"t": 3})
        assert valid.ok
        # 可读性抽查：清单中的文件名未出现在产物内容 → 失败
        miss = verify_backup(kind="pg_base", content="只有一段输出", baseline_bytes=None, spot_check=["base.tar"])
        assert not miss.ok
        hit = verify_backup(
            kind="pg_base", content="base.tar OK", baseline_bytes=None, spot_check=["base.tar"]
        )
        assert hit.ok

    def test_spot_check_via_runner(self) -> None:
        """抽查清单经 runner 配置接入：产物内容缺清单项 → 该对象失败。"""
        executor = RecordingExecutor(stdout_of={"ossutil cp -r /data/backup-stage/pg": "不完整输出"})
        runner = _runner(executor, spot_checks={"pg_base": ["base.tar"]})
        manifest = runner.run(DAY1)
        assert manifest.status == "failed"
        assert "可读性抽查" in manifest.failures[0].reason
        # milvus/config 未配抽查清单 → 仍成功（跳过不判失败）
        assert [a.kind for a in manifest.artifacts] == ["milvus", "config"]

    def test_row_counts_provider(self) -> None:
        """PG 条数快照提供器接入：快照随校验通过并可用作演练比对基线。"""
        executor = RecordingExecutor()
        counts = lambda: {"knowledge_item": 128, "knowledge_chunk": 4096}  # noqa: E731
        runner = _runner(executor, row_counts_provider=counts)
        manifest = runner.run(DAY1)
        assert manifest.status == "success"


class TestIdempotencyAndWindow:
    """防重入与低峰窗口（SR §6.1 / §9 流水线_防重入）。"""

    def test_same_day_rerun_skips(self) -> None:
        executor = RecordingExecutor()
        runner = _runner(executor)
        first = runner.run(DAY1)
        assert first.status == "success"
        again = runner.run(DAY1)
        assert again is first  # 幂等返回既有 manifest
        assert len(executor.commands) == 6  # 无重复备份：命令数不增
        assert len(runner.history) == 1

    def test_ledger_only_success_returns_skipped(self) -> None:
        """台账已有 success 但无历史 manifest（跨进程重建）：返回 skipped 不执行。"""
        executor = RecordingExecutor()
        ledger = RunLedger()
        for obj in ("pg_base", "milvus", "config"):
            ledger.finish(DAY1, obj, "success")
        runner = _runner(executor, ledger=ledger)
        manifest = runner.run(DAY1)
        assert manifest.status == "skipped"
        assert executor.commands == []

    def test_low_peak_window_gate(self) -> None:
        """窗口外（12:00）跳过；窗口内（02:30）执行；force 可人工补跑。"""
        executor = RecordingExecutor()
        runner = _runner(executor)
        out = runner.run(DAY1, now=datetime(2026, 10, 9, 12, 0))
        assert out.status == "skipped_window" and executor.commands == []
        inside = runner.run(DAY2, now=datetime(2026, 10, 10, 2, 30))
        assert inside.status == "success" and len(executor.commands) == 6
        forced = runner.run(date(2026, 10, 11), now=datetime(2026, 10, 11, 12, 0), force=True)
        assert forced.status == "success"

    def test_window_boundary(self) -> None:
        """窗口含起点不含终点（02:00 放行、05:00 拒绝）。"""
        w = LowPeakWindow("02:00", "05:00")
        assert w.contains(time(2, 0)) and w.contains(time(4, 59))
        assert not w.contains(time(5, 0)) and not w.contains(time(1, 59))


class TestRetentionGC:
    """保留期清理边界（SR §6.3 / §9 GC_边界）。"""

    def test_purge_boundary_30_kept_31_purged(self) -> None:
        today = date(2026, 10, 31)
        stored = [
            StoredBackup("backup/pg/base/20261001/base.tar", "pg_base", date(2026, 10, 1)),  # 恰 30 天 → 保留
            StoredBackup("backup/pg/base/20260930/base.tar", "pg_base", date(2026, 9, 30)),  # 31 天 → 过期
            StoredBackup("backup/milvus/20261001/b", "milvus", date(2026, 10, 1)),  # 恰 30 天 → 保留
            StoredBackup("backup/config/20260929/c", "config", date(2026, 9, 29)),  # 32 天 → 过期
        ]
        report = purge_expired(stored, retention_days=30, today=today)
        assert report.kept_count == 2 and report.purged_count == 2
        assert {i.object_key for i in report.kept} == {
            "backup/pg/base/20261001/base.tar", "backup/milvus/20261001/b",
        }
        # 清理审计计数：按对象类型分类
        assert report.audit_summary() == {
            "kept.pg_base": 1, "kept.milvus": 1, "purged.pg_base": 1, "purged.config": 1,
        }

    def test_wal_keeps_five_extra_days(self) -> None:
        """WAL 比基础备份多保留 5 天（35 天）：任意 30 天内基线均可回放。"""
        today = date(2026, 10, 31)
        stored = [
            StoredBackup("backup/pg/wal/0001", "pg_wal", date(2026, 9, 28)),  # 33 天 ≤35 → 保留
            StoredBackup("backup/pg/wal/0002", "pg_wal", date(2026, 9, 25)),  # 36 天 >35 → 过期
            StoredBackup("backup/pg/base/20260928/base.tar", "pg_base", date(2026, 9, 28)),  # 33 天 >30 → 过期
        ]
        report = purge_expired(stored, retention_days=30, today=today)
        assert [i.object_key for i in report.kept] == ["backup/pg/wal/0001"]
        assert {i.object_key for i in report.purged} == {
            "backup/pg/wal/0002", "backup/pg/base/20260928/base.tar"
        }
        assert report.wal_retention_days == 35

    def test_empty_input(self) -> None:
        report = purge_expired([], retention_days=30, today=DAY1)
        assert report.kept_count == 0 and report.purged_count == 0
        assert isinstance(report, PurgeReport)


class TestSha256:
    """manifest 逐项 sha256 计算口径。"""

    def test_sha256_hex_str_and_bytes(self) -> None:
        assert sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()
        assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()
