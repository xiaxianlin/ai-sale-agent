# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""后台页面加载性能用例（NFR-PER-05，工程部分；TDD 阶段①，迭代 1.3）。

验收口径映射：SRS §NFR-PER-05「后台页面加载 ≤3 秒（常规操作页），Playwright 抽测」
——本组用例覆盖其工程基座：page_budget_evaluator 纯函数（P95 判定 + 超标页面清单）
与 tools/loadtest/admin_page_load.py 页面池计时器（ASGI app / URL 计时 N 轮，
与 kb 检索压测同风格；Playwright 抽测复用同一判定函数）。
"""
from __future__ import annotations

from typing import Any

import pytest

from services.training.core.board.report import PAGE_BUDGET_MS, page_budget_evaluator, p95
from tools.loadtest.admin_page_load import measure_pages, run_page_load

from .test_board_fakes import SleepApp


# --------------------------------------------------------------------------- 纯函数用例


def test_p95_linear_interpolation() -> None:
    """P95 线性插值口径：1..100 → 95.05（与 kb metrics.percentile 同法）。"""
    assert p95([float(v) for v in range(1, 101)]) == pytest.approx(95.05)


def test_p95_single_element_and_empty() -> None:
    """边界：单元素即其自身；空样本无定义（显式拒绝，调用方须先保证非空）。"""
    assert p95([42.0]) == 42.0
    with pytest.raises(ValueError):
        p95([])


def test_budget_evaluator_all_pass() -> None:
    """等价类：全部页面 P95 ≤ 3000ms → 通过、超标清单为空（NFR-PER-05 判定口径）。"""
    report = page_budget_evaluator(
        ["/board/overview", "/board/flags"],
        {"/board/overview": [100.0, 120.0, 110.0], "/board/flags": [80.0, 90.0, 85.0]},
    )
    assert report.budget_ms == PAGE_BUDGET_MS == 3000.0
    assert report.passed is True
    assert report.over_budget_pages == ()
    assert all(page.passed for page in report.results)


def test_budget_evaluator_over_budget_listing() -> None:
    """超标页面清单：P95 超预算页入选并按超标幅度降序，整体判定失败。"""
    report = page_budget_evaluator(
        ["/fast", "/slow", "/slower"],
        {"/fast": [100.0] * 5, "/slow": [3000.0, 3100.0, 3000.0, 3000.0, 3000.0],
         "/slower": [5000.0] * 5},
    )
    assert report.passed is False
    paths = [page.path for page in report.over_budget_pages]
    assert paths == ["/slower", "/slow"]  # 降序：最慢在前
    assert report.over_budget_pages[0].p95_ms == 5000.0


def test_budget_evaluator_empty_samples_fail() -> None:
    """降级：页面无计时样本 → 判失败（无样本即失败，同压测口径），不静默放过。"""
    report = page_budget_evaluator(["/no-samples"], {"/no-samples": []})
    assert report.passed is False
    assert [page.path for page in report.over_budget_pages] == ["/no-samples"]


def test_budget_evaluator_per_page_budget_override() -> None:
    """页面级预算覆盖全局默认：/heavy 允许 8s，/normal 仍按 3s 判定。"""
    report = page_budget_evaluator(
        [("/heavy", 8000.0), "/normal"],
        {"/heavy": [7000.0] * 3, "/normal": [4000.0] * 3},
    )
    by_path = {page.path: page for page in report.results}
    assert by_path["/heavy"].passed is True  # 7s ≤ 页面级 8s 预算
    assert by_path["/normal"].passed is False  # 4s > 全局 3s
    assert report.passed is False


# --------------------------------------------------------------------------- 计时器用例


def test_measure_pages_with_asgi_app_and_url() -> None:
    """页面池计时器：对注入的 ASGI app 计时 N 轮 → 每页样本数 = 轮数；base_url 字符串通道同法。"""
    app = SleepApp(delay_s=0.001, paths=("/a", "/b"))
    samples = measure_pages(app, ["/a", "/b"], rounds=3)
    assert set(samples) == {"/a", "/b"}
    assert all(len(v) == 3 for v in samples.values())
    assert all(v > 0 for values in samples.values() for v in values)
    assert app.hits == {"/a": 3, "/b": 3}  # 每页确实请求了 N 轮
    samples_url = measure_pages(app, ["/a"], rounds=2, base_url="http://testserver")
    assert len(samples_url["/a"]) == 2  # URL 通道（生产对真实服务计时）形态一致


def test_run_page_load_verdicts(tmp_path: Any) -> None:
    """端到端判定：预算 1µs → FAIL 且超标清单非空；宽松预算 → PASS。"""
    app = SleepApp(delay_s=0.001, paths=("/a", "/b"))
    failed = run_page_load(app=app, pages=["/a", "/b"], rounds=2, budget_ms=0.001)
    assert failed.passed is False
    assert {page.path for page in failed.over_budget_pages} == {"/a", "/b"}
    app2 = SleepApp(delay_s=0.001, paths=("/a", "/b"))
    passed = run_page_load(app=app2, pages=["/a", "/b"], rounds=2, budget_ms=60_000.0)
    assert passed.passed is True
    assert passed.over_budget_pages == ()
