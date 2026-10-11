# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-005 CRM 集成·合并执行/可撤销窗口/客户 360（SR-SITE-005 §6.3/§6.4，Phase 3 第二批）。

模块职责：
- :class:`CustomerMerger.execute`——软合并：merged_into 指针 + channels 追加（带 merged_from）
  + 会话/身份物理重挂 + 全量快照台账（customer_merge 形态）；行锁按 id 排序 +
  merged_into 二次校验 + find_root 链式收口（防合并环）——并发同客户恰一胜；
- :meth:`CustomerMerger.undo`——可撤销窗口内按快照逐项还原（KPI：窗口内 100% 回滚），
  超窗/重复撤销/状态不允许 → 40901；:meth:`expire_windows` 过期归档（后台任务语义）；
- :meth:`CustomerMerger.dry_run`——confirm=false 预检，不落库；
- :meth:`CustomerMerger.build_360`——客户 360 视图：跨渠道（会话/邮件/访客行为）时间线
  运行时装配，consent=false 事件一律不纳入（FR-SITE-007）。

误合并五防线中的互斥与可撤销在本模块落实（识别侧保守策略见 identity.py）。
依据文档：docs/07-实现方案/SITE/SR-SITE-005-CRM集成.md §2.2/§6.3/§6.4。
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol


class MergeError(Exception):
    """合并域业务冲突（40901）：已合并/合并环/超撤销窗口/状态不允许。"""

    code = 40901


class NotFoundCRM(Exception):
    """CRM 对象不存在（40404 族）。"""

    code = 40404


