# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟会话存储（FR-TR-002，SR-TR-002 §8 并发与幂等的存储侧契约）。

协议 :class:`SimSessionStore`：会话快照 save/load + 重放索引 find/record +
count（测试观测）。生产形态为 PG（exercise 表）+ Redis 快照
（sim:session:{exercise_id}，TTL 24h，断线续聊与崩溃恢复）；demo/测试注入
:class:`InMemorySimSessionStore`（线程安全，协议不变）。

幂等口径（SR-TR-002 §8）：同 (scope, request_id) 键以**首次写入为准**——
引擎重复提交同 request_id 时直接重放首次响应，不覆盖、不推进状态。
scope 取会话 session_id 或 :data:`START_REPLAY_SCOPE`（start 幂等作用域）。

注意：本模块运行期依赖 engine 的数据类型、engine 类型期引用本模块协议
（TYPE_CHECKING）——import 方向固定为 store → engine，不得反向。
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # 防循环导入：仅类型期引用
    from services.training.core.simulator.engine import SimSession

#: start 幂等作用域（start 无 session_id，以固定作用域键隔离重放索引）
START_REPLAY_SCOPE: str = "__start__"


class SimSessionStore(Protocol):
    """模拟会话存储协议（生产 PG + Redis 快照；demo 内存实现）。"""

    def save(self, session: "SimSession") -> None:
        """保存/覆盖会话快照（引擎每轮推进后整体保存）。"""
        ...

    def load(self, session_id: str) -> "SimSession | None":
        """按会话 ID 取快照；未知返回 None（引擎据此转 40401）。"""
        ...

    def find_replay(self, scope: str, request_id: str) -> dict[str, Any] | None:
        """按 (scope, request_id) 取首次响应快照；未记录返回 None。"""
        ...

    def record_replay(self, scope: str, request_id: str, response: dict[str, Any]) -> None:
        """记录响应快照；同键重复记录以首次为准（幂等）。"""
        ...

    def count(self) -> int:
        """会话总数（测试观测）。"""
        ...


class InMemorySimSessionStore:
    """内存会话存储（demo/测试）：线程安全，重放索引首写为准。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, "SimSession"] = {}
        self._replays: dict[tuple[str, str], dict[str, Any]] = {}

    def save(self, session: "SimSession") -> None:
        """覆盖保存会话快照。"""
        with self._lock:
            self._sessions[session.session_id] = session

    def load(self, session_id: str) -> "SimSession | None":
        """按会话 ID 取快照（未知返回 None）。"""
        with self._lock:
            return self._sessions.get(session_id)

    def find_replay(self, scope: str, request_id: str) -> dict[str, Any] | None:
        """按 (scope, request_id) 取首次响应快照（缺失返回 None）。"""
        with self._lock:
            cached = self._replays.get((scope, request_id))
            return dict(cached) if cached is not None else None

    def record_replay(self, scope: str, request_id: str, response: dict[str, Any]) -> None:
        """记录响应快照；同键以首次写入为准（幂等口径）。"""
        with self._lock:
            key = (scope, request_id)
            if key not in self._replays:
                self._replays[key] = dict(response)

    def count(self) -> int:
        """会话总数。"""
        with self._lock:
            return len(self._sessions)


__all__ = ["START_REPLAY_SCOPE", "SimSessionStore", "InMemorySimSessionStore"]
