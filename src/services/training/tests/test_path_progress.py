# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""学习路径进度状态机与完成事件测试（FR-TR-005，SR-TR-005 §6.2；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：合法迁移矩阵（未开始→进行中→已完成；测验结算；需重修复课）、
  无测验课时学完即完成、测验通过结课（first_completed_at 恰一次）、
  测验失败→需重修→重修通过（attempts 累计）、同 event_uid 重放幂等；
- 边界：乱序合并（测验结果先于完成事件到达，取并集不丢单）、
  重修尝试超上限（attempts ≥ max_attempts → 主管干预标记 path_stuck）、
  测验事件缺 score 拒绝；
- 异常：前置课时未完成锁定（E_LESSON_LOCKED）、课时不在当前路径、
  无活动路径、非法迁移（已完成不回退/需重修须先开始）；
- 并发：同 event_uid 并发重放恰一次生效；不同课时事件并发均落账。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import pytest

from services.training.core.path.planner import (
    IllegalTransitionError,
    LessonLockedError,
    LessonNotInPathError,
    NoActivePathError,
    PathConfig,
    transition,
)
from services.training.tests.test_path_planner import TRAINEE, make_planner, make_snapshot

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)


def seeded_planner(**kwargs: Any):
    """带 v1 路径的规划器（BASE_SCORES 薄弱维 negotiation/product，6 课时）。"""
    planner = make_planner(with_radar=True, **kwargs)
    planner.plan(TRAINEE, make_snapshot(snapshot_id="RS-0001"), now=NOW)
    return planner


class TestProgressStateMachine:
    """状态机：纯函数迁移表（落库四态，awaiting_quiz 为内存瞬态不落库）。"""

    def test_legal_transitions_matrix(self) -> None:
        """合法迁移全矩阵：未开始/进行中/已完成/需重修 × 四事件的既定口径。"""
        cases = [
            # (当前态, 事件, 有无测验, 期望态)
            ("not_started", "lesson_started", False, "in_progress"),
            ("not_started", "lesson_finished", False, "completed"),  # 无测验课时学完即完成
            ("not_started", "lesson_finished", True, "in_progress"),  # 有测验：保持待测验（瞬态落库为 in_progress）
            ("in_progress", "lesson_finished", False, "completed"),
            ("in_progress", "lesson_finished", True, "in_progress"),
            ("in_progress", "quiz_passed", False, "completed"),
            ("in_progress", "quiz_failed", False, "needs_retake"),
            ("needs_retake", "lesson_started", False, "in_progress"),  # 重修先开始
            ("needs_retake", "quiz_passed", False, "completed"),
            ("needs_retake", "quiz_failed", False, "needs_retake"),
            ("completed", "quiz_passed", False, "completed"),  # 重复交卷幂等：已完成不回退
        ]
        for state, event, has_quiz, expected in cases:
            assert transition(state, event, has_quiz=has_quiz) == expected, (state, event)

    def test_illegal_transitions_raise(self) -> None:
        """非法迁移：已完成不因再测失败回退；需重修须先开始；未开始不得直接出测验结果。"""
        for state, event in (
            ("completed", "quiz_failed"),
            ("completed", "lesson_finished"),
            ("completed", "lesson_started"),
            ("needs_retake", "lesson_finished"),
            ("not_started", "quiz_passed"),
            ("not_started", "quiz_failed"),
        ):
            with pytest.raises(IllegalTransitionError):
                transition(state, event, has_quiz=True)


