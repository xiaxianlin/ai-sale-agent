# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""在线监考规则引擎（FR-TR-007，SR-TR-007 §6.2：事件 → 证据链 → 加权规则 → 阈值终判）。

判定口径（证据链 + 加权规则 + 人工复核，demo 为事件规则引擎——生产前端 useProctor.ts 采集）：
- **规则与权重**：switch_window(3 切屏) / paste_answer(2 长文本粘贴) /
  rapid_answer(1 作答过快，弱信号) / submit_burst(3 多题秒级连交且全对) /
  face_leave(3 人脸离开，摄像头开启时才生效，默认关)；
- **宁漏勿误**：cheat_score = Σ 未裁决命中规则权重，仅当 ≥ cheat_threshold（默认 5）
  才判嫌疑进人工复核；低于阈值只留痕不影响判分（误判由复核 overturned 通道兜底纠正）；
- **证据链可回放**：命中 flag 携带触发原始事件切片与统计快照（SR §4 proctor_flags 结构）；
- **阈值全配置化**（NFR-MAI-02）：:class:`ProctorConfig` 构造期注入，调整免发版。

依据文档：docs/07-实现方案/TR/SR-TR-007-考试认证系统.md §6.2、§7、§9。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- 配置（SR §7 缺省值）


@dataclass(frozen=True)
class ProctorConfig:
    """监考阈值配置（SR §7 TR_PROCTOR_* 配置族的进程内形态，构造期注入）。"""

    blur_limit: int = 3
    """切屏次数阈值（≥ 命中 switch_window 次数口径）。"""

    away_ms_limit: int = 30_000
    """累计离开时长阈值（毫秒，≥ 命中 switch_window 时长口径）。"""

    paste_chars: int = 50
    """粘贴长文本阈值（字符，≥ 命中 paste_answer）。"""

    rapid_ms: int = 3_000
    """单题作答用时阈值（毫秒，< 计入过快）。"""

    rapid_q_limit: int = 5
    """过快作答题数阈值（≥ 命中 rapid_answer，权重 1 弱信号）。"""

    burst_q_limit: int = 5
    """秒级连交且全对的连击题数阈值（≥ 命中 submit_burst）。"""

    burst_gap_ms: int = 500
    """连击判定的事件间隔上限（毫秒，超过即断连）。"""

    cheat_threshold: int = 5
    """嫌疑加权分阈值（≥ 才判嫌疑进复核，SR §7 TR_PROCTOR_CHEAT_THRESHOLD）。"""

    face_enabled: bool = False
    """摄像头监考开关（默认关；开启后 face_leave 规则才生效）。"""

    face_ms_limit: int = 60_000
    """人脸离开时长阈值（毫秒）。"""


# --------------------------------------------------------------------------- 事件与证据链


@dataclass(frozen=True)
class ProctorEvent:
    """单条监考事件（前端 useProctor 上报形态：t 毫秒时间戳 + 类型 + 明细）。"""

    t: int
    """事件时间戳（epoch 毫秒；会话内单调）。"""

    type: str
    """事件类型：blur/focus/hide/show/paste/answer/submit/face_leave。"""

    detail: dict[str, Any] = field(default_factory=dict)
    """事件明细（paste.len / answer.elapsed_ms+correct / face_leave.ms 等）。"""


