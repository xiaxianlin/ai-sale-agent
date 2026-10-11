# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""拟人化输入器单元测试（FR-AG-002 + NFR-SEC-11，SR-AG-002 §6.5）。

覆盖：
- 计划结构：逐字延迟数 == 字符数、文本保真、空文本退化；
- 分布区间可断言：逐字延迟落 3-5 字符/秒换算区间 [200, 333.4]ms（1000 次采样全量）；
- 随机停顿：时长落 [150, 800]ms，频率逼近配置 pause_prob；
- 对数正态参数化签名：延迟分布右偏（mean > median，lognormal 采样特征）；
- 确定性：同种子 → 同计划（可重放）。

实现方案：docs/07-实现方案/AG/SR-AG-002-浏览器自动化.md §6.5、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import random
import statistics

from services.agent.core.driver import TypingPlan
from services.agent.core.engine import HumanTyper

TEXT = "您好，感谢询盘！Our MOQ is 100pcs. 请查收报价单。"


def make_typer(**kwargs: float) -> HumanTyper:
    """默认装配：固定种子（确定性采样）。"""
    return HumanTyper(rng=random.Random(20261011), **kwargs)


def test_plan_char_delays_length_matches_text() -> None:
    """逐字延迟数 == 字符数（逐字输入语义，不做整段粘贴）。"""
    plan = make_typer().plan(TEXT)
    assert len(plan.char_delays_ms) == len(TEXT)


def test_plan_text_preserved() -> None:
    """计划保真：plan.text 与输入逐字节一致（回删重打后最终文本不变）。"""
    plan = make_typer().plan(TEXT)
    assert plan.text == TEXT


def test_char_delays_within_cps_interval() -> None:
    """分布区间：1000 次采样 × 全部逐字延迟均落 [200, 333.4]ms（3-5 字符/秒，NFR-SEC-11）。"""
    typer = make_typer()
    lo, hi = 1000.0 / 5.0 - 1e-6, 1000.0 / 3.0 + 0.4  # 200ms .. 333.33ms（浮点余量）
    for _ in range(1000):
        plan = typer.plan("abcdefghijklmnopqrstuvwxyz")
        assert plan.char_delays_ms, "延迟序列不得为空"
        assert all(lo <= d <= hi for d in plan.char_delays_ms)


def test_pauses_within_range_and_frequency() -> None:
    """随机停顿：时长落 [150, 800]ms；频率逼近 pause_prob=0.10（3000 字符样本）。"""
    typer = make_typer()
    total_chars = 0
    total_pauses = 0
    for _ in range(100):
        plan = typer.plan("x" * 30)
        for _idx, dur in plan.pauses:
            assert 150.0 <= dur <= 800.0
        total_chars += 30
        total_pauses += len(plan.pauses)
    freq = total_pauses / total_chars
    assert 0.08 <= freq <= 0.12  # p=0.10 的 5σ 邻域


def test_lognormal_signature_mean_above_median() -> None:
    """对数正态签名：逐字延迟右偏（mean > median），证明按对数正态参数化采样。"""
    typer = HumanTyper(rng=random.Random(7))
    delays: list[float] = []
    for _ in range(300):
        delays.extend(typer.plan("s" * 20).char_delays_ms)
    assert statistics.mean(delays) > statistics.median(delays)


def test_plan_deterministic_with_seed() -> None:
    """确定性：同种子两次规划完全一致；异种子几乎必然不同。"""
    a = HumanTyper(rng=random.Random(42)).plan(TEXT)
    b = HumanTyper(rng=random.Random(42)).plan(TEXT)
    c = HumanTyper(rng=random.Random(43)).plan(TEXT)
    assert (a.char_delays_ms, a.pauses) == (b.char_delays_ms, b.pauses)
    assert (a.char_delays_ms, a.pauses) != (c.char_delays_ms, c.pauses)


def test_plan_empty_text_degenerates() -> None:
    """空文本退化：零延迟零停顿，不抛异常（防御式口径）；
    TypingPlan.total_ms 为逐字延迟与停顿之和（执行时长预算依据）。"""
    plan = make_typer().plan("")
    assert plan.char_delays_ms == ()
    assert plan.pauses == ()
    assert plan.total_ms == 0.0
    sample = TypingPlan(text="ab", char_delays_ms=(200.0, 300.0), pauses=((0, 500.0),))
    assert sample.total_ms == 1000.0