class LockRegistry:
    """进程内互斥注册表（对齐 Redis SETNX 分布式锁语义的 demo 形态）。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def acquire(self, key: str):
        """按 key 互斥（调用方按 id 排序取锁防死锁）。"""
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield


@dataclass(frozen=True)
class MergeResult:
    """合并执行结果：台账 id / 幸存方 / 撤销截止时刻。"""

    merge_id: int
    survivor_id: int
    undo_deadline: datetime


@dataclass
class MergeTask:
    """合并任务台账（customer_merge 形态：pending/done/rejected/expired/undone）。"""

    merge_id: int
    source_id: int | None  # 被并入方（pending 人工任务时为 None）
    target_id: int | None  # 幸存方
    match_level: int | None = None  # level 0=人工发起
    score: float = 1.0
    status: str = "done"
    trigger_by: str = "auto"
    candidates: list[dict] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)  # 撤销依据（合并前双方档案+重挂行 id）
    reason: str | None = None
    created_at: datetime | None = None
    done_at: datetime | None = None
    undo_deadline: datetime | None = None
    undone_at: datetime | None = None
    undone_by: str | None = None


class TimelineSource(Protocol):
    """客户 360 时间线数据源协议（跨渠道事件注入接缝；生产为 PG 三路查询形态）。"""

    def conversations_of(self, customer_ids: list[int]) -> list[dict]:
        """会话/消息事件（已物理重挂，按客户集合查询）。"""
        ...

    def mail_jobs_of(self, emails: list[str]) -> list[dict]:
        """邮件事件（mail_job 无 customer_id，经邮箱身份运行时关联）。"""
        ...

    def visitor_events_of(self, external_ids: list[str]) -> list[dict]:
        """访客行为事件（仅 consent=true 由装配侧强制过滤）。"""
        ...


class CustomerMerger:
    """合并执行与撤销（指针 + channels 追加 + 重挂 + 审计快照）。"""

    def __init__(self, store: Any, *, clock: Callable[[], datetime], undo_window_h: int = 72,
                 locks: LockRegistry | None = None) -> None:
        self.store = store
        self.clock = clock
        self.undo_window_h = undo_window_h  # CRM_UNDO_WINDOW_H（默认 72h，配置外置）
        self.locks = locks or LockRegistry()
        self._task_id = 0
        self.audit: list[dict] = []  # audit_log 追加记录（合并/撤销全留痕，NFR-SEC-03）

    # ---------------------------------------------------------------- 合并执行

    def execute(self, source_id: int, target_id: int, *, trigger_by: str = "auto",
                level: int = 0, score: float = 1.0, reason: str = "") -> MergeResult:
        """执行合并：双锁互斥 → 状态校验 → 快照 → 指针/追加/重挂 → 台账 done。"""
        lock_keys = [f"cust:{i}" for i in sorted({source_id, target_id})]  # 按 id 排序防死锁
        with self.locks.acquire(lock_keys[0]):
            if len(lock_keys) > 1:
                with self.locks.acquire(lock_keys[1]):
                    return self._execute_locked(source_id, target_id, trigger_by, level, score, reason)
            return self._execute_locked(source_id, target_id, trigger_by, level, score, reason)

    def _execute_locked(self, source_id: int, target_id: int, trigger_by: str,
                        level: int, score: float, reason: str) -> MergeResult:
        """持锁后的合并主体（互斥临界区）。"""
        src = self.store.customers.get(source_id)
        tgt = self.store.customers.get(target_id)
        if src is None or tgt is None:
            raise NotFoundCRM(f"客户不存在：{source_id or target_id}")
        if src.merged_into is not None:  # 双保险：source 已带 merged_into 拒绝
            raise MergeError(f"source 客户 {source_id} 已被合并")
        root_id = self.store.find_root(target_id)  # 顺链找根（防并入已合并档）
        if root_id == source_id:
            raise MergeError("合并环：target 的根即 source 自身")
        now = self.clock()
        snap = {  # 全量快照（撤销依据）：双方档案 + 将重挂的行 id 清单
            "source_merged_into": src.merged_into,
            "source_channels": copy.deepcopy(src.channels),
            "target_channels": copy.deepcopy(tgt.channels),
            "conversation_ids": [c.conversation_id for c in self.store.conversations
                                 if c.customer_id == source_id],
            "identity_ids": [i.identity_id for i in self.store.identities
                             if i.customer_id == source_id],
        }
        # ① 软合并指针 + ② channels 追加（保留全部渠道历史，标记来源）
        src.merged_into = root_id
        tgt.channels = tgt.channels + [{**c, "merged_from": str(source_id)} for c in src.channels]
        # ② 关联数据物理重挂：会话 + 身份（邮件/访客经身份表自然关联）
        for conv in self.store.conversations:
            if conv.customer_id == source_id:
                conv.customer_id = root_id
        for ident in self.store.identities:
            if ident.customer_id == source_id:
                ident.customer_id = root_id
        # ③ 台账 + 审计（撤销依据与 NFR-SEC-03 留痕）
        self._task_id += 1
        task = MergeTask(merge_id=self._task_id, source_id=source_id, target_id=root_id,
                         match_level=level, score=score, status="done", trigger_by=trigger_by,
                         snapshot=snap, reason=reason, created_at=now, done_at=now,
                         undo_deadline=now + timedelta(hours=self.undo_window_h))
        self.store.merge_tasks[task.merge_id] = task
        self.audit.append({"action": "customer_merge", "task": task.merge_id,
                           "detail": f"{source_id}->{root_id}", "by": trigger_by, "at": now})
        return MergeResult(task.merge_id, root_id, task.undo_deadline)

    # ---------------------------------------------------------------- 撤销

    def undo(self, merge_id: int, actor: str) -> MergeTask:
        """撤销合并：仅窗口内且 done 状态；按快照逐项还原（合并的逆操作）。"""
        task = self.store.merge_tasks.get(merge_id)
        if task is None:
            raise NotFoundCRM(f"合并台账不存在：{merge_id}")
        now = self.clock()
        if task.status != "done" or task.undo_deadline is None or now > task.undo_deadline:
            raise MergeError("超出可撤销窗口或状态不允许撤销")
        with self.locks.acquire(f"cust:{task.source_id}"):
            if task.status != "done":  # 持锁后复核（并发撤销恰一胜）
                raise MergeError("状态不允许撤销")
            snap = task.snapshot
            src = self.store.customers[task.source_id]
            tgt = self.store.customers[task.target_id]
            src.merged_into = snap["source_merged_into"]
            src.channels = copy.deepcopy(snap["source_channels"])
            tgt.channels = copy.deepcopy(snap["target_channels"])
            for conv in self.store.conversations:
                if conv.conversation_id in snap["conversation_ids"]:
                    conv.customer_id = task.source_id
            for ident in self.store.identities:
                if ident.identity_id in snap["identity_ids"]:
                    ident.customer_id = task.source_id
            task.status = "undone"
            task.undone_at, task.undone_by = now, actor
            self.audit.append({"action": "customer_merge_undo", "task": merge_id,
                               "by": actor, "at": now})
        return task

    def expire_windows(self) -> int:
        """过期窗口归档（后台任务语义）：超窗 done → expired，此后不可撤销。"""
        now = self.clock()
        expired = 0
        for task in self.store.merge_tasks.values():
            if task.status == "done" and task.undo_deadline is not None and now > task.undo_deadline:
                task.status = "expired"
                expired += 1
        return expired

    # ---------------------------------------------------------------- 预检与队列

    def dry_run(self, source_id: int, target_id: int) -> dict:
        """预检（confirm=false 与人工确认界面共用）：返回对照信息，不落库。"""
        src = self.store.customers.get(source_id)
        tgt = self.store.customers.get(target_id)
        if src is None or tgt is None:
            raise NotFoundCRM(f"客户不存在：{source_id or target_id}")
        return {
            "dry_run": True,
            "source_id": source_id,
            "target_id": target_id,
            "source_channels": [dict(c) for c in src.channels],
            "target_channels": [dict(c) for c in tgt.channels],
            "conversation_count": sum(1 for c in self.store.conversations
                                      if c.customer_id in (source_id, target_id)),
            "source_merged_into": src.merged_into,
        }

    def create_pending_task(self, lead_row: Any, candidates: list, *,
                            trigger_by: str = "auto") -> MergeTask:
        """人工确认队列入队（识别结果不确定时；候选与分值全保留）。"""
        self._task_id += 1
        task = MergeTask(
            merge_id=self._task_id, source_id=None, target_id=None,
            match_level=lead_row.match_level if lead_row.match_level is not None
            else (candidates[0].level if candidates else None),
            status="pending", trigger_by=trigger_by,
            candidates=[{"customer_id": c.customer_id, "level": c.level, "score": c.score}
                        for c in candidates],
            reason=f"lead#{lead_row.lead_id} 待人工确认",
            created_at=self.clock())
        self.store.merge_tasks[task.merge_id] = task
        return task

    # ---------------------------------------------------------------- 客户 360

    def build_360(self, customer_id: int, days: int = 180,
                  timeline: TimelineSource | None = None) -> dict:
        """客户 360：合并链收口 → 跨渠道时间线按时间归并（consent=false 一律不纳入）。"""
        root_id = self.store.find_root(customer_id)
        ids = self.store.chain_ids(root_id)
        root = self.store.customers[root_id]
        emails = sorted(self.store.emails_of(ids))
        site_externals = [c["external_id"] for cid in ids
                          for c in self.store.customers[cid].channels if c["channel"] == "site"]
        items: list[dict] = []
        if timeline is not None:  # 三渠道事件运行时装配（会话已重挂/邮件经邮箱/访客经 site 身份）
            items += timeline.conversations_of(ids)
            items += timeline.mail_jobs_of(emails)
            items += timeline.visitor_events_of(site_externals)
        else:  # 未注入时间线源时退化为会话事件（API demo 形态）
            items += [{"ts": c.created_at, "channel": c.channel, "kind": "message",
                       "summary": c.summary}
                      for c in self.store.conversations if c.customer_id in ids]
        items = [i for i in items if i.get("consent", True) is not False]  # FR-SITE-007 强制
        items.sort(key=lambda i: i["ts"])
        return {
            "customer": {"customer_id": root_id, "display_name": root.display_name,
                         "company": root.company, "country": root.country},
            "channels": [dict(c) for c in root.channels],
            "chain_ids": ids,
            "grade": None,
            "items": items,
        }
