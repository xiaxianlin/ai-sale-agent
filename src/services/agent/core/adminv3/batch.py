# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 批量操作执行器：子项级事务 + 部分失败清单 + 仅重试失败项 + 幂等
（FR-SC-005，SR-SC-005 §6.3）。

- 动作白名单（batch_task CHECK 口径）：start / stop / restart / strategy_update；
  策略调整按账号写 ``<cfg_key>.acct.<id>`` 覆盖键（防 28 账号误共用全局键）；
- 子项级事务（§6.3 重点）：单账号失败**不回滚已成功项**，任务态 done/partial/
  failed 三态；失败项带 error_msg 进失败清单；
- 仅重试失败项（§9 用例）：``retry_fails`` 从原任务取失败账号生成新批量，
  原任务留痕不变（审计链不可变）；
- 幂等三层：①同 Idempotency-Key 重复提交返回首次任务不重复执行；②目标态幂等
  （已停再 stop 由执行器语义保证 ok）；③account_ids/params 提交即快照，防调用方
  可变对象漂移；
- 审计先行：批量任务受理即写 audit（哈希链摘要含 failed 清单与 params 快照，
  demo 为进程内列表，生产 = SR-CORE audit_log 哈希链）。

demo 形态：同步执行（生产 = 受理 202 + runner 异步执行 + 进度查询轮询/WebSocket）。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: 单批账号数上限（28 账号 = 4 主 + 24 子，账号/IP 规模口径）
MAX_BATCH_ITEMS: int = 28
#: 动作白名单（batch_task CHECK 口径）
ACTIONS: tuple[str, ...] = ("start", "stop", "restart", "strategy_update")
#: 子项状态（batch_item.state CHECK 口径）
ITEM_STATES: tuple[str, ...] = ("ok", "fail")
#: 任务状态：全部成功 / 部分失败 / 全部失败
TASK_STATUSES: tuple[str, ...] = ("done", "partial", "failed")


