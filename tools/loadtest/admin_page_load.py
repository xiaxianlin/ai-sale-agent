# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""后台页面加载抽测脚本（NFR-PER-05 工程部分：常规操作页 ≤3s，迭代 1.3）。

设计（与 tools/loadtest/retrieval_load.py 同风格，页面池计时版）：
- 页面池：对注入的 ASGI app（联调/回归，httpx.ASGITransport 通道）或 base URL
  （生产对真实服务抽测）逐页计时 N 轮；
- 判定：复用 services.training.core.board.report.page_budget_evaluator 纯函数——
  逐页 P95 ≤ 预算（缺省 3000ms，页面级预算可覆盖）→ PASS，输出超标页面清单；
- Playwright 抽测（验收口径）复用同一判定函数，保证「脚本计时」与「浏览器抽测」
  同一验收口径（SR-TR-009 §6 PER-05 落点）。

用法（仓库根）：
  python3 tools/loadtest/admin_page_load.py --base-url http://127.0.0.1:8000 \
      --rounds 10 --budget-ms 3000 --report out/admin_page_load.json
  python3 tools/loadtest/admin_page_load.py --app services.training.api.board_api:create_board_app ...
退出码：0=PASS；1=FAIL（或参数错误）。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

# 服务包定位：src 布局下从仓库根解析（工具脚本独立于 pytest 运行的自举）
_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

import httpx  # noqa: E402  (sys.path 自举之后导入)
from services.training.core.board.report import (  # noqa: E402
    PageBudgetReport,
    page_budget_evaluator,
)

# ── 默认参数（口径来源：SRS NFR-PER-05 / SR-TR-006 §5④ / SR-TR-009 §6）──
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_PAGES: tuple[str, ...] = (
    "/api/v1/training/board/overview",
    "/api/v1/training/board/flags",
)
DEFAULT_ROUNDS = 10
DEFAULT_BUDGET_MS = 3000.0  # NFR-PER-05 硬线：常规操作页 ≤3s

#: 计时通道工厂（httpx.AsyncClient 及其子类均可注入；测试以 ASGI 通道替代真实端口）
ClientBuilder = Callable[..., httpx.AsyncClient]


def measure_pages(
    target: Any,
    pages: list[str],
    *,
    rounds: int = DEFAULT_ROUNDS,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    base_url: str = "http://testserver",
) -> dict[str, list[float]]:
    """页面池计时：对 ASGI app 或 base URL 逐页计时 rounds 轮。

    - target 为 ASGI 可调用对象 → ASGITransport 内存通道（联调/回归零端口依赖）；
      为字符串 → 视为 base URL（生产抽测通道）；
    - 失败请求（HTTP ≥400）不计样本：页面无样本即判失败（降级口径，不静默放过）。
    返回：路径 → 耗时样本（ms）列表。
    """
    if rounds < 1:
        raise ValueError(f"计时轮数须 ≥1：{rounds}")
    if not pages:
        raise ValueError("页面池不能为空")
    if isinstance(target, str):
        client: httpx.AsyncClient = httpx.AsyncClient(base_url=target, timeout=timeout_s)
    else:  # ASGI app（注入通道；ASGITransport 为异步传输，统一走 AsyncClient）
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target), base_url=base_url, timeout=timeout_s
        )
    samples: dict[str, list[float]] = {page: [] for page in pages}

    async def _measure() -> None:
        """计时主循环（async 传输通道；异常不计样本，页面以「无样本」判失败）。"""
        try:
            for _ in range(rounds):
                for page in pages:
                    t0 = time.perf_counter()
                    try:
                        resp = await client.get(page, headers=headers)
                    except httpx.HTTPError:
                        continue
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    if 200 <= resp.status_code < 400:
                        samples[page].append(elapsed_ms)
        finally:
            await client.aclose()

    asyncio.run(_measure())
    return samples


