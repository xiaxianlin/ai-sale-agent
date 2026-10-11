# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识库初始化异步长任务（FR-SAAS-002，SR-SAAS-002 §6.2）。

文件粒度任务：进度可查（轮询）、失败重试（指数退避 30/60/120s）、
部分失败进 failed 回退态（人工修复后仅补跑 failed 文件）。
- 入队幂等：UNIQUE(task, oss_key)，同向导重复 start 不翻倍；
- 瞬时故障（TransientError，OCR/ASR/Embedding 抖动）→ attempts < retry_max 重排队；
- 确定性故障（DeterministicError，格式不支持等）→ 不重试直落 failed；
- 任务收敛回调 on_transit：全部成功 initializing→active（租户可用门打开）、
  部分失败 initializing→failed、重试 failed→initializing。

demo 口径：队列与文件状态为进程内权威（生产 = PG tenant_kb_init_* + Redis Stream
saas:onboarding:kbinit + worker 心跳回灌）；退避只记录不睡眠（生产由队列延迟投递）。
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查期引用，运行时经方法内局部导入防循环（wizard↔kb_init）
    from services.saas.core.onboarding.wizard import OnboardingWizard

#: 单文件失败重试上限（SR §7：saas.onboarding.kb_retry_max 默认 3）
KB_RETRY_MAX: int = 3
#: 重试退避基数秒（指数 30/60/120s，SR §7：kb_retry_backoff_sec）
KB_BACKOFF_BASE_SEC: int = 30

#: 任务/文件状态词汇（与向导状态机同域：initializing→active / failed 回退）
TASK_RUNNING = "initializing"
TASK_DONE = "done"
TASK_FAILED = "failed"
FILE_QUEUED = "queued"
FILE_RUNNING = "running"
FILE_DONE = "done"
FILE_FAILED = "failed"


class TransientError(Exception):
    """瞬时故障（OCR/ASR/Embedding 抖动）→ 退避重试。"""


class DeterministicError(Exception):
    """确定性故障（格式不支持等）→ 不重试直落失败。"""


@dataclass
class KbFile:
    """文件粒度任务项（tenant_kb_init_file 行的进程内形态）。"""

    id: int
    oss_key: str
    format: str
    status: str = FILE_QUEUED
    attempts: int = 0
    last_error: str | None = None
    delays: list[int] = field(default_factory=list)  # 每次重排队的退避秒（观测用）


@dataclass
class KbTask:
    """初始化任务（tenant_kb_init_task 行的进程内形态）。"""

    uid: str
    id: int
    tenant_id: int
    onboarding_uid: str
    status: str = TASK_RUNNING  # 任务级 running（与向导 initializing 同域口径）
    files: list[KbFile] = field(default_factory=list)


def _not_found(msg: str) -> Exception:
    """40401 业务错误（局部导入 OnboardingError 防模块级循环导入）。"""
    from services.saas.core.onboarding.wizard import OnboardingError

    return OnboardingError(40401, msg)