class BatchError(Exception):
    """批量域业务异常（40001 参数/校验类；40401 资源不存在，对齐 04-接口设计 §1.3）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class BatchItem:
    """批量子项（batch_item 行语义；失败项必带 error_msg 进失败清单）。"""

    account_id: int
    state: str                                   # ok / fail
    error_msg: str = ""


@dataclass(frozen=True)
class BatchTask:
    """批量任务（batch_task 行语义；account_ids/params 为提交时快照，不可变）。"""

    uid: str
    action: str
    account_ids: tuple[int, ...]
    params: dict[str, Any]
    status: str                                  # done / partial / failed
    items: tuple[BatchItem, ...]
    created_by: str
    created_at: datetime
    idem_key: str = ""

    @property
    def total(self) -> int:
        """批量总数（进度查询 total 字段）。"""
        return len(self.account_ids)

    @property
    def failed_account_ids(self) -> tuple[int, ...]:
        """失败清单（重试入口与状态墙红点的数据源）。"""
        return tuple(item.account_id for item in self.items if item.state == "fail")


def _digest_of(payload: dict[str, Any]) -> str:
    """审计摘要指纹（demo：canonical JSON 的 sha256；生产接 audit_log 哈希链）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BatchOps:
    """批量执行器：校验 → 快照 → 逐项执行 → 三态归集 → 审计（SR-SC-005 §6.3）。

    :param instance_ctl: 实例控制接缝（生产 = agent-runner 容器指令通道；
      demo 测试注入记录型 Fake）；约定：正常返回 = ok，抛异常 = 该子项失败；
    :param config_writer: app_config 写入接缝（策略调整按账号写覆盖键）；
    :param clock: 时钟注入。
    """

    def __init__(
        self,
        *,
        instance_ctl: Callable[[str, int], None],
        config_writer: Callable[[str, Any, str], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._instance_ctl = instance_ctl
        self._config_writer = config_writer
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self._tasks: dict[str, BatchTask] = {}          # uid → 任务（不可变留痕）
        self._idem_index: dict[str, str] = {}           # idem_key → uid（幂等索引）
        self.audit: list[dict[str, Any]] = []           # 审计流水（生产 = 哈希链 audit_log）

    # ---------------------------------------------------------------- 查询

    @property
    def tasks(self) -> list[BatchTask]:
        """任务台账（提交序快照）。"""
        with self._lock:
            return list(self._tasks.values())

    def task(self, uid: str) -> BatchTask:
        """按 uid 取任务；未知 → 40401。"""
        with self._lock:
            found = self._tasks.get(uid)
        if found is None:
            raise BatchError(40401, f"批量任务 {uid} 不存在")
        return found

    # ---------------------------------------------------------------- 提交与执行

    def submit(
        self,
        action: str,
        account_ids: list[int],
        *,
        idem_key: str,
        actor: str,
        params: dict[str, Any] | None = None,
    ) -> BatchTask:
        """受理并同步执行一个批量任务（生产形态为受理后异步，见模块 docstring）。"""
        with self._lock:
            seen = self._idem_index.get(idem_key)
            if seen is not None:
                return self._tasks[seen]                 # 幂等：同键返回首次任务，不重复执行
        self._validate(action, account_ids, params)
        snapshot_ids = tuple(int(a) for a in account_ids)          # 快照防漂移
        snapshot_params: dict[str, Any] = dict(params or {})
        items = self._execute(action, snapshot_ids, snapshot_params, actor)
        ok_cnt = sum(1 for item in items if item.state == "ok")
        if ok_cnt == len(items):
            status = "done"
        elif ok_cnt == 0:
            status = "failed"
        else:
            status = "partial"
        task = BatchTask(
            uid=f"bt-{uuid.uuid4().hex[:10]}",
            action=action,
            account_ids=snapshot_ids,
            params=snapshot_params,
            status=status,
            items=tuple(items),
            created_by=actor,
            created_at=self._clock(),
            idem_key=idem_key,
        )
        with self._lock:
            self._idem_index[idem_key] = task.uid
            self._tasks[task.uid] = task
        self._write_audit(task)
        return task

    def _validate(self, action: str, account_ids: list[int], params: dict[str, Any] | None) -> None:
        """参数校验（全部 40001：非法动作/空清单/超上限/策略调整缺参）。"""
        if action not in ACTIONS:
            raise BatchError(40001, f"非法批量动作 {action}（仅 {'/'.join(ACTIONS)}）")
        if not account_ids:
            raise BatchError(40001, "批量账号清单为空")
        if len(account_ids) > MAX_BATCH_ITEMS:
            raise BatchError(40001, f"单批账号数 {len(account_ids)} 超上限 {MAX_BATCH_ITEMS}")
        if action == "strategy_update":
            cfg = params or {}
            if "cfg_key" not in cfg or "cfg_value" not in cfg:
                raise BatchError(40001, "策略调整必须提供 cfg_key 与 cfg_value")

    def _execute(
        self, action: str, account_ids: tuple[int, ...],
        params: dict[str, Any], actor: str,
    ) -> list[BatchItem]:
        """逐项执行（子项级事务：单点失败不回滚已成功项，异常归集为失败清单）。"""
        items: list[BatchItem] = []
        for account_id in account_ids:
            try:
                if action == "strategy_update":
                    # 按账号写覆盖键：防 28 账号误共用全局键（§6.3）
                    self._config_writer(f"{params['cfg_key']}.acct.{account_id}",
                                        params["cfg_value"], actor)
                else:
                    self._instance_ctl(action, account_id)   # 目标态幂等由执行器语义保证
                items.append(BatchItem(account_id=account_id, state="ok"))
            except Exception as exc:                          # noqa: BLE001——执行器异常类型不约定
                items.append(BatchItem(account_id=account_id, state="fail", error_msg=str(exc)))
        return items

    def _write_audit(self, task: BatchTask) -> None:
        """审计先行：任务整体 + 失败清单同写 audit（摘要含 failed/params，哈希链 demo）。"""
        failed = list(task.failed_account_ids)
        digest_payload = {
            "uid": task.uid, "action": task.action, "actor": task.created_by,
            "total": task.total, "ok": task.total - len(failed), "failed": failed,
            "params": task.params,
        }
        self.audit.append({
            "action_type": f"batch_{task.action}",
            "actor": task.created_by,
            "at": task.created_at.isoformat(),
            "uid": task.uid,
            "digest": digest_payload,
            "digest_sha256": _digest_of(digest_payload),
        })

    # ---------------------------------------------------------------- 重试

    def retry_fails(self, uid: str, *, actor: str) -> BatchTask:
        """仅重试失败项：按原任务失败清单生成新批量（原任务留痕不变，§9 用例）。"""
        original = self.task(uid)
        failed = list(original.failed_account_ids)
        if not failed:
            raise BatchError(40001, f"任务 {uid} 无失败项，无需重试")
        with self._lock:
            attempt = sum(1 for t in self._tasks.values() if t.uid.startswith(f"{uid}:retry"))
        return self.submit(
            original.action, failed,
            idem_key=f"{uid}:retry:{attempt + 1}",
            actor=actor,
            params=dict(original.params),
        )


__all__ = [
    "MAX_BATCH_ITEMS", "ACTIONS", "ITEM_STATES", "TASK_STATUSES",
    "BatchError", "BatchItem", "BatchTask", "BatchOps",
]