class TestProgressApplyEquivalence:
    """等价类：完成事件应用主链路（幂等 / 测验结算 / 重修闭环）。"""

    def test_start_finish_no_quiz_completed(self) -> None:
        """无测验课时：开始 → 学完即 completed，完成度随之推进。"""
        planner = seeded_planner()
        r1 = planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="lesson_started", lesson_uid="L-req-1", now=NOW
        )
        assert r1.status == "in_progress"
        r2 = planner.apply_progress_event(
            TRAINEE, event_uid="e2", kind="lesson_finished", lesson_uid="L-req-1", now=NOW
        )
        assert r2.status == "completed" and r2.completion_pct == round(1 / 6, 4)

    def test_quiz_flow_to_completed(self) -> None:
        """有测验课时：学完保持进行中（待测验瞬态）→ 测验通过 → completed 且首完时间恰一次。"""
        planner = seeded_planner()
        r1 = planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="lesson_finished", lesson_uid="L-neg-1", now=NOW
        )
        assert r1.status == "in_progress"  # 有测验，待结算
        r2 = planner.apply_progress_event(
            TRAINEE, event_uid="e2", kind="quiz_passed", lesson_uid="L-neg-1", score=92.0, now=NOW
        )
        assert r2.status == "completed" and r2.attempts == 1
        progress = planner.progress_store.get(TRAINEE, "L-neg-1")
        assert progress.first_completed_at == NOW and progress.quiz_best == 92.0

    def test_retake_fail_then_pass(self) -> None:
        """重修闭环：测验 55 分（未达 60 通过线）→ needs_retake → 重修通过 → completed。"""
        planner = seeded_planner()
        planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="lesson_finished", lesson_uid="L-neg-1", now=NOW
        )
        r_fail = planner.apply_progress_event(
            TRAINEE, event_uid="e2", kind="quiz_failed", lesson_uid="L-neg-1", score=55.0, now=NOW
        )
        assert r_fail.status == "needs_retake" and r_fail.attempts == 1
        planner.apply_progress_event(
            TRAINEE, event_uid="e3", kind="lesson_started", lesson_uid="L-neg-1", now=NOW
        )
        r_pass = planner.apply_progress_event(
            TRAINEE, event_uid="e4", kind="quiz_passed", lesson_uid="L-neg-1", score=75.0, now=NOW
        )
        assert r_pass.status == "completed" and r_pass.attempts == 2
        progress = planner.progress_store.get(TRAINEE, "L-neg-1")
        assert progress.quiz_best == 75.0  # 最高分留存

    def test_event_replay_idempotent(self) -> None:
        """同 event_uid 重放：返回首次结果（replayed=True），attempts/完成度不重复累计。"""
        planner = seeded_planner()
        first = planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="quiz_passed", lesson_uid="L-neg-1", score=90.0, now=NOW
        )
        replay = planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="quiz_passed", lesson_uid="L-neg-1", score=90.0, now=NOW
        )
        assert first.replayed is False and replay.replayed is True
        assert replay.status == first.status and replay.completion_pct == first.completion_pct
        assert planner.progress_store.get(TRAINEE, "L-neg-1").attempts == 1


class TestProgressApplyBoundary:
    """边界：乱序合并、重修超限干预标记、测验事件必须携带分数。"""

    def test_out_of_order_quiz_first_merged(self) -> None:
        """乱序合并：quiz_failed 先于 lesson_finished 到达 → 视同已完成学习再结算（不丢单）。"""
        planner = seeded_planner()
        r1 = planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="quiz_failed", lesson_uid="L-neg-1", score=40.0, now=NOW
        )
        assert r1.status == "needs_retake" and r1.attempts == 1
        r2 = planner.apply_progress_event(
            TRAINEE, event_uid="e2", kind="quiz_passed", lesson_uid="L-neg-1", score=70.0, now=NOW
        )
        assert r2.status == "completed" and r2.attempts == 2

    def test_attempts_overflow_stuck_flag(self) -> None:
        """重修超限：attempts 达 max_attempts(3) 仍不过 → 保持 needs_retake + path_stuck 干预标记。"""
        planner = seeded_planner()
        for i in range(1, 4):
            result = planner.apply_progress_event(
                TRAINEE, event_uid=f"e{i}", kind="quiz_failed", lesson_uid="L-neg-1", score=50.0, now=NOW
            )
        assert result.status == "needs_retake" and result.attempts == 3
        assert result.stuck is True
        assert planner.interventions == [
            {"trainee_id": TRAINEE, "kind": "path_stuck", "lesson_uid": "L-neg-1", "attempts": 3}
        ]

    def test_quiz_event_requires_score(self) -> None:
        """测验事件缺 score → ValueError（API 层映射 40001）。"""
        planner = seeded_planner()
        with pytest.raises(ValueError, match="score"):
            planner.apply_progress_event(
                TRAINEE, event_uid="e1", kind="quiz_passed", lesson_uid="L-neg-1", now=NOW
            )

    def test_quiz_submitted_derives_outcome_by_pass_line(self) -> None:
        """quiz_submitted（SR §6.2 事件口径）：按通过线 60 派生结算——55 → needs_retake，75 → completed。"""
        planner = seeded_planner()
        planner.apply_progress_event(
            TRAINEE, event_uid="e0", kind="lesson_finished", lesson_uid="L-neg-1", now=NOW
        )
        below = planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="quiz_submitted", lesson_uid="L-neg-1", score=55.0, now=NOW
        )
        assert below.status == "needs_retake" and below.attempts == 1
        above = planner.apply_progress_event(
            TRAINEE, event_uid="e2", kind="quiz_submitted", lesson_uid="L-neg-1", score=75.0, now=NOW
        )
        assert above.status == "completed" and above.attempts == 2
        assert planner.progress_store.get(TRAINEE, "L-neg-1").quiz_best == 75.0  # 最高分留存


