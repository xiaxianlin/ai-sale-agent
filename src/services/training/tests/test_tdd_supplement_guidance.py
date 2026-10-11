# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""引导式交互 TDD 补齐测试（NFR-USA-03 / SR-NFR-USA-03 §9 之外的五类场景补强）。

按 TDD 新规五类场景审计既有 test_guidance_config/state/service/api 后补齐：
- 等价类：注册表重复注册同一配置（含同对象）必须拒绝——「版本单调递增」无幂等例外；
- 边界：已看主版本高于当前主版本（配置降级方向）永不重显的方向性判定；
- 异常：localStorage 载荷损坏（major=null / major=非数字）的容错口径——
  文档契约承诺抛 ValueError（API 层据此映射 40001 降级，而非 500）；
- 并发：注册表等版本并发注册恰一胜者；双端并发上报终态覆盖写恰一行；
- 降级：迟到上报（改版后旧页面才回调）按所看版本如实记录、重显门控自然收敛。

KPI 映射：NFR-USA-03 引导式交互（克制原则：一切可跳过、主版本升级重显一次）——
门控方向性与损坏载荷降级路径直接守护「回访用户不被打扰、引导层故障不产生 500」。

依据文档：docs/07-实现方案/NFR/SR-NFR-USA-03-引导式交互.md §2、§4、§6.1、§6.2、§9；
         docs/03-技术文档/02-开发规范.md §3。
"""
from __future__ import annotations

import threading

import pytest

from services.training.core.guidance.config import (
    FIRST_LOGIN_TOUR,
    TourRegistry,
    TourVersionError,
)
from services.training.core.guidance.service import GuidanceService
from services.training.core.guidance.state import (
    InMemoryGuidanceStateStore,
    from_localstorage_payload,
    should_show,
)

#: 任一合法配置快照（与内置导览同构，便于并发/重复注册构造）
_ANY_CONFIG = FIRST_LOGIN_TOUR


# --------------------------------------------------------------------------- 并发/等价类：注册表版本守门
# KPI 映射：NFR-USA-03 §6.1——版本回滚/静默覆盖会让「主版本重显」门控永久失灵。


class TestRegistryConcurrencyAndIdempotence:
    def test_concurrent_equal_version_registration_exactly_one_winner(self) -> None:
        """并发：8 线程并发注册同 (tour_id, version) → 恰 1 成功，其余 TourVersionError。"""
        registry = TourRegistry()
        successes: list[int] = []
        rejections: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()  # 同步起跑，最大化竞态窗口
            try:
                registry.register(_ANY_CONFIG)
                won = 1
            except TourVersionError:
                won = 0
            with lock:
                (successes if won else rejections).append(index)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(successes) == 1 and len(rejections) == 7
        assert registry.tour_ids() == ["first-login"]  # 注册表结构无重复行

    def test_reregister_identical_config_rejected(self) -> None:
        """等价类：同一配置对象重复注册（同版本再入册）也必须拒绝——无幂等注册例外。"""
        registry = TourRegistry([_ANY_CONFIG])
        with pytest.raises(TourVersionError, match="单调递增"):
            registry.register(_ANY_CONFIG)


# --------------------------------------------------------------------------- 并发/等价类：终态上报覆盖写
# KPI 映射：NFR-USA-03 §6.2——多端并发上报以最后一写为准，恰一行（不做追加账本）。


class TestReportStateConcurrency:
    def test_concurrent_report_done_and_skipped_lands_single_row(self) -> None:
        """并发：同 (trainee, tour) 两端同时上报 done / skipped → 覆盖写恰一行且为终态。"""
        store = InMemoryGuidanceStateStore()
        service = GuidanceService(states=store)
        barrier = threading.Barrier(2)

        def report(outcome: str) -> None:
            barrier.wait()
            service.report_tour_state("first-login", 42, outcome)  # type: ignore[arg-type]

        threads = [threading.Thread(target=report, args=(name,)) for name in ("done", "skipped")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert store.size() == 1  # 恰一行：不因并发产生重复记录
        state = store.load("first-login", 42)
        assert state is not None and state.dismissed is True  # 两个方向都是终态
        assert should_show(store, FIRST_LOGIN_TOUR, 42) is False  # 终态后不再自动弹出


# --------------------------------------------------------------------------- 降级/边界：迟到上报与门控方向性
# KPI 映射：NFR-USA-03 §6.2——迟到上报与版本方向性是「重显一次」收敛判定的两个斜边。


class TestLateReportAndGateDirection:
    def test_late_report_older_major_reshows_once_then_converges(self) -> None:
        """降级：迟到上报旧主版本（0.9）→ 如实落库并重显一次；补报当前版本后收敛不再弹。"""
        store = InMemoryGuidanceStateStore()
        service = GuidanceService(states=store)
        service.report_tour_state("first-login", 7, "skipped", seen_version="0.9")
        assert should_show(store, FIRST_LOGIN_TOUR, 7) is True  # 已看主版本 0 < 1：改版重显
        service.report_tour_state("first-login", 7, "done", seen_version="1.0")
        assert should_show(store, FIRST_LOGIN_TOUR, 7) is False  # 已看过当前主版本：收敛

    def test_seen_version_newer_major_never_reshows(self) -> None:
        """边界：已看主版本高于当前配置主版本（配置降级方向）→ 恒不重显（方向性单边判定）。"""
        store = InMemoryGuidanceStateStore()
        service = GuidanceService(states=store)
        service.report_tour_state("first-login", 8, "done", seen_version="2.0")
        receipt = service.report_tour_state("first-login", 8, "done", seen_version="2.0")
        assert receipt["should_show"] is False
        assert should_show(store, FIRST_LOGIN_TOUR, 8) is False


# --------------------------------------------------------------------------- 异常/降级：localStorage 载荷损坏容错
# KPI 映射：NFR-USA-03 §4——损坏载荷按 ValueError 降级（API 40001 / 前端弃缓存重导览），
# 绝不以 500 形态漏出（引导层故障不产生用户可见错误，SR §8）。


class TestCorruptedLocalstoragePayload:
    def test_non_numeric_major_degrades_to_valueerror(self) -> None:
        """异常：major="abc"（损坏 JSON 手工篡改）→ ValueError（容错契约口径，非静默接受）。"""
        with pytest.raises(ValueError):  # int("abc") 的 ValueError：API 层据此降级 40001
            from_localstorage_payload(1, "first-login", {"major": "abc"})

    def test_null_major_raises_valueerror_per_contract(self) -> None:
        """异常：major=null（JSON null）→ 按容错契约抛 ValueError（2026-10-10 红→绿修复）。"""
        with pytest.raises(ValueError, match="major"):
            from_localstorage_payload(1, "first-login", {"major": None})
