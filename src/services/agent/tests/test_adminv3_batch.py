# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 批量操作单元测试（FR-SC-005，SR-SC-005 §6.3/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（批量启停、批量策略调整落账号级覆盖键）/
边界（max_items=28 上限、全失败/全成功归类）/ 异常（非法 action、空清单、策略
调整缺参、未知任务与无失败项重试）/ 幂等（同 Idempotency-Key no-op、目标态幂等、
参数快照防漂移）/ 降级（部分失败**不回滚已成功项** + 失败清单 + 仅重试失败项）。
验收口径映射：批量操作正确性（SRS §16.5）——部分失败不回滚 / 仅重试失败项 /
批量幂等三用例组直接对应 SR-SC-005 §9。

实现方案：docs/07-实现方案/SC/SR-SC-005-管理后台V3.md §6.3、§9
"""

from __future__ import annotations

from datetime import datetime

import pytest

from services.agent.core.adminv3.batch import ACTIONS, BatchError, BatchOps

NOW = datetime(2026, 10, 11, 10, 0, 0)


def make_ops(fail_ids: set[int] | None = None, *,
             stopped: set[int] | None = None) -> tuple[BatchOps, dict]:
    """标准批量执行器：fail_ids 注入失败实例；calls 记录逐项调用（断言无副作用）。"""
    calls: dict = {"ctl": [], "cfg": []}
    fails = fail_ids or set()

    def instance_ctl(action: str, account_id: int) -> None:
        calls["ctl"].append((action, account_id))
        if account_id in fails:
            fails.discard(account_id)                                    # 一次性故障注入：重试即恢复（§9 重试成功用例前提）
            raise RuntimeError(f"容器操作超时 acct={account_id}")
        if action == "stop" and stopped and account_id in stopped:
            return                                                       # 目标态幂等：已停再停 ok

    def config_writer(cfg_key: str, cfg_value: object, actor: str) -> None:
        calls["cfg"].append((cfg_key, cfg_value, actor))

    ops = BatchOps(instance_ctl=instance_ctl, config_writer=config_writer, clock=lambda: NOW)
    return ops, calls


class TestHappyPaths:
    """等价类：批量启停与批量策略调整正常路径。"""

    def test_batch_stop_all_ok(self) -> None:
        """批量 stop 全部成功 → status=done，逐项 ok 且逐项执行。"""
        ops, calls = make_ops()
        task = ops.submit("stop", [1, 2, 3], idem_key="k1", actor="admin:1")
        assert task.status == "done"
        assert all(i.state == "ok" for i in task.items)
        assert len(calls["ctl"]) == 3

    def test_strategy_update_writes_per_account_key(self) -> None:
        """批量策略调整：按账号写 .acct.{id} 覆盖键（防 28 账号误共用全局键）。"""
        ops, calls = make_ops()
        task = ops.submit("strategy_update", [101, 102], idem_key="k2", actor="admin:1",
                          params={"cfg_key": "strategy.followup.cadence", "cfg_value": {"A": [1, 2, 3]}})
        assert task.status == "done"
        assert [c[0] for c in calls["cfg"]] == [
            "strategy.followup.cadence.acct.101", "strategy.followup.cadence.acct.102"]

    def test_actions_whitelist(self) -> None:
        """动作白名单 = start/stop/restart/strategy_update（batch_task CHECK 口径）。"""
        assert ACTIONS == ("start", "stop", "restart", "strategy_update")


class TestPartialFailure:
    """降级：部分失败不回滚 + 失败清单 + 仅重试失败项（§6.3 重点）。"""

    def test_partial_failure_keeps_success_no_rollback(self) -> None:
        """28 项批量、3 项故意失败：其余 25 项保持 ok（不回滚），status=partial（§16.5）。"""
        ops, _ = make_ops(fail_ids={7, 19, 23})
        task = ops.submit("restart", list(range(1, 29)), idem_key="k3", actor="admin:1")
        assert task.status == "partial"
        assert [i.account_id for i in task.items if i.state == "fail"] == [7, 19, 23]
        assert all(i.state == "ok" for i in task.items if i.account_id not in {7, 19, 23})
        assert all(i.error_msg for i in task.items if i.state == "fail")

    def test_all_failed_marks_failed(self) -> None:
        """边界：全失败 → status=failed（非 partial）。"""
        task = make_ops(fail_ids={1, 2})[0].submit("stop", [1, 2], idem_key="k4", actor="a")
        assert task.status == "failed"

    def test_retry_fails_only_failed_items(self) -> None:
        """仅重试失败项：新批量只含失败账号；原任务留痕不变（§9 用例）。"""
        ops, _ = make_ops(fail_ids={7, 19})
        original = ops.submit("restart", list(range(1, 20)), idem_key="k5", actor="a")
        fixed = ops.retry_fails(original.uid, actor="admin:1")
        assert fixed.account_ids == (7, 19) and fixed.status == "done"
        assert ops.task(original.uid).status == "partial"

    def test_retry_without_fails_or_unknown_task(self) -> None:
        """异常：无失败项重试 → 40001；未知任务重试 → 40401。"""
        ops, _ = make_ops()
        task = ops.submit("stop", [1], idem_key="k6", actor="a")
        with pytest.raises(BatchError) as no_fails:
            ops.retry_fails(task.uid, actor="a")
        assert no_fails.value.code == 40001
        with pytest.raises(BatchError) as unknown:
            ops.retry_fails("b-not-exist", actor="a")
        assert unknown.value.code == 40401

    def test_audit_records_failed_list(self) -> None:
        """审计先行：批量任务整体与失败清单同写 audit（哈希链摘要含 failed/params）。"""
        ops, _ = make_ops(fail_ids={7})
        task = ops.submit("stop", [1, 7], idem_key="k7", actor="admin:9")
        (event,) = ops.audit
        assert (event["action_type"], event["actor"]) == ("batch_stop", "admin:9")
        assert event["digest"]["failed"] == [7]
        assert task.created_by == "admin:9"


class TestIdempotency:
    """并发/幂等：Idempotency-Key 与目标态语义。"""

    def test_duplicate_idem_key_returns_first_task(self) -> None:
        """同 Idempotency-Key 重复提交 → 返回首次任务，不重复执行（§9 用例）。"""
        ops, calls = make_ops()
        first = ops.submit("stop", [1, 2], idem_key="same-key", actor="a")
        second = ops.submit("stop", [1, 2], idem_key="same-key", actor="a")
        assert second.uid == first.uid
        assert len(ops.tasks) == 1 and len(calls["ctl"]) == 2             # 仅首次执行

    def test_target_state_idempotent_and_snapshot_immutable(self) -> None:
        """目标态幂等（已停再 stop ok 无副作用）；account_ids/params 落库快照防漂移。"""
        ops, calls = make_ops(stopped={5})
        assert ops.submit("stop", [5], idem_key="k8", actor="a").status == "done"
        assert calls["ctl"] == [("stop", 5)]
        ids, params = [1, 2], {"cfg_key": "k", "cfg_value": 1}
        task = ops.submit("strategy_update", ids, idem_key="k9", actor="a", params=params)
        ids.append(3)
        params["cfg_value"] = 999
        assert task.account_ids == (1, 2) and task.params["cfg_value"] == 1


class TestValidation:
    """异常：参数校验（40001）。"""

    def test_invalid_inputs_rejected(self) -> None:
        """非法 action / 空清单 / 超 28 上限 / 策略调整缺参 → 40001。"""
        ops, _ = make_ops()
        bad_calls = [
            lambda: ops.submit("format_disk", [1], idem_key="k10", actor="a"),
            lambda: ops.submit("stop", [], idem_key="k11", actor="a"),
            lambda: ops.submit("stop", list(range(1, 30)), idem_key="k12", actor="a"),
            lambda: ops.submit("strategy_update", [1], idem_key="k13", actor="a",
                               params={"cfg_key": "x"}),
        ]
        for bad in bad_calls:
            with pytest.raises(BatchError) as ei:
                bad()
            assert ei.value.code == 40001