@dataclass(frozen=True)
class ProctorFlag:
    """命中规则 flag（SR §4 proctor_flags 元素结构：规则/权重/原始事件证据/统计快照）。"""

    rule: str
    """规则名。"""

    weight: int
    """规则权重（即作弊嫌疑分）。"""

    evidence: tuple[ProctorEvent, ...]
    """触发规则的原始事件切片（证据链，可回放）。"""

    stat: dict[str, Any]
    """命中时刻的统计快照（如 {switch_count, away_total_ms}）。"""

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（API / exam.proctor_flags JSONB 落库口径）。"""
        return {
            "rule": self.rule,
            "weight": self.weight,
            "evidence": [
                {"t": e.t, "type": e.type, "detail": dict(e.detail)} for e in self.evidence
            ],
            "stat": dict(self.stat),
        }


# --------------------------------------------------------------------------- 会话规则引擎


class ProctorSession:
    """单场考试的监考会话：事件累计 → 规则评估 → 加权终判。

    demo 为进程内实现（生产：Redis 会话内累计 + 落库对账，语义一致）。
    """

    #: 规则名 → 权重（弱信号低权重只累计，宁漏勿误）
    RULE_WEIGHTS: dict[str, int] = {
        "switch_window": 3,
        "paste_answer": 2,
        "rapid_answer": 1,
        "submit_burst": 3,
        "face_leave": 3,
    }

    def __init__(self, config: ProctorConfig | None = None) -> None:
        self._config = config if config is not None else ProctorConfig()
        self._events: list[ProctorEvent] = []

    @property
    def config(self) -> ProctorConfig:
        """会话阈值配置。"""
        return self._config

    @property
    def stats(self) -> dict[str, Any]:
        """滚动统计快照（弱信号「仅留痕」的观测面，可回放）。"""
        return self._compute_stats()

    # ── 事件入口 ────────────────────────────────────────────────────────────
    def ingest(self, events: Iterable[ProctorEvent]) -> list[ProctorFlag]:
        """批量摄入事件并即时评估：返回当前命中的规则 flag（服务端即时返回口径）。"""
        self._events.extend(events)
        return self.evaluate()

    # ── 统计 ────────────────────────────────────────────────────────────────
    def _compute_stats(self) -> dict[str, Any]:
        """事件流 → 滚动统计（切屏次数/累计离开/粘贴/过快/连击/人脸离开）。"""
        cfg = self._config
        switch_count = 0
        away_total_ms = 0
        pending_blur: int | None = None
        paste_lens: list[int] = []
        rapid_events: list[ProctorEvent] = []
        face_leave_ms = 0
        for event in self._events:
            if event.type == "blur":
                switch_count += 1
                pending_blur = event.t
            elif event.type == "focus" and pending_blur is not None:
                away_total_ms += max(0, event.t - pending_blur)
                pending_blur = None
            elif event.type == "paste":
                paste_lens.append(int(event.detail.get("len", 0)))
            elif event.type == "answer" and int(event.detail.get("elapsed_ms", 10**9)) < cfg.rapid_ms:
                rapid_events.append(event)
            elif event.type == "face_leave":
                face_leave_ms += int(event.detail.get("ms", 0))
        return {
            "switch_count": switch_count,
            "away_total_ms": away_total_ms,
            "paste_lens": paste_lens,
            "rapid_events": rapid_events,
            "burst_streak": self._burst_streak(),
            "face_leave_ms": face_leave_ms,
        }

    def _burst_streak(self) -> list[ProctorEvent]:
        """最长连击：连续「过快且全对」且相邻间隔 ≤ burst_gap_ms 的作答事件切片。"""
        cfg = self._config
        answers = sorted(
            (e for e in self._events if e.type == "answer"), key=lambda e: e.t
        )
        best: list[ProctorEvent] = []
        current: list[ProctorEvent] = []
        for event in answers:
            rapid = int(event.detail.get("elapsed_ms", 10**9)) < cfg.rapid_ms
            correct = bool(event.detail.get("correct", False))
            chained = (
                current
                and event.t - current[-1].t <= cfg.burst_gap_ms
            )
            if rapid and correct and (not current or chained):
                current.append(event)
            elif rapid and correct:
                current = [event]
            else:
                current = []
            if len(current) > len(best):
                best = list(current)
        return best

    # ── 规则评估 ────────────────────────────────────────────────────────────
    def evaluate(self) -> list[ProctorFlag]:
        """评估全部规则：命中者生成 flag（权重降序稳定排序）。"""
        cfg = self._config
        stats = self._compute_stats()
        flags: list[ProctorFlag] = []

        def add(rule: str, evidence: list[ProctorEvent], stat: dict[str, Any]) -> None:
            flags.append(ProctorFlag(rule=rule, weight=self.RULE_WEIGHTS[rule],
                                     evidence=tuple(evidence), stat=stat))

        if stats["switch_count"] >= cfg.blur_limit or stats["away_total_ms"] >= cfg.away_ms_limit:
            add(
                "switch_window",
                [e for e in self._events if e.type == "blur"],  # 证据链：触发切屏的原始事件
                {"switch_count": stats["switch_count"], "away_total_ms": stats["away_total_ms"]},
            )
        long_pastes = [e for e in self._events if e.type == "paste"
                       and int(e.detail.get("len", 0)) >= cfg.paste_chars]
        if long_pastes:
            add("paste_answer", long_pastes,
                {"max_len": max(int(e.detail.get("len", 0)) for e in long_pastes)})
        if len(stats["rapid_events"]) >= cfg.rapid_q_limit:
            add("rapid_answer", list(stats["rapid_events"]),
                {"too_fast_questions": len(stats["rapid_events"])})
        burst = stats["burst_streak"]
        if len(burst) >= cfg.burst_q_limit:
            add("submit_burst", burst, {"burst_correct_streak": len(burst)})
        if cfg.face_enabled and stats["face_leave_ms"] >= cfg.face_ms_limit:
            add("face_leave", [e for e in self._events if e.type == "face_leave"],
                {"face_leave_ms": stats["face_leave_ms"]})
        return sorted(flags, key=lambda f: (-f.weight, f.rule))

    # ── 终判 ────────────────────────────────────────────────────────────────
    def finalize(self) -> tuple[float, bool]:
        """交卷终判：(cheat_score, suspect)。cheat_score=Σ命中规则权重；≥ 阈值判嫌疑。

    弱信号（如单次过快作答权重 1）远低于阈值——宁可漏判不可误判，
    误判风险由人工复核 overturned 通道兜底（SR §6.2 口径）。
        """
        flags = self.evaluate()
        score = float(sum(flag.weight for flag in flags))
        return score, score >= self._config.cheat_threshold


__all__ = ["ProctorConfig", "ProctorEvent", "ProctorFlag", "ProctorSession"]
