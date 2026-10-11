# 知识产权归甲方所有（COM-IP-01）
"""TDD 补充测试（平台域）：SEC-06 数据不出域 + REL-04 数据备份——五类场景缺口补齐。

TDD 新规口径：等价类 / 边界 / 异常 / 并发 / 降级 五类场景逐列审计后，
本文件只覆盖既有 test_egress_*.py / test_backup_*.py 未覆盖的缺口：
- SEC-06：register_all 批量冲突保留已注册前缀（文档承诺未测）、并发 assert_egress
  序号唯一单调（竞态）、http scheme 默认端口 80（等价类）、目的地端口边界值、
  URL 主机大小写等价、http-only 目的地拒绝 https、畸形 URL 留痕字段形态、
  空白块剔除后的载荷拼接（网关管线边界）；
- REL-04：上传步失败的重试语义、基线跳过失败 manifest（降级）、基线 0 字节边界、
  空行数快照边界、成功后 force 重入仍幂等、WAL 显式保留天数覆盖。

KPI 映射：SEC-06 → NFR-SEC-06 数据不出域（EGRESS-1001 白名单外拦截率 100%、
留痕完整率）；REL-04 → NFR-REL-04（备份成功率告警、保留 ≥30 天、RPO 分钟级）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from foundation.backup import (
    BackupConfig,
    BackupRunner,
    RecordingExecutor,
    StoredBackup,
    default_plan,
    purge_expired,
    verify_backup,
)
from foundation.net.egress import (
    EGRESS_UNREGISTERED,
    EgressDestination,
    EgressGuard,
    EgressRegistryError,
    EgressViolation,
)
from foundation.net.llm_gateway import (
    LlmGatewayConfig,
    LLMEgressGateway,
)

DAY1 = date(2026, 10, 9)


def _dest(**overrides: object) -> EgressDestination:
    """DeepSeek 目的地基线（443/https/llm 用途）。"""
    fields: dict[str, object] = {
        "code": "deepseek_api",
        "host": "api.deepseek.com",
        "port": 443,
        "purposes": ("llm_chat",),
        "owner": "AI 平台组",
    }
    fields.update(overrides)
    return EgressDestination(**fields)  # type: ignore[arg-type]


def _guard_with_llm_dest() -> tuple[EgressGuard, object]:
    """注册好 LLM 目的地的 guard + HTTP 桩（llm_gateway 用）。"""

    class StubHttpClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def post(self, url: str, body: dict[str, object]) -> dict[str, object]:
            self.calls.append((url, dict(body)))
            return {"text": "ok"}

    http = StubHttpClient()
    guard = EgressGuard()
    guard.register(_dest(purposes=("llm_chat", "reply_generate")))
    return guard, http


# ---------------------------------------------------------------- SEC-06 EgressGuard


class TestEgressSupplement:
    def test_register_all_conflict_keeps_registered_prefix(self) -> None:
        """【异常】批量注册中途冲突：抛 EgressRegistryError 且已注册前缀保持（A、B 在册，
        冲突项及其后不落地）——注册表不留半清理中间态。

        KPI 映射：NFR-SEC-06 注册表唯一权威（批量装载的一致性语义）。
        """
        guard = EgressGuard()
        with pytest.raises(EgressRegistryError, match="编码已注册"):
            guard.register_all([
                _dest(code="dest_a", host="a.example.com"),
                _dest(code="dest_b", host="b.example.com"),
                _dest(code="dest_a", host="c.example.com"),  # 编码冲突
            ])
        assert sorted(d.code for d in guard.destinations) == ["dest_a", "dest_b"]

    def test_concurrent_assert_egress_seq_unique_monotonic(self) -> None:
        """【并发】12 线程 × 8 次 assert_egress：96 条留痕 seq 逐一唯一且连续 1..96。

        KPI 映射：NFR-SEC-06 出域留痕完整率（并发拦截/放行均一条不丢、不重号）。
        """
        guard = EgressGuard()
        guard.register(_dest())
        errors: list[Exception] = []

        def produce(worker: int) -> None:
            for i in range(8):
                try:
                    guard.assert_egress(
                        "https://api.deepseek.com", "llm_chat",
                        caller=f"w{worker}", trace_id=f"t-{worker}-{i}",
                    )
                except Exception as exc:  # pragma: no cover - 出现即断言失败
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(produce, range(12)))
        assert errors == []
        records = guard.iter_calls()
        assert len(records) == 96
        assert sorted(r.seq for r in records) == list(range(1, 97))
        assert all(r.verdict == "allowed" for r in records)

    def test_http_scheme_default_port_80(self) -> None:
        """【等价类】http 目的地默认端口 80：注册 port=80/schemes=("http",) 后
        http:// 放行、https://（443 未注册）拒绝——scheme→默认端口映射双向精确。

        KPI 映射：NFR-SEC-06 白名单精确匹配（默认端口不可被跨 scheme 复用）。
        """
        guard = EgressGuard()
        guard.register(_dest(code="webhook", host="hook.example.com", port=80, schemes=("http",)))
        assert guard.is_allowed("http://hook.example.com") is True
        assert guard.is_allowed("http://hook.example.com:80") is True
        assert guard.is_allowed("https://hook.example.com") is False

    def test_destination_port_boundaries(self) -> None:
        """【边界】目的地端口边界：1 与 65535 合法，0 / 65536 / 负数构造期拒绝。

        KPI 映射：NFR-SEC-06 注册表入参校验（非法端口不入注册表）。
        """
        assert _dest(port=1).port == 1
        assert _dest(port=65535).port == 65535
        for bad in (0, 65536, -1):
            with pytest.raises(ValidationError):
                _dest(port=bad)  # type: ignore[arg-type]

    def test_url_host_case_insensitive(self) -> None:
        """【等价类】URL 主机大小写：API.DEEPSEEK.COM 归一为小写后命中同一注册项。

        KPI 映射：NFR-SEC-06 白名单匹配（大小写变体不构成绕过面）。
        """
        guard = EgressGuard()
        guard.register(_dest())
        assert guard.is_allowed("https://API.DEEPSEEK.COM/v1/chat") is True
        assert guard.is_allowed("https://Api.DeepSeek.Com") is True

    def test_http_only_dest_rejects_https(self) -> None:
        """【异常】同 host:port 下 scheme 白名单反向校验：注册仅 http 的 443 目的地，
        https 请求按 EGRESS-1001 拒绝（协议不匹配也是白名单外目标）。

        KPI 映射：NFR-SEC-06 出域目标仅 TLS 端口的反向口径（协议维度同样 fail-closed）。
        """
        guard = EgressGuard()
        guard.register(_dest(schemes=("http",)))
        result = guard.check("https://api.deepseek.com")
        assert not result.ok
        assert result.code == EGRESS_UNREGISTERED
        assert "https" in result.detail

    def test_malformed_url_blocked_record_has_empty_target(self) -> None:
        """【异常】断言畸形 URL（非 http/https）：拦截留痕中解析失败字段置空
        （scheme=""、host=""、port=0），block_reason 携带 EGRESS-1001。

        KPI 映射：NFR-SEC-06 拦截留痕完整率（不可解析目标同样落账可审计）。
        """
        guard = EgressGuard()
        guard.register(_dest())
        with pytest.raises(EgressViolation):
            guard.assert_egress("ftp://files.example.com/secret", "llm_chat", content="x")
        record = guard.iter_calls()[-1]
        assert record.verdict == "blocked"
        assert record.block_reason.startswith(EGRESS_UNREGISTERED)
        assert record.scheme == "" and record.host == "" and record.port == 0
        assert record.dest_code == ""

    def test_blank_blocks_dropped_from_payload(self) -> None:
        """【边界】管线剔除空白块：send(["块一", "   ", "块二"]) 载荷仅保留非空块，
        以 \n 连接——空白块不进入出域内容与快照。

        KPI 映射：NFR-SEC-06 出域内容最小化（无内容块不出域）。
        """
        guard, http = _guard_with_llm_dest()
        gateway = LLMEgressGateway(guard, http)  # type: ignore[arg-type]
        gateway.send(["块一", "   ", "块二"])
        assert http.calls[0][1]["content"] == "块一\n块二"  # type: ignore[index]

    def test_gateway_config_rejects_unknown_word_level(self) -> None:
        """【异常】LlmGatewayConfig 敏感词仅 warn/block 两级：未知级别构造期拒绝。

        KPI 映射：NFR-SEC-06 词表配置化边界（NFR-MAI-02 配置项契约校验前移）。
        """
        with pytest.raises(ValidationError, match="warn/block"):
            LlmGatewayConfig(sensitive_words={"critical": ("机密",)})  # type: ignore[dict-item]


# ---------------------------------------------------------------- REL-04 备份


class TestBackupSupplement:
    def test_upload_step_failure_retries_whole_object(self) -> None:
        """【异常】上传步（ossutil）失败：单对象重试 max_retry 次——重试重新执行
        「备份+上传」整组命令，最终 StepFailure.attempts=3，三对象全部失败并告警。

        KPI 映射：NFR-REL-04 备份成功率与失败告警（上传链路故障同样计入失败）。
        """
        executor = RecordingExecutor(fail_on=["ossutil"])
        hook_events: list[dict[str, str]] = []

        class Hook:
            def send(self, level: str, type: str, title: str, body: str) -> None:
                hook_events.append({"level": level, "type": type, "title": title, "body": body})

        runner = BackupRunner(default_plan(), executor, alerter=Hook(), config=BackupConfig(max_retry=2))
        manifest = runner.run(DAY1)
        assert manifest.status == "failed"
        assert [f.kind for f in manifest.failures] == ["pg_base", "milvus", "config"]
        assert all(f.attempts == 3 for f in manifest.failures)
        # 每对象重试 3 轮：备份命令各执行 3 次（整组重试，不是只重传上传）
        assert len([c for c in executor.commands if c.startswith("pg_basebackup")]) == 3
        assert len(hook_events) == 1 and hook_events[0]["level"] == "high"

    def test_baseline_skips_failed_manifests(self) -> None:
        """【降级】大小基线只取最近一次「成功」manifest：中间失败日不污染基线，
        恢复正常产出后校验照常通过（失败日的缩水产物不作为参照）。

        KPI 映射：NFR-REL-04 备份可用性校验（基线演化在故障日后仍可信）。
        """
        runner1 = BackupRunner(default_plan(), RecordingExecutor())
        assert runner1.run(DAY1).status == "success"          # 建立基线（D1）
        shrunk = RecordingExecutor(stdout_of={"ossutil": "x"})
        runner2 = BackupRunner(default_plan(), shrunk, history=runner1.history)
        assert runner2.run(date(2026, 10, 10)).status == "failed"  # D2 全部缩水失败
        runner3 = BackupRunner(default_plan(), RecordingExecutor(),
                               history=[*runner1.history, *runner2.history])
        assert runner3.run(date(2026, 10, 11)).status == "success"  # D3 以 D1 为基线恢复通过

    def test_verify_baseline_zero_bytes_fails(self) -> None:
        """【边界】基线为 0 字节（退化基线）：偏差按 100% 计判失败（除零保护分支）。

        KPI 映射：NFR-REL-04 校验三件套（退化基线不放行空备份）。
        """
        result = verify_backup(kind="pg_base", content="abc", baseline_bytes=0)
        assert not result.ok
        assert result.checks[0].name == "大小偏差"

    def test_verify_empty_row_counts_fails(self) -> None:
        """【边界】条数快照为空 dict：视为「快照不可读」判失败（非 None 的空提供无效）。

        KPI 映射：NFR-REL-04 校验三件套（提供器故障不静默跳过）。
        """
        result = verify_backup(kind="pg_base", content="x", baseline_bytes=None, row_counts={})
        assert not result.ok
        assert any(c.name == "条数快照" and not c.passed for c in result.checks)

    def test_force_rerun_after_success_still_idempotent(self) -> None:
        """【边界】同日成功后 force=True 人工补跑：防重入优先于 force——返回既有
        manifest、不产生新命令（force 只绕过窗口判定，不绕过防重入）。

        KPI 映射：NFR-REL-04 防重入（人工补跑不产生重复备份）。
        """
        executor = RecordingExecutor()
        runner = BackupRunner(default_plan(), executor)
        first = runner.run(DAY1)
        assert first.status == "success"
        again = runner.run(DAY1, now=datetime(2026, 10, 9, 12, 0), force=True)
        assert again is first
        assert len(executor.commands) == 6                  # 无新增命令
        assert len(runner.history) == 1

    def test_purge_wal_explicit_retention_override(self) -> None:
        """【边界】WAL 显式保留天数覆盖（默认 retention+5）：31 天边界保留、32 天清理，
        基础备份仍按 retention_days=30 判定——两类对象保留窗口互不串扰。

        KPI 映射：NFR-REL-04 保留策略（WAL ≥ 基础备份，任意基线可回放）。
        """
        today = date(2026, 10, 31)
        stored = [
            StoredBackup("backup/pg/wal/w31", "pg_wal", date(2026, 9, 30)),   # 恰 31 天 → 保留
            StoredBackup("backup/pg/wal/w32", "pg_wal", date(2026, 9, 29)),   # 32 天 → 清理
            StoredBackup("backup/pg/base/b31/base.tar", "pg_base", date(2026, 9, 30)),  # 31 天 >30 → 清理
        ]
        report = purge_expired(stored, retention_days=30, today=today, wal_retention_days=31)
        assert report.wal_retention_days == 31
        assert [i.object_key for i in report.kept] == ["backup/pg/wal/w31"]
        assert {i.object_key for i in report.purged} == {"backup/pg/wal/w32", "backup/pg/base/b31/base.tar"}
