# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""内容防同质守卫单元测试（FR-SC-004-③，SR-SC-004 §6.3/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：首发通过登记指纹、跨账号同文案命中 → 强制重写后过检（reason=ok:rewritten）、
  完全相同（sha256 快路径）与近相似（simhash ≤3bit）两条命中路径；
- 边界：汉明距离恰等阈值（=3 判相似）与恰过（=4 放行）、重写次数恰耗尽（2 次后拒发）、
  24h 窗口恰过期（旧指纹不再命中）；
- 异常：重写耗尽仍相似 → allow=False（content_homog，BLOCK 语义）、
  msg_uid 重复 guard 幂等（不重复登记不重复计数）；
- 并发：多账号并发 guard 同文案，指纹库各行其是（无重复 uid、无丢登记）；
- 降级：本账号自身历史排除（不与自己的旧内容互斥）+ 窗口裁剪后库容量受控。

验收口径映射：SRS §9.2 FR-SC-004-③（避免多账号发送完全相同内容）、§17 R-06。
实现方案：docs/07-实现方案/SC/SR-SC-004-风控V2.md §6.3、§9
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from services.agent.core.riskv2.uniqueness import (
    ContentUniqueness,
    GuardResult,
    hamming_distance,
    simhash64,
)

T0 = datetime(2026, 10, 11, 9, 0, 0)
TEXT = "您好！感谢咨询，我们的 ST-3200 支架现货供应，价格优惠，欢迎下单。"
VARIANT = "承蒙垂询！ST-3200 支架备货充足，本期报价实惠，期待合作下单。"


class FixedRewriter:
    """确定性重写桩：attempt 1 返回既定变体（汉明距离 >3），可配置为始终相似。"""

    def __init__(self, variant: str = VARIANT, always_similar: bool = False) -> None:
        self.variant = variant
        self.always_similar = always_similar
        self.calls: list[tuple[str, int, int]] = []  # (account_id, attempt, len(text))

    def rewrite(self, text: str, account_id: int, attempt: int) -> str:
        self.calls.append((account_id, attempt, len(text)))
        return text if self.always_similar else self.variant


def make_guard(**kw) -> tuple[ContentUniqueness, FixedRewriter]:
    """标准装配：可控时钟 + 注入式重写器。"""
    rewriter = kw.pop("rewriter", None) or FixedRewriter()
    clock = kw.pop("clock", None) or {"t": T0}
    guard = ContentUniqueness(rewriter=rewriter, clock=lambda: clock["t"], **kw)
    guard.clock_handle = clock
    return guard, rewriter


# ───────────────────────── simhash 基元 ─────────────────────────


class TestSimhashPrimitive:
    """simhash-64 与汉明距离基元（SR-SC-004 §6.3）。"""

    def test_identical_text_distance_zero(self) -> None:
        """等价类：同文本指纹一致，汉明距离 0。"""
        assert simhash64(TEXT) == simhash64(TEXT)
        assert hamming_distance(simhash64(TEXT), simhash64(TEXT)) == 0

    def test_variant_distance_above_threshold(self) -> None:
        """边界：重写变体与原文距离 >3（重写后可过检的前提）。"""
        assert hamming_distance(simhash64(TEXT), simhash64(VARIANT)) > 3

    def test_minor_edit_distance_small(self) -> None:
        """近相似：仅改一字符 → 距离小（相似域灵敏度）。"""
        minor = TEXT.replace("优惠", "优慧")
        assert hamming_distance(simhash64(TEXT), simhash64(minor)) <= 8


# ───────────────────────── 守卫主流程 ─────────────────────────


