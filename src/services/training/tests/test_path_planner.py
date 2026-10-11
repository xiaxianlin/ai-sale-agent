# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""学习路径规划器测试（FR-TR-005，SR-TR-005 §6.1/§6.3；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：雷达快照 → 三段式路径（薄弱突破置顶/必修/选修）、薄弱维升序排序、
  前置依赖拓扑排程、重算版本递增归档、重算保留已完成课时（评估更新后动态调整）、
  完成度统计（completion_pct/by_section）、证据不足维（null）不参与薄弱排序但给补证提示；
- 边界：薄弱维个数上限（weak_dims_count）、薄弱线恰 70 分（低于才入选）、
  课程编码重复注册拒绝、未知维度注册拒绝、必修课时跨段去重（不重复编排）；
- 异常：前置依赖成环 → 剔除成环课时并留诊断（不阻塞整条路径）、
  无雷达快照重算 → TR-3100、快照维度残缺 → 拒绝生成；
- 并发/降级由进度与 API 层测试覆盖。

映射表注册制：CourseCatalog.register(dimension → [course])，维度限雷达四维口径。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from services.training.core.path.planner import (
    DIMENSIONS,
    Course,
    CourseCatalog,
    InMemoryPathEventStore,
    InMemoryPathPlanStore,
    InMemoryPathProgressStore,
    Lesson,
    NoRadarSnapshotError,
    PathConfig,
    PathPlanner,
)
from services.training.core.radar.engine import RadarSnapshot

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)
TRAINEE = 1024

#: 基线四维分：negotiation 40 / product 55 为薄弱两维（阈值 70），communication 75 / platform 88
BASE_SCORES: dict[str, float | None] = {
    "product": 55.0,
    "communication": 75.0,
    "negotiation": 40.0,
    "platform": 88.0,
}

L_REQ_1 = Lesson(lesson_uid="L-req-1", title="新人入职第一课", est_minutes=15)
L_REQ_2 = Lesson(lesson_uid="L-req-2", title="客户沟通基础", est_minutes=20, prereqs=("L-req-1",))
L_PRO_1 = Lesson(lesson_uid="L-pro-1", title="产品参数速查与认证要点", est_minutes=20, has_quiz=True)
L_NEG_1 = Lesson(lesson_uid="L-neg-1", title="议价底线与让步节奏", est_minutes=15, has_quiz=True)
L_COM_1 = Lesson(lesson_uid="L-com-1", title="沟通技巧拓展：异议处理", est_minutes=10)
L_PLA_1 = Lesson(lesson_uid="L-pla-1", title="平台规则拓展：arf 即时沟通", est_minutes=10)


def make_catalog() -> CourseCatalog:
    """标准课程映射表：必修课程包 + 四维各一门课程（注册制 dimension → [course]）。"""
    catalog = CourseCatalog()
    catalog.register(
        Course(course_code="C-REQ-01", title="新人必修基础", dim="product",
               lessons=(L_REQ_1, L_REQ_2), required=True)
    )
    catalog.register(Course(course_code="C-PRO-01", title="产品知识突破", dim="product", lessons=(L_PRO_1,)))
    catalog.register(Course(course_code="C-NEG-01", title="议价能力突破", dim="negotiation", lessons=(L_NEG_1,)))
    catalog.register(Course(course_code="C-COM-01", title="沟通技巧拓展", dim="communication", lessons=(L_COM_1,)))
    catalog.register(Course(course_code="C-PLA-01", title="平台规则拓展", dim="platform", lessons=(L_PLA_1,)))
    return catalog


def make_snapshot(
    scores: dict[str, float | None] | None = None,
    *,
    snapshot_id: str = "RS-0001",
    trainee_id: int = TRAINEE,
    trigger: str = "initial",
) -> RadarSnapshot:
    """按四维分构造雷达快照（score=None 表示证据不足，n 随之为 0）。"""
    dims = {
        d: {"score": (scores if scores is not None else BASE_SCORES).get(d),
            "n": 3 if (scores if scores is not None else BASE_SCORES).get(d) is not None else 0}
        for d in DIMENSIONS
    }
    return RadarSnapshot(snapshot_id=snapshot_id, trainee_id=trainee_id, trigger=trigger,
                         dims=dims, weights_used="test-digest", created_at=NOW)