def run_page_load(
    *,
    app: Any | None = None,
    url: str | None = None,
    pages: list[str] | tuple[str, ...] = DEFAULT_PAGES,
    rounds: int = DEFAULT_ROUNDS,
    budget_ms: float = DEFAULT_BUDGET_MS,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
) -> PageBudgetReport:
    """执行页面池计时并给出 PASS/FAIL 判定与超标清单（NFR-PER-05 验收口径）。"""
    if (app is None) == (url is None):
        raise ValueError("须且仅须提供 app（ASGI）或 url（base URL）之一")
    target = app if app is not None else url
    page_list = list(pages)
    samples = measure_pages(target, page_list, rounds=rounds, headers=headers, timeout_s=timeout_s)
    return page_budget_evaluator(page_list, samples, budget_ms=budget_ms)


def load_app(spec: str) -> Any:
    """装载 ASGI app（``模块路径:工厂调用表达式``，如 ``pkg.mod:create_board_app()``）。"""
    module_path, _, expr = spec.partition(":")
    spec_loader = importlib.util.spec_from_file_location("_admin_page_load_target", module_path)
    if spec_loader is None or spec_loader.loader is None:
        raise ValueError(f"无法装载模块：{module_path}")
    module = importlib.util.module_from_spec(spec_loader)
    spec_loader.loader.exec_module(module)
    return eval(expr, {"app_module": module})  # noqa: S307  (工具入口，表达式来自命令行)


def summary_lines(report: PageBudgetReport, *, rounds: int) -> list[str]:
    """人读摘要（stdout 输出，中文）。"""
    verdict = "PASS" if report.passed else "FAIL"
    lines = [
        f"[页面加载抽测] 页面池 {len(report.results)} 页 × {rounds} 轮  预算 {report.budget_ms:.0f}ms（NFR-PER-05）",
    ]
    for page in report.results:
        mark = "PASS" if page.passed else "FAIL"
        lines.append(
            f"  [{mark}] {page.path}  P95 {page.p95_ms:.1f}ms / 预算 {page.budget_ms:.0f}ms"
            f"（样本 {page.sample_count}）"
        )
    lines.append(f"[判定] {verdict}")
    if report.over_budget_pages:
        lines.append("[超标页面清单] " + "、".join(page.path for page in report.over_budget_pages))
    return lines


def build_arg_parser() -> argparse.ArgumentParser:
    """命令行参数定义（默认值即 NFR-PER-05 验收口径）。"""
    parser = argparse.ArgumentParser(
        prog="admin_page_load",
        description="NFR-PER-05 后台页面加载抽测（页面池计时 → P95 → 3s 判定 + 超标清单）",
    )
    channel = parser.add_mutually_exclusive_group(required=True)
    channel.add_argument("--base-url", default=None, help="目标服务 base URL（生产抽测通道）")
    channel.add_argument("--app", default=None, help="ASGI app（模块:工厂表达式，联调通道）")
    parser.add_argument("--pages", default=",".join(DEFAULT_PAGES), help="逗号分隔页面池（默认看板页）")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="每页计时轮数（默认 10）")
    parser.add_argument("--budget-ms", type=float, default=DEFAULT_BUDGET_MS, help="加载预算 ms（默认 3000）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒（默认 10）")
    parser.add_argument("--report", default=None, help="可选：JSON 报告落盘路径（验收留档）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：解析参数 → 页面池计时 → 判定输出 → 可选落盘；退出码 0=PASS / 1=FAIL。"""
    args = build_arg_parser().parse_args(argv)
    pages = [p.strip() for p in args.pages.split(",") if p.strip()]
    try:
        report = run_page_load(
            app=load_app(args.app) if args.app else None,
            url=args.base_url,
            pages=pages,
            rounds=args.rounds,
            budget_ms=args.budget_ms,
            timeout_s=args.timeout,
        )
    except (ValueError, ImportError) as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 1
    for line in summary_lines(report, rounds=args.rounds):
        print(line)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[归档] 报告已写入 {report_path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