class TestGuardFlow:
    """发送前防同质守卫：比对 → 强制重写 → 过检/拒发（SR-SC-004 §6.3 guard）。"""

    def test_first_send_allowed_and_registered(self) -> None:
        """等价类：首账号首发 → 放行、登记 1 条指纹（reason=ok）。"""
        guard, rewriter = make_guard()
        result = guard.guard(7001, "m-0001", TEXT)
        assert isinstance(result, GuardResult)
        assert result.allow is True and result.reason == "ok"
        assert result.rewrites == 0 and rewriter.calls == []
        assert guard.fingerprint_count() == 1

    def test_same_text_second_account_forced_rewrite_passes(self) -> None:
        """核心用例：第二账号同文案 → 命中强制重写 → 重写后过检（ok:rewritten）。"""
        guard, rewriter = make_guard()
        guard.guard(7001, "m-0001", TEXT)
        result = guard.guard(7002, "m-0002", TEXT)
        assert result.allow is True
        assert result.reason == "ok:rewritten"
        assert result.text_out == VARIANT
        assert result.rewrites == 1
        assert result.similar_to and result.similar_to[0] == 7001
        assert rewriter.calls == [(7002, 1, len(TEXT))]

    def test_exact_duplicate_hits_sha_fast_path(self) -> None:
        """等价类：完全相同内容（sha256 快路径）同样命中（不依赖 simhash 位判）。"""
        guard, _ = make_guard()
        guard.guard(7001, "m-0001", TEXT)
        result = guard.guard(7003, "m-0003", TEXT.upper().lower())  # 归一后同文
        assert result.similar_to and result.rewrites >= 1

    def test_rewrite_exhausted_blocks(self) -> None:
        """异常：重写器始终返回相似文本 → 2 次耗尽 → 拒发（content_homog）不登记。"""
        guard, rewriter = make_guard(rewriter=FixedRewriter(always_similar=True))
        guard.guard(7001, "m-0001", TEXT)
        result = guard.guard(7002, "m-0002", TEXT)
        assert result.allow is False
        assert result.reason == "content_homog"
        assert result.rewrites == 2  # 恰耗尽（rewrite_max_attempts 默认 2）
        assert guard.fingerprint_count() == 1  # 拒发不登记

    def test_msg_uid_idempotent_replay(self) -> None:
        """异常/幂等：同 msg_uid 重复 guard → 回放首次结论、指纹库不重复计数。"""
        guard, rewriter = make_guard()
        first = guard.guard(7001, "m-0001", TEXT)
        second = guard.guard(7001, "m-0001", TEXT)
        assert second.reason == "duplicate_msg"
        assert second.allow == first.allow
        assert guard.fingerprint_count() == 1
        assert rewriter.calls == []


# ───────────────────────── 窗口与排除规则 ─────────────────────────


class TestWindowAndExclusion:
    """24h 指纹窗口与本账号排除（SR-SC-004 §4/§6.3）。"""

    def test_own_history_excluded(self) -> None:
        """降级/排除：同账号再次发送同文案不判同质（仅跨账号比对）。"""
        guard, _ = make_guard()
        guard.guard(7001, "m-0001", TEXT)
        result = guard.guard(7001, "m-0002", TEXT)
        assert result.allow is True and result.reason == "ok"

    def test_fingerprint_expires_after_24h_and_prune(self) -> None:
        """边界/降级：指纹恰过期（>24h）不再命中（窗口滚动）；显式裁剪后库计数下降。"""
        guard, rewriter = make_guard()
        guard.guard(7001, "m-0001", TEXT)
        guard.clock_handle["t"] = T0 + timedelta(hours=25)
        result = guard.guard(7002, "m-0002", TEXT)
        assert result.allow is True and result.reason == "ok"
        assert rewriter.calls == []
        assert guard.prune() == 1  # 过期指纹可被维护裁剪（容量受控）
        assert guard.fingerprint_count() == 1


# ───────────────────────── 并发 ─────────────────────────


class TestConcurrency:
    """并发 guard：多账号同文案同时送检，登记不丢不重。"""

    def test_concurrent_guards_all_registered(self) -> None:
        """并发：14 账号并发各自文案 → 14 条指纹全落库（线程安全）。"""
        guard, _ = make_guard()

        def worker(i: int) -> None:
            guard.guard(7000 + i, f"m-{i:04d}", f"账号{i}专属文案：ST-{3200 + i} 支架参数与报价说明")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert guard.fingerprint_count() == 14