def make_planner(
    catalog: CourseCatalog | None = None,
    *,
    scores: dict[str, float | None] | None = None,
    with_radar: bool = False,
    config: PathConfig | None = None,
    **kwargs: Any,
) -> PathPlanner:
    """标准装配：注册制课程表 + 三存储全内存实现；with_radar 时预置基线雷达快照。"""
    from services.training.core.radar.engine import InMemoryRadarSnapshotStore

    plan_store = InMemoryPathPlanStore()
    radar_store = InMemoryRadarSnapshotStore()
    if with_radar:
        radar_store.save(make_snapshot(scores, snapshot_id="RS-0001"))
        radar_store.save(make_snapshot(scores, snapshot_id="RS-0002", trigger="event"))
    return PathPlanner(
        catalog if catalog is not None else make_catalog(),
        plan_store,
        InMemoryPathProgressStore(),
        InMemoryPathEventStore(),
        config=config or PathConfig(),
        radar_store=radar_store if with_radar else None,
        **kwargs,
    )


class TestPathPlanEquivalence:
    """等价类：薄弱排序 → 课程映射 → 三段组装 → 重算保留 主链路。"""

    def test_plan_three_sections_weak_first(self) -> None:
        """三段式：weak 段置顶（SRS §14.2 薄弱项优先）且逐课时标注 weak_dim；required/elective 次之。"""
        planner = make_planner()
        plan = planner.plan(TRAINEE, make_snapshot(), now=NOW)
        sections = [item.section for item in plan.items]
        assert sections == ["weak", "weak", "required", "required", "elective", "elective"]
        weak_items = [item for item in plan.items if item.section == "weak"]
        assert [(item.lesson_uid, item.weak_dim) for item in weak_items] == [
            ("L-neg-1", "negotiation"),
            ("L-pro-1", "product"),
        ]
        assert [item.order_no for item in weak_items] == [1, 2]

    def test_weak_dims_ranked_ascending(self) -> None:
        """薄弱维排序：绝对分升序（negotiation 40 < product 55），低分者优先突破。"""
        planner = make_planner()
        plan = planner.plan(TRAINEE, make_snapshot(), now=NOW)
        assert plan.weak_dims == ("negotiation", "product")
        assert plan.radar_snapshot_id == "RS-0001"

    def test_prereq_topo_order_within_required(self) -> None:
        """前置依赖排程：L-req-2 依赖 L-req-1 → 拓扑序排在前置之后（段内 order 递增）。"""
        planner = make_planner()
        plan = planner.plan(TRAINEE, make_snapshot(), now=NOW)
        required = [item.lesson_uid for item in plan.items if item.section == "required"]
        assert required == ["L-req-1", "L-req-2"]
        item2 = next(item for item in plan.items if item.lesson_uid == "L-req-2")
        assert item2.prereqs == ("L-req-1",)

    def test_replan_version_increment_archives(self) -> None:
        """评估更新后重算：版本 +1，旧版本留档可溯，active 指向最新版。"""
        planner = make_planner(with_radar=True)
        v1 = planner.plan(TRAINEE, make_snapshot(snapshot_id="RS-0001"), now=NOW)
        v2 = planner.replan(TRAINEE, now=NOW)
        assert v1.version == 1 and v2.version == 2
        assert planner.plan_store.versions(TRAINEE)[0].version == 1  # 旧版本留档
        assert planner.active(TRAINEE).version == 2

    def test_replan_preserves_completed_status(self) -> None:
        """重算保留已完成（核心口径）：完成后重算，已完成状态原样回填，不重置进度。"""
        planner = make_planner(with_radar=True)
        planner.plan(TRAINEE, make_snapshot(snapshot_id="RS-0001"), now=NOW)
        planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="quiz_passed", lesson_uid="L-neg-1", score=90.0, now=NOW
        )
        # 评估更新：议价能力提升至 90，薄弱维变为 product/communication → L-neg-1 移入 elective
        new_scores = {"product": 55.0, "communication": 75.0, "negotiation": 90.0, "platform": 88.0}
        v2 = planner.replan(TRAINEE, now=NOW, snapshot=make_snapshot(new_scores, snapshot_id="RS-0009"))
        assert v2.weak_dims == ("product", "communication")
        neg_item = next(item for item in v2.items if item.lesson_uid == "L-neg-1")
        view = planner.current_view(TRAINEE)
        assert view["items"]  # 视图非空
        status_of = {i["lesson_uid"]: i["status"] for i in view["items"]}
        assert status_of["L-neg-1"] == "completed"  # 已完成保留
        assert status_of["L-pro-1"] == "not_started"

    def test_stats_completion_pct_and_by_section(self) -> None:
        """完成度统计：completed/total 与分段计数（供看板 FR-TR-006 与进度条渲染）。"""
        planner = make_planner(with_radar=True)
        planner.plan(TRAINEE, make_snapshot(snapshot_id="RS-0001"), now=NOW)
        planner.apply_progress_event(
            TRAINEE, event_uid="e1", kind="lesson_finished", lesson_uid="L-req-1", now=NOW
        )
        planner.apply_progress_event(
            TRAINEE, event_uid="e2", kind="quiz_passed", lesson_uid="L-neg-1", score=88.0, now=NOW
        )
        view = planner.current_view(TRAINEE)
        stats = view["stats"]
        assert stats["total"] == 6 and stats["completed"] == 2
        assert stats["completion_pct"] == round(2 / 6, 4)
        assert stats["by_section"]["required"] == {"total": 2, "completed": 1}
        assert stats["by_section"]["weak"] == {"total": 2, "completed": 1}

    def test_null_dim_uncovered_hint(self) -> None:
        """证据不足维（null）不参与薄弱排序，由必修段覆盖并透传补证提示（SR §2 偏差④）。"""
        planner = make_planner()
        scores = {"product": 55.0, "communication": 75.0, "negotiation": 40.0, "platform": None}
        plan = planner.plan(TRAINEE, make_snapshot(scores), now=NOW)
        assert plan.weak_dims == ("negotiation", "product")
        assert plan.diagnostics.uncovered_dims == ("platform",)
        # platform 维课程仍进选修段（非薄弱维口径），不因证据缺失而消失
        assert any(item.lesson_uid == "L-pla-1" and item.section == "elective" for item in plan.items)


