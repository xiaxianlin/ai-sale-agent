# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-KB-031 API 调用日志（全量留痕，含被拒请求）——平台基座实现。

依据：docs/07-实现方案/KB/SR-KB-031-API鉴权与配额.md
- §6.4 调用日志：结构化全量留痕——401/403/429 被拒调用同样落一行（consumer 记
  'unknown'，暴力探测审计线索）；trace_id 贯穿（04-接口设计 §1.4）；
- §4 数据模型 api_call_log（本文件为其内存形态 ApiCallLog；PG 批量落库随服务侧落地）；
- §5 查询 API 口径：按调用方/时间/结果码检索，分页 ≤100（04 §1.4）。

平台基座范围：日志模型 + 内存存储 + 写入钩子（依赖方 dependency.py 在每次
鉴权判定后调用 record，成功与被拒均留痕）；异步批量刷库（有界队列 + flusher）
为生产形态，随 kb-service 落地，钩子机制保持两侧写入路径一致。
"""

from __future__ import annotations

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field

_LOGGER = logging.getLogger(__name__)

UNKNOWN_CONSUMER: str = "unknown"
"""被拒调用的消费方占位（密钥无效/缺失时无法归因到调用方，SR §4 注释）。"""

MAX_PAGE_SIZE: int = 100
"""分页上限（04-接口设计 §1.4）。"""


class ApiCallLog(BaseModel):
    """单次 API 调用留痕（api_call_log 表行的内存形态，SR §4）。

    全量留痕口径：成功（http_code=200/code=0）与被拒（401/40101、403/40301、
    429/42901）各落一行；id 由存储分配（等价 PG IDENTITY）。
    """

    model_config = ConfigDict(frozen=True)

    id: int = 0                                    # 行号（存储分配；0=未入存储）
    trace_id: str                                  # 全链路追踪（X-Trace-Id 透传或新生成，04 §1.4）
    consumer: str                                  # 调用方；被拒且无法归因时记 'unknown'
    method: str                                    # HTTP 方法
    path: str                                      # 请求路径
    client_ip: str | None = None                   # 来源 IP（结构化定位，SR §4）
    http_code: int = 200                           # HTTP 状态码（401/429 验收证据列）
    code: int = 0                                  # 业务码（0/40101/40301/42901/…）
    latency_ms: int = 0                            # 鉴权+判定耗时（毫秒）
    created_at: datetime | None = None             # 留痕时间（UTC 感知；存储侧应用生成）


# --------------------------------------------------------------------------- 存储抽象


class CallLogStore(ABC):
    """调用日志存储抽象：仅追加 + 分页检索（repo 层无 UPDATE/DELETE，全量留痕不可改写）。"""

    @abstractmethod
    def append(self, entry: ApiCallLog) -> int:
        """追加一行（分配并写回 id），返回行号。"""

    @abstractmethod
    def iter_entries(self) -> Iterator[ApiCallLog]:
        """按 id 升序迭代全部行（对账/导出用）。"""

    @abstractmethod
    def query(
        self,
        *,
        consumer: str | None = None,
        code: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> list[ApiCallLog]:
        """检索：调用方/业务码/时间窗过滤 + 分页（≤100，04 §1.4）。"""


class InMemoryCallLogStore(CallLogStore):
    """内存存储实现（单元测试与本地开发用；线程安全，等价 PG 批量 INSERT 的验收替身）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[ApiCallLog] = []

    def append(self, entry: ApiCallLog) -> int:
        with self._lock:
            seq = len(self._entries) + 1
            stored = entry.model_copy(update={"id": seq})
            self._entries.append(stored)
            return seq

    def iter_entries(self) -> Iterator[ApiCallLog]:
        with self._lock:
            return iter(tuple(self._entries))  # 快照迭代

    def query(
        self,
        *,
        consumer: str | None = None,
        code: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> list[ApiCallLog]:
        size = min(max(1, page_size), MAX_PAGE_SIZE)
        current_page = max(1, page)
        with self._lock:
            snapshot = tuple(self._entries)
        matched = [
            e for e in snapshot
            if (consumer is None or e.consumer == consumer)
            and (code is None or e.code == code)
            and (since is None or (e.created_at is not None and e.created_at >= since))
            and (until is None or (e.created_at is not None and e.created_at <= until))
        ]
        start = (current_page - 1) * size
        return list(matched[start : start + size])

    def __len__(self) -> int:
        """已留痕行数（测试观测用）。"""
        with self._lock:
            return len(self._entries)


# --------------------------------------------------------------------------- 写入器（模型 + 存储 + 钩子）


CallLogHook = Callable[[ApiCallLog], None]
"""写入钩子：每留痕一行即回调（联动审计链/指标/告警；含被拒请求）。"""


class ApiCallLogger:
    """调用日志写入器：record 落存储 + 触发钩子；query 委托存储分页检索。

    钩子失败降级：单个钩子抛异常只记告警日志、不影响留痕与其他钩子
    （与 rbac._audit_deny 同口径——治理留痕失败不改变请求处理结论，可查可追）。
    """

    def __init__(
        self,
        store: CallLogStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        hooks: Iterable[CallLogHook] = (),
    ) -> None:
        self._store = store if store is not None else InMemoryCallLogStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._hooks: list[CallLogHook] = list(hooks)
        self._hook_lock = threading.Lock()

    @property
    def store(self) -> CallLogStore:
        """底层存储（检索/导出只读入口）。"""
        return self._store

    def add_hook(self, hook: CallLogHook) -> None:
        """注册写入钩子（幂等追加；测试与审计联动入口）。"""
        with self._hook_lock:
            if hook not in self._hooks:
                self._hooks.append(hook)

    def remove_hook(self, hook: CallLogHook) -> None:
        """移除写入钩子（测试隔离）。"""
        with self._hook_lock:
            if hook in self._hooks:
                self._hooks.remove(hook)

    def record(
        self,
        *,
        trace_id: str = "",
        consumer: str = UNKNOWN_CONSUMER,
        method: str = "",
        path: str = "",
        client_ip: str | None = None,
        http_code: int = 200,
        code: int = 0,
        latency_ms: int = 0,
    ) -> ApiCallLog:
        """留痕一行（成功与被拒统一入口）：落存储 → 触发钩子 → 返回入存储后的条目。

        trace_id 为空时自动生成 uuid（保证每行可追溯，04 §1.4）。
        """
        entry = ApiCallLog(
            trace_id=trace_id or str(uuid.uuid4()),
            consumer=consumer or UNKNOWN_CONSUMER,
            method=method,
            path=path,
            client_ip=client_ip,
            http_code=http_code,
            code=code,
            latency_ms=max(0, int(latency_ms)),
            created_at=self._clock(),
        )
        seq = self._store.append(entry)
        stored = entry.model_copy(update={"id": seq})
        self._fire_hooks(stored)
        return stored

    def query(
        self,
        *,
        consumer: str | None = None,
        code: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> list[ApiCallLog]:
        """检索入口（验收「调用日志可查」：按调用方/时间/结果码，SR §5）。"""
        return self._store.query(
            consumer=consumer, code=code, since=since, until=until, page=page, page_size=page_size
        )

    def entries(self) -> list[ApiCallLog]:
        """全量快照（测试断言用；生产走分页 query）。"""
        return list(self._store.iter_entries())

    def _fire_hooks(self, entry: ApiCallLog) -> None:
        """顺序触发钩子；单钩子异常降级为告警（不阻断留痕链路）。"""
        with self._hook_lock:
            hooks = tuple(self._hooks)
        for hook in hooks:
            try:
                hook(entry)
            except Exception as exc:  # 有意捕获：留痕结论优先，钩子故障仅告警（可查可追）
                _LOGGER.warning("调用日志钩子执行失败（留痕不受影响）: %s", exc)
