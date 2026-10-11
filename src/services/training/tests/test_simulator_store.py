# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟会话存储单元测试（FR-TR-002，SR-TR-002 §8 幂等与并发；迭代 1.2 TDD 阶段①）。

五类场景覆盖（开发规范 §9.1）：
- 等价类：save/load 往返、重放索引读写；
- 边界：未知会话/未知重放键返回 None、同会话覆盖写；
- 异常：无文件/网络等外部依赖（内存实现无异常路径），由类型注解与锁保证；
- 并发：多线程并发 save 无丢失；
- 幂等：重放键首写为准（同键二次写入不覆盖）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from services.training.core.simulator.engine import CustomerState, SimSession
from services.training.core.simulator.persona import BUILT_IN_PERSONAS, BUILT_IN_SCENARIOS
from services.training.core.simulator.store import START_REPLAY_SCOPE, InMemorySimSessionStore


def make_session(session_id: str = "sim-t1") -> SimSession:
    """构造测试会话快照。"""
    return SimSession(
        session_id=session_id,
        persona_id="C-01",
        scenario_id="sc-bargain-moq",
        persona=BUILT_IN_PERSONAS["C-01"],
        scenario=BUILT_IN_SCENARIOS["sc-bargain-moq"],
        difficulty=2,
        effective_difficulty=2,
        max_rounds=20,
        state=CustomerState(price_stance=0.15, emotion=0, patience=100),
        transcript=[{"seq": 1, "role": "customer", "content": "开场", "state_snapshot": None}],
    )


class TestSessionCrud:
    """等价类 + 边界：会话存取。"""

    def test_save_load_roundtrip(self) -> None:
        """save → load 往返返回同一会话对象。"""
        store = InMemorySimSessionStore()
        session = make_session()
        store.save(session)
        assert store.load("sim-t1") is session
        assert store.count() == 1

    def test_load_unknown_returns_none(self) -> None:
        """边界：未知 session_id 返回 None（不抛错，引擎据此转 40401）。"""
        store = InMemorySimSessionStore()
        assert store.load("sim-not-exist") is None

    def test_save_overwrites_same_id(self) -> None:
        """边界：同 session_id 重复 save 覆盖写（引擎每轮推进后整体保存的口径）。"""
        store = InMemorySimSessionStore()
        first = make_session("sim-t1")
        second = make_session("sim-t1")
        second.rounds = 3
        store.save(first)
        store.save(second)
        assert store.count() == 1
        assert store.load("sim-t1").rounds == 3


class TestReplayIndex:
    """幂等：重放索引（request_id → 首次响应）。"""

    def test_replay_first_wins(self) -> None:
        """同 (scope, request_id) 二次写入不覆盖——幂等以首次响应为准。"""
        store = InMemorySimSessionStore()
        store.record_replay("sim-t1", "m1", {"round": 1, "customer_reply": "首次"})
        store.record_replay("sim-t1", "m1", {"round": 1, "customer_reply": "重放"})
        assert store.find_replay("sim-t1", "m1") == {"round": 1, "customer_reply": "首次"}

    def test_replay_unknown_returns_none(self) -> None:
        """边界：未记录过的重放键返回 None。"""
        store = InMemorySimSessionStore()
        assert store.find_replay("sim-t1", "m-none") is None

    def test_start_scope_isolated_from_session_scope(self) -> None:
        """边界：start 作用域（__start__）与会话作用域重放索引互不串扰。"""
        store = InMemorySimSessionStore()
        store.record_replay(START_REPLAY_SCOPE, "r1", {"session_id": "sim-a"})
        store.record_replay("sim-a", "r1", {"round": 1})
        assert store.find_replay(START_REPLAY_SCOPE, "r1") == {"session_id": "sim-a"}
        assert store.find_replay("sim-a", "r1") == {"round": 1}


class TestConcurrency:
    """并发：多线程写入线程安全。"""

    def test_parallel_saves_threadsafe(self) -> None:
        """8 线程并发 save 不同会话：全部落库、计数一致、无异常。"""
        store = InMemorySimSessionStore()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: store.save(make_session(f"sim-p{i}")), range(8)))
        assert store.count() == 8
        assert store.load("sim-p0") is not None and store.load("sim-p7") is not None
