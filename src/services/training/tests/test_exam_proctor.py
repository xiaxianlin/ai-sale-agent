# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""在线监考规则引擎单元测试（FR-TR-007，SR-TR-007 §6.2；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类）：
- 边界：切屏 2 次（阈值下）不命中 / 3 次恰命中；加权分恰=阈值进复核 / 差 1 不进；
- 异常（宁漏勿误）：单一弱信号（权重 1）不判嫌疑；
- 降级：摄像头关闭时 face_leave 事件忽略；
- 证据链：命中的 flag 携带原始事件与统计快照（可回放）。
"""
from __future__ import annotations

from services.training.core.exam.proctor import (
    ProctorConfig,
    ProctorEvent,
    ProctorSession,
)

# --------------------------------------------------------------------------- 事件构造助手


def blur(t: int) -> ProctorEvent:
    return ProctorEvent(t=t, type="blur")


def focus(t: int) -> ProctorEvent:
    return ProctorEvent(t=t, type="focus")


def paste(t: int, length: int) -> ProctorEvent:
    return ProctorEvent(t=t, type="paste", detail={"len": length})


def answer(t: int, *, elapsed_ms: int = 10000, correct: bool = True) -> ProctorEvent:
    return ProctorEvent(t=t, type="answer", detail={"elapsed_ms": elapsed_ms, "correct": correct})


# --------------------------------------------------------------------------- 边界：切屏规则


class TestSwitchWindowBoundary:
    """切屏规则阈值边界（SR §9 用例：监考阈值边界——2 次仅留痕、3 次命中）。"""

    def test_blur_below_limit_no_flag(self) -> None:
        """切屏 2 次（< 3）→ 不命中任何规则（事件仅留痕于会话统计）。"""
        session = ProctorSession()
        session.ingest([blur(1000), focus(3000), blur(5000), focus(7000)])
        assert session.evaluate() == []
        assert session.stats["switch_count"] == 2  # 留痕：统计可回放

    def test_blur_at_limit_hits_switch_window(self) -> None:
        """切屏 3 次（恰达 blur_limit）→ 命中 switch_window，权重 3。"""
        session = ProctorSession()
        session.ingest([blur(1000), focus(2000)] * 3)
        flags = session.evaluate()
        assert [f.rule for f in flags] == ["switch_window"]
        assert flags[0].weight == 3
        assert flags[0].stat["switch_count"] == 3

    def test_away_total_ms_hits(self) -> None:
        """累计离开时长 ≥ away_ms_limit（2 次但单次 40s）→ 命中（时长口径独立于次数）。"""
        session = ProctorSession()
        session.ingest([blur(0), focus(40_000)])  # 一次离开 40s ≥ 30s 阈值
        flags = session.evaluate()
        assert [f.rule for f in flags] == ["switch_window"]
        assert flags[0].stat["away_total_ms"] >= 30_000

    def test_away_total_ms_below_limit(self) -> None:
        """累计离开 20s < 30s → 不命中（时长等价类下界）。"""
        session = ProctorSession()
        session.ingest([blur(0), focus(20_000)])
        assert session.evaluate() == []


# --------------------------------------------------------------------------- 加权判定（宁漏勿误）


class TestWeightedVerdict:
    """交卷终判：cheat_score = Σ命中规则权重；≥ 阈值才嫌疑（弱信号宁漏勿误）。"""

    def test_weak_signal_alone_not_suspect(self) -> None:
        """仅 rapid_answer 命中（权重 1）→ cheat_score=1 < 5，不判嫌疑（SR §9 用例）。"""
        session = ProctorSession()
        for i in range(ProctorConfig().rapid_q_limit):
            session.ingest([answer(i * 1000, elapsed_ms=500)])
        score, suspect = session.finalize()
        assert score == 1
        assert suspect is False

    def test_score_below_threshold_not_suspect(self) -> None:
        """仅切屏命中（权重 3）→ 3 < 5：留痕不拦截，不影响判分。"""
        session = ProctorSession()
        session.ingest([blur(1000), focus(2000)] * 3)
        score, suspect = session.finalize()
        assert score == 3
        assert suspect is False

    def test_score_at_threshold_suspect(self) -> None:
        """切屏（3）+ 长粘贴（2）→ cheat_score=5 恰达阈值 → 嫌疑进复核。"""
        session = ProctorSession()
        session.ingest([blur(1000), focus(2000)] * 3)
        session.ingest([paste(9000, 80)])
        score, suspect = session.finalize()
        assert score == 5
        assert suspect is True

    def test_score_above_threshold_suspect(self) -> None:
        """5.5 等价类上探：切屏（3）+ 粘贴（2）+ rapid（1）= 6 > 5 → 嫌疑。"""
        session = ProctorSession()
        session.ingest([blur(1000), focus(2000)] * 3)
        session.ingest([paste(9000, 80)])
        for i in range(ProctorConfig().rapid_q_limit):
            session.ingest([answer(10_000 + i * 1000, elapsed_ms=500)])
        score, suspect = session.finalize()
        assert score == 6
        assert suspect is True

    def test_threshold_configurable(self) -> None:
        """阈值配置化：阈值降到 2 时，仅切屏（3）也判嫌疑（NFR-MAI-02 免发版调整）。"""
        session = ProctorSession(config=ProctorConfig(cheat_threshold=2))
        session.ingest([blur(1000), focus(2000)] * 3)
        _, suspect = session.finalize()
        assert suspect is True


# --------------------------------------------------------------------------- 其他规则与降级


class TestOtherRules:
    """粘贴 / 连交 / 摄像头降级规则。"""

    def test_paste_below_chars_no_hit(self) -> None:
        """粘贴 49 字（< 50 阈值）→ 不命中（中信号下界）。"""
        session = ProctorSession()
        session.ingest([paste(1000, 49)])
        assert session.evaluate() == []

    def test_paste_at_chars_hits(self) -> None:
        """粘贴 50 字（恰达阈值）→ 命中 paste_answer 权重 2。"""
        session = ProctorSession()
        flags = session.ingest([paste(1000, 50)])
        assert [f.rule for f in flags] == ["paste_answer"]
        assert flags[0].weight == 2

    def test_submit_burst_hits(self) -> None:
        """多题秒级连交且全对 ≥ 5 题 → 命中 submit_burst 权重 3。"""
        session = ProctorSession()
        for i in range(5):
            session.ingest([answer(i * 100, elapsed_ms=500, correct=True)])
        flags = session.evaluate()
        assert "submit_burst" in [f.rule for f in flags]

    def test_face_leave_ignored_when_camera_disabled(self) -> None:
        """降级：摄像头默认关闭 → face_leave 事件不计（SR §7 FACE_ENABLED=false）。"""
        session = ProctorSession()
        flags = session.ingest([ProctorEvent(t=0, type="face_leave", detail={"ms": 999_999})])
        assert flags == []
        _, suspect = session.finalize()
        assert suspect is False

    def test_evidence_chain_preserved(self) -> None:
        """证据链可回放：flag.evidence 含触发原始事件（SR §4 proctor_flags 结构）。"""
        session = ProctorSession()
        events = [blur(1000), focus(2000), blur(3000), focus(4000), blur(5000), focus(6000)]
        session.ingest(events)
        (flag,) = session.evaluate()
        assert {e.t for e in flag.evidence} >= {1000, 3000, 5000}
        assert all(e.type == "blur" for e in flag.evidence)
