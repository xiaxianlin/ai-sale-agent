# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练存储层测试（FR-TR-003，store 协议 + 内存实现；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：结果/校准台账读写回环与幂等索引；
- 异常：缺失键返回 None（不抛错，服务层据此判定未命中）；
- 并发：多线程取号唯一性（InMemoryDrillStore 线程安全口径）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from services.training.core.drill.casebank import build_demo_cases
from services.training.core.drill.service import CalibrationSample, DrillResult
from services.training.core.drill.store import InMemoryDrillStore


def make_result(result_id: str, trainee_id: int = 1, request_id: str = "req-1") -> DrillResult:
    """构造极简已评分结果（评分明细字段留空，专注存储语义）。"""
    return DrillResult(
        result_id=result_id,
        request_id=request_id,
        case_id=build_demo_cases(1)[0].case_id,
        trainee_id=trainee_id,
        answer="作答原文",
        status="scored",
        dims=(),
        total=0,
        comment="",
        gap_analysis=None,
        scoring_error="",
    )


def make_sample(calibration_id: str) -> CalibrationSample:
    return CalibrationSample(
        calibration_id=calibration_id,
        result_id="RES-0001",
        supervisor="主管甲",
        ai_scores={"product_accuracy": 60, "script": 60, "strategy": 60},
        supervisor_scores={"product_accuracy": 60, "script": 60, "strategy": 60},
        consistent=True,
        deviation={},
        total_deviation=0,
    )


class TestResultRoundtrip:
    """等价类：结果读写回环与按请求幂等索引。"""

    def test_save_and_get(self) -> None:
        store = InMemoryDrillStore()
        store.save_result(make_result("RES-0001"))
        loaded = store.get_result("RES-0001")
        assert loaded is not None and loaded.result_id == "RES-0001"

    def test_get_missing_returns_none(self) -> None:
        """异常：缺失键返回 None（服务层转 TR-2003 资源不存在）。"""
        assert InMemoryDrillStore().get_result("RES-404") is None

    def test_find_by_request(self) -> None:
        store = InMemoryDrillStore()
        store.save_result(make_result("RES-0001", trainee_id=7, request_id="req-a"))
        found = store.find_result_by_request(7, "req-a")
        assert found is not None and found.result_id == "RES-0001"
        assert store.find_result_by_request(8, "req-a") is None  # 会话隔离：他人请求不可见
        assert store.find_result_by_request(7, "req-b") is None

    def test_list_results_filters_trainee(self) -> None:
        store = InMemoryDrillStore()
        store.save_result(make_result("RES-0001", trainee_id=1))
        store.save_result(make_result("RES-0002", trainee_id=2))
        assert [r.result_id for r in store.list_results(1)] == ["RES-0001"]
        assert [r.result_id for r in store.list_results(2)] == ["RES-0002"]


class TestCalibrationRoundtrip:
    """等价类：校准台账按序追加。"""

    def test_save_and_list_in_order(self) -> None:
        store = InMemoryDrillStore()
        for i in range(3):
            store.save_calibration(make_sample(f"CAL-{i + 1:04d}"))
        assert [c.calibration_id for c in store.list_calibrations()] == [
            "CAL-0001",
            "CAL-0002",
            "CAL-0003",
        ]


class TestIdGeneration:
    """并发：取号线程安全（结果/校准 id 全局唯一递增）。"""

    def test_result_ids_unique_under_concurrency(self) -> None:
        store = InMemoryDrillStore()
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: store.next_result_id(), range(64)))
        assert len(set(ids)) == 64
        assert ids[0] == "RES-0001"

    def test_calibration_ids_sequence(self) -> None:
        store = InMemoryDrillStore()
        assert [store.next_calibration_id() for _ in range(2)] == ["CAL-0001", "CAL-0002"]