class TestProgressApplyException:
    """异常：前置锁定 / 课时不在路径 / 无活动路径。"""

    def test_locked_prereq_rejected(self) -> None:
        """前置课时未完成：后续课时事件被拒（TR-3103），且不产生事件与状态副作用。"""
        planner = seeded_planner()
        with pytest.raises(LessonLockedError) as excinfo:
            planner.apply_progress_event(
                TRAINEE, event_uid="e1", kind="lesson_started", lesson_uid="L-req-2", now=NOW
            )
        assert excinfo.value.biz_code == "TR-3103"
        assert planner.progress_store.get(TRAINEE, "L-req-2") is None  # 无副作用
        # 前置完成后解锁
        planner.apply_progress_event(
            TRAINEE, event_uid="e2", kind="lesson_finished", lesson_uid="L-req-1", now=NOW
        )
        unlocked = planner.apply_progress_event(
            TRAINEE, event_uid="e3", kind="lesson_started", lesson_uid="L-req-2", now=NOW
        )
        assert unlocked.status == "in_progress"

    def test_lesson_not_in_path_rejected(self) -> None:
        """课时不在当前路径（TR-3102）→ 拒绝上报。"""
        planner = seeded_planner()
        with pytest.raises(LessonNotInPathError):
            planner.apply_progress_event(
                TRAINEE, event_uid="e1", kind="lesson_finished", lesson_uid="L-ghost", now=NOW
            )

    def test_no_active_path_rejected(self) -> None:
        """无活动路径（未生成路径即上报）→ NoActivePathError（TR-3101）。"""
        planner = make_planner()  # 未生成任何路径
        with pytest.raises(NoActivePathError) as excinfo:
            planner.apply_progress_event(
                TRAINEE, event_uid="e1", kind="lesson_started", lesson_uid="L-req-1", now=NOW
            )
        assert excinfo.value.biz_code == "TR-3101"


class TestProgressApplyConcurrency:
    """并发：事件幂等恰一次生效 / 多课时事件并发均落账（demo 进程锁，生产 Redis+DB 唯一约束）。"""

    def test_concurrent_same_event_uid_single_apply(self) -> None:
        """8 线程并发重放同一 event_uid：仅一次生效（attempts=1），其余幂等返回。"""
        planner = seeded_planner()

        def report(_: int):
            return planner.apply_progress_event(
                TRAINEE, event_uid="race-1", kind="quiz_passed", lesson_uid="L-neg-1", score=88.0, now=NOW
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(report, range(8)))
        applied = [r for r in results if not r.replayed]
        assert len(applied) == 1
        assert all(r.status == "completed" for r in results)
        assert planner.progress_store.get(TRAINEE, "L-neg-1").attempts == 1

    def test_concurrent_distinct_events_both_applied(self) -> None:
        """两课时事件并发上报：互不覆盖，均落账为 in_progress。"""
        planner = seeded_planner()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    planner.apply_progress_event, TRAINEE, event_uid=uid, kind="lesson_started",
                    lesson_uid=lesson, now=NOW,
                )
                for uid, lesson in (("e-a", "L-neg-1"), ("e-b", "L-pro-1"))
            ]
            results = [f.result() for f in futures]
        assert {r.status for r in results} == {"in_progress"}
        assert planner.progress_store.get(TRAINEE, "L-neg-1").status == "in_progress"
        assert planner.progress_store.get(TRAINEE, "L-pro-1").status == "in_progress"


# PathConfig 导入校验（防止误删配置模型导致规划器缺省装配失败）
def test_path_config_defaults() -> None:
    """缺省配置与 SR-TR-005 §7 一致：薄弱 2 维 / 阈值 70 / 通过线 60 / 重修上限 3。"""
    config = PathConfig()
    assert (config.weak_dims_count, config.weak_threshold, config.quiz_pass_line, config.max_attempts) == (
        2, 70.0, 60.0, 3,
    )


__all__ = ["NOW", "seeded_planner"]