class TestPathPlanBoundary:
    """边界：薄弱维个数、薄弱线、注册表校验、跨段去重。"""

    def test_weak_count_limit(self) -> None:
        """薄弱维个数上限：count=1 时仅最低维驱动 weak 段，product 课程降入选修。"""
        planner = make_planner(config=PathConfig(weak_dims_count=1))
        plan = planner.plan(TRAINEE, make_snapshot(), now=NOW)
        assert plan.weak_dims == ("negotiation",)
        assert [item.lesson_uid for item in plan.items if item.section == "weak"] == ["L-neg-1"]
        pro_item = next(item for item in plan.items if item.lesson_uid == "L-pro-1")
        assert pro_item.section == "elective"

    def test_weak_threshold_boundary(self) -> None:
        """薄弱线恰 70 分：70.0 不算低于阈值（69.9 才算）；补位仍按分数升序。"""
        scores = {"product": 70.0, "communication": 69.9, "negotiation": 90.0, "platform": 88.0}
        planner = make_planner(config=PathConfig(weak_dims_count=1))
        plan = planner.plan(TRAINEE, make_snapshot(scores), now=NOW)
        assert plan.weak_dims == ("communication",)  # 恰 70.0 的 product 未达薄弱线
        planner2 = make_planner(config=PathConfig(weak_dims_count=2))
        plan2 = planner2.plan(TRAINEE, make_snapshot(scores), now=NOW)
        assert plan2.weak_dims == ("communication", "product")  # 不足额从剩余有效维升序补齐

    def test_duplicate_course_code_rejected(self) -> None:
        """课程编码重复注册 → ValueError（注册表唯一事实来源）。"""
        catalog = make_catalog()
        with pytest.raises(ValueError, match="重复注册"):
            catalog.register(
                Course(course_code="C-PRO-01", title="撞号课程", dim="product", lessons=(L_PRO_1,))
            )

    def test_register_unknown_dim_rejected(self) -> None:
        """向四维之外的维度注册课程 → ValueError（映射表口径与雷达四维强一致）。"""
        catalog = CourseCatalog()
        with pytest.raises(ValueError, match="维度"):
            catalog.register(
                Course(course_code="C-X-01", title="越维课程", dim="typing", lessons=(L_COM_1,))
            )

    def test_required_course_not_duplicated(self) -> None:
        """跨段去重：同课时同时出现在薄弱映射与必修课程包时只编排一次（weak 段先编排优先）。"""
        catalog = make_catalog()
        catalog.register(
            Course(course_code="C-PRO-01R", title="产品必修版", dim="product",
                   lessons=(L_PRO_1,), required=True)
        )
        planner = make_planner(catalog=catalog)
        plan = planner.plan(TRAINEE, make_snapshot(), now=NOW)
        pro_items = [item for item in plan.items if item.lesson_uid == "L-pro-1"]
        assert len(pro_items) == 1 and pro_items[0].section == "weak"  # 薄弱段先编排，必修段去重跳过


