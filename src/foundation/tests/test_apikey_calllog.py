"""知识产权归甲方所有（COM-IP-01）

FR-KB-031 调用日志单元测试（模型/内存存储/写入钩子/检索）。

覆盖（SR-KB-031 §6.4 + §9 日志完整性 + §5 查询口径）：
- 完整性：正常/401/429 各落一行，trace_id/consumer/method/path/client_ip/
  http_code/code/latency_ms 齐全；被拒且无法归因时 consumer='unknown'；
- 写入钩子：每次留痕触发（含被拒）；单钩子异常降级告警，不阻断留痕与其他钩子；
- 检索：按调用方/业务码/时间窗过滤 + 分页（≤100，04-接口设计 §1.4）；
- 模型不可变：入存储后禁止改写（全量留痕不可篡改口径）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from foundation.apikey.calllog import (
    UNKNOWN_CONSUMER,
    ApiCallLog,
    ApiCallLogger,
    InMemoryCallLogStore,
)


class FakeClock:
    """可推进假时钟（时间窗检索的确定性 created_at 来源）。"""

    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 9, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def logger(clock: FakeClock) -> ApiCallLogger:
    return ApiCallLogger(clock=clock)


def record_mixed(logger: ApiCallLogger) -> None:
    """依次留痕：成功 / 401（无法归因）/ 429（可归因）——SR §9 日志完整性三例。"""
    logger.record(trace_id="t-ok", consumer="agent-core", method="POST", path="/api/v1/knowledge/search",
                  client_ip="10.0.0.8", http_code=200, code=0, latency_ms=12)
    logger.record(trace_id="t-401", consumer=UNKNOWN_CONSUMER, method="POST", path="/api/v1/knowledge/search",
                  client_ip="10.0.0.8", http_code=401, code=40101, latency_ms=1)
    logger.record(trace_id="t-429", consumer="agent-core", method="POST", path="/api/v1/knowledge/search",
                  client_ip="10.0.0.8", http_code=429, code=42901, latency_ms=2)


# --------------------------------------------------------------------------- 完整性


class TestRecord:
    def test_record_completeness_success_and_rejected(self, logger: ApiCallLogger) -> None:
        """正常/401/429 各落一行且字段齐全；被拒无法归因记 'unknown'（SR §6.4）。"""
        record_mixed(logger)
        entries = logger.entries()
        assert len(entries) == 3
        assert [e.code for e in entries] == [0, 40101, 42901]
        assert [e.http_code for e in entries] == [200, 401, 429]
        assert [e.consumer for e in entries] == ["agent-core", UNKNOWN_CONSUMER, "agent-core"]
        for entry in entries:
            assert entry.trace_id and entry.method == "POST"
            assert entry.path == "/api/v1/knowledge/search"
            assert entry.client_ip == "10.0.0.8"
            assert entry.latency_ms >= 0 and entry.created_at is not None
        assert [e.id for e in entries] == [1, 2, 3]  # 行号由存储顺序分配

    def test_record_generates_trace_id_and_defaults(self, logger: ApiCallLogger) -> None:
        """trace_id 缺失自动生成（04 §1.4 每行可追溯）；默认未知消费方。"""
        entry = logger.record(method="GET", path="/api/v1/ping")
        assert entry.trace_id and entry.consumer == UNKNOWN_CONSUMER

    def test_record_returns_stored_copy_with_id(self, logger: ApiCallLogger) -> None:
        """record 返回入存储后的条目（携带存储分配 id，便于调用方关联）。"""
        entry = logger.record(trace_id="t-1", consumer="c1")
        assert entry.id == 1
        assert logger.store.query(consumer="c1")[0].trace_id == "t-1"


# --------------------------------------------------------------------------- 写入钩子（含被拒）


class TestHooks:
    def test_hook_fired_for_every_entry_including_rejected(self, logger: ApiCallLogger) -> None:
        """钩子对每次留痕回调（成功与 401/429 被拒一视同仁，SR §6.4 联动口径）。"""
        seen: list[ApiCallLog] = []
        logger.add_hook(seen.append)
        record_mixed(logger)
        assert [e.code for e in seen] == [0, 40101, 42901]

    def test_hook_failure_degrades_without_blocking(self, logger: ApiCallLogger) -> None:
        """单钩子抛异常：降级告警，留痕与其他钩子不受影响（与 rbac 审计降级同口径）。"""
        seen: list[ApiCallLog] = []

        def bad_hook(entry: ApiCallLog) -> None:
            raise RuntimeError("钩子故障（联调环境）")

        logger.add_hook(bad_hook)
        logger.add_hook(seen.append)
        entry = logger.record(trace_id="t-x", consumer="c1")
        assert len(logger.entries()) == 1 and logger.entries()[0].id == entry.id
        assert [e.trace_id for e in seen] == ["t-x"]

    def test_remove_hook(self, logger: ApiCallLogger) -> None:
        """钩子可移除（测试隔离）。"""
        seen: list[ApiCallLog] = []
        hook = seen.append
        logger.add_hook(hook)
        logger.record(trace_id="t-1")
        logger.remove_hook(hook)
        logger.record(trace_id="t-2")
        assert [e.trace_id for e in seen] == ["t-1"]


# --------------------------------------------------------------------------- 检索（SR §5 查询口径）


class TestQuery:
    def test_query_by_consumer_and_code(self, logger: ApiCallLogger) -> None:
        """按调用方 / 业务码过滤（验收「调用日志可查」检索维度）。"""
        record_mixed(logger)
        assert [e.code for e in logger.query(consumer="agent-core")] == [0, 42901]
        assert [e.consumer for e in logger.query(code=40101)] == [UNKNOWN_CONSUMER]
        assert logger.query(consumer="nobody") == []

    def test_query_by_time_window(self, logger: ApiCallLogger, clock: FakeClock) -> None:
        """按时间窗过滤（since/until 闭区间，created_at 为应用生成时间）。"""
        logger.record(trace_id="t-1", consumer="c1")
        clock.advance(60)
        logger.record(trace_id="t-2", consumer="c1")
        since = clock() - timedelta(seconds=30)
        assert [e.trace_id for e in logger.query(since=since)] == ["t-2"]
        until = clock() - timedelta(seconds=30)
        assert [e.trace_id for e in logger.query(until=until)] == ["t-1"]

    def test_query_pagination_and_cap(self, logger: ApiCallLogger) -> None:
        """分页 page/page_size 且页大小钳制 ≤100（04 §1.4）。"""
        for i in range(7):
            logger.record(trace_id=f"t-{i}", consumer="c1")
        page1 = logger.query(page=1, page_size=3)
        page3 = logger.query(page=3, page_size=3)
        assert [e.trace_id for e in page1] == ["t-0", "t-1", "t-2"]
        assert [e.trace_id for e in page3] == ["t-6"]
        assert len(logger.query(page_size=500)) == 7  # 超上限被钳制为 100 → 全量返回


# --------------------------------------------------------------------------- 存储与模型


class TestStoreAndModel:
    def test_store_append_assigns_sequential_ids(self) -> None:
        """内存存储：追加分配连续行号；iter_entries 升序；__len__ 计数。"""
        store = InMemoryCallLogStore()
        base = dict(trace_id="t", consumer="c", method="GET", path="/p")
        assert store.append(ApiCallLog(**base)) == 1
        assert store.append(ApiCallLog(**base)) == 2
        assert len(store) == 2
        assert [e.id for e in store.iter_entries()] == [1, 2]

    def test_model_frozen_immutable(self) -> None:
        """模型不可变：入存储后禁止改写（全量留痕防篡改的应用层口径）。"""
        entry = ApiCallLog(trace_id="t", consumer="c", method="GET", path="/p")
        with pytest.raises(ValidationError):
            entry.http_code = 500  # type: ignore[misc]