class KbInitService:
    """知识库初始化长任务：start 入队 / run_task 消费 / progress 查询 / retry_failed 重试。

    on_transit 回调由向导注入（uid, from_status, to_status, by, detail）——
    任务收敛时迁移向导状态，本服务不直接依赖向导类型（防循环导入）。
    """

    def __init__(self, *, on_transit: Callable[..., None],
                 retry_max: int = KB_RETRY_MAX,
                 backoff_base_sec: int = KB_BACKOFF_BASE_SEC) -> None:
        self._on_transit = on_transit
        self._retry_max = retry_max
        self._backoff_base = backoff_base_sec
        self._lock = threading.Lock()
        self.tasks: dict[str, KbTask] = {}
        self._next_task_id = 1
        self._next_file_id = 1

    # -- 入队 ----------------------------------------------------------------

    def start(self, onboarding: Any) -> KbTask:
        """激活后建任务入队（幂等：UNIQUE(task,oss_key)，重复 start 文件不翻倍）。"""
        materials = onboarding.steps.get("kb_init", {}).get("materials", [])
        with self._lock:
            existing = next((t for t in self.tasks.values()
                             if t.onboarding_uid == onboarding.uid), None)
            if existing is None:
                task = KbTask(uid=f"kbt_{uuid.uuid4().hex[:12]}",
                              id=self._next_task_id, tenant_id=onboarding.tenant_id,
                              onboarding_uid=onboarding.uid)
                self._next_task_id += 1
                self.tasks[task.uid] = task
            else:
                task = existing
            seen = {f.oss_key for f in task.files}
            for m in materials:
                if m["oss_key"] not in seen:  # 入队幂等
                    task.files.append(KbFile(id=self._next_file_id, oss_key=m["oss_key"],
                                             format=m["format"]))
                    self._next_file_id += 1
            return task

    # -- 消费 ----------------------------------------------------------------

    def run_task(self, task_uid: str,
                 pipeline: Callable[[str, str], None]) -> str:
        """worker 消费循环：CAS 认领 queued→running → 调 P0 流水线 → 状态收敛。

        返回任务终态："done"（全部成功）或 "failed"（存在失败文件）。
        """
        task = self.tasks.get(task_uid)
        if task is None:
            raise _not_found(f"初始化任务不存在：{task_uid}")
        while True:
            with self._lock:
                f = next((x for x in task.files if x.status == FILE_QUEUED), None)
                if f is not None:
                    f.status = FILE_RUNNING
                    f.attempts += 1
            if f is None:
                break  # 队列清空 → 收敛
            try:
                pipeline(f.oss_key, f.format)  # P0 流水线（解析→清洗→分块→向量化）
                f.status = FILE_DONE
            except TransientError as exc:
                if f.attempts >= self._retry_max:
                    self._fail_file(task, f, str(exc))
                else:  # 退避重排队（demo 仅记录延迟，不阻塞）
                    delay = self._backoff_base * 2 ** (f.attempts - 1)
                    f.delays.append(delay)
                    f.status = FILE_QUEUED
                    f.last_error = str(exc)
            except DeterministicError as exc:  # 确定性故障：不重试
                self._fail_file(task, f, str(exc))
        return self._settle(task)

    def _fail_file(self, task: KbTask, f: KbFile, error: str) -> None:
        """文件落 failed（生产此处置高级告警 alert.push saas_kb_init_failed）。"""
        f.status = FILE_FAILED
        f.last_error = error

    def _settle(self, task: KbTask) -> str:
        """任务收敛：全部成功→done + 向导 active；存在失败→failed + 向导 failed。"""
        failed = [f for f in task.files if f.status == FILE_FAILED]
        if not failed:
            task.status = "done"
            self._on_transit(task.onboarding_uid, "initializing", "active",
                             by="system", detail={"done_files": len(task.files)})
            return TASK_DONE
        task.status = TASK_FAILED
        self._on_transit(task.onboarding_uid, "initializing", "failed",
                         by="system", detail={"failed_files": len(failed)})
        return TASK_FAILED

    # -- 查询 / 重试 ----------------------------------------------------------

    def progress(self, task_uid: str) -> dict:
        """文件粒度进度（percent 保留 1 位，轮询渲染进度条）。"""
        task = self.tasks.get(task_uid)
        if task is None:
            raise _not_found(f"初始化任务不存在：{task_uid}")
        total = len(task.files)
        done = sum(1 for f in task.files if f.status == FILE_DONE)
        failed = sum(1 for f in task.files if f.status == FILE_FAILED)
        percent = round(done / total * 100, 1) if total else 100.0
        return {"total_files": total, "done_files": done, "failed_files": failed,
                "percent": percent}

    def retry_failed(self, onboarding_uid: str, by: str = "ops-admin") -> int:
        """failed 文件重置 queued（done 不重跑）并向导 failed→initializing（SR §6.2）。"""
        task = self.task_of(onboarding_uid)
        with self._lock:
            reset = 0
            for f in task.files:
                if f.status == FILE_FAILED:
                    f.status = FILE_QUEUED
                    f.last_error = None
                    reset += 1
        self._on_transit(onboarding_uid, "failed", "initializing",
                         by=by, detail={"reset_files": reset})
        return reset

    def task_of(self, onboarding_uid: str) -> KbTask:
        """按向导查任务（1:1；未建任务即 40401）。"""
        task = next((t for t in self.tasks.values()
                     if t.onboarding_uid == onboarding_uid), None)
        if task is None:
            raise _not_found(f"该向导尚无初始化任务：{onboarding_uid}")
        return task