class TestPathPlanException:
    """异常：成环剔除、无雷达快照、快照维度残缺。"""

    def test_prereq_cycle_dropped_with_diagnostics(self) -> None:
        """前置依赖成环 → 剔除成环课时并留诊断（不阻塞整条路径生成）。"""
        catalog = make_catalog()
        catalog.register(
            Course(course_code="C-CYC-01", title="成环课时组", dim="platform",
                   lessons=(
                       Lesson(lesson_uid="L-cyc-a", title="A", prereqs=("L-cyc-b",)),
                       Lesson(lesson_uid="L-cyc-b", title="B", prereqs=("L-cyc-a",)),
                   ))
        )
        planner = make_planner(catalog=catalog)
        plan = planner.plan(TRAINEE, make_snapshot(), now=NOW)
        assert plan.diagnostics.cycle_dropped == ("L-cyc-a", "L-cyc-b")
        assert all(item.lesson_uid not in ("L-cyc-a", "L-cyc-b") for item in plan.items)
        assert len(plan.items) == 6  # 其余课时不受影响

    def test_replan_without_radar_raises_tr3100(self) -> None:
        """无雷达快照（未完成诊断初评）→ NoRadarSnapshotError（TR-3100）。"""
        planner = make_planner()  # 未装配雷达存储
        with pytest.raises(NoRadarSnapshotError) as excinfo:
            planner.replan(TRAINEE, now=NOW)
        assert excinfo.value.biz_code == "TR-3100"
        empty_radar = make_planner(with_radar=False)
        with pytest.raises(NoRadarSnapshotError):
            empty_radar.replan(TRAINEE, now=NOW)

    def test_snapshot_missing_dims_rejected(self) -> None:
        """雷达快照维度残缺（缺 platform）→ 拒绝生成路径（口径强一致）。"""
        planner = make_planner()
        broken = make_snapshot()
        dims = {k: v for k, v in broken.dims.items() if k != "platform"}
        broken = type(broken)(snapshot_id=broken.snapshot_id, trainee_id=broken.trainee_id,
                              trigger=broken.trigger, dims=dims, weights_used=broken.weights_used,
                              created_at=broken.created_at)
        with pytest.raises(ValueError, match="维度"):
            planner.plan(TRAINEE, broken, now=NOW)


__all__ = ["NOW", "TRAINEE", "BASE_SCORES", "make_catalog", "make_snapshot", "make_planner"]
