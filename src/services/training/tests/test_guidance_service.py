# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""引导服务单测（NFR-USA-03，迭代 1.1；SR-NFR-USA-03 §6.2/§6.3/§9）。

覆盖：
- get_current_tour / tour_view：内置 v1 获取、版本比较字段
  （current_version/seen_version/major_changed/should_show）与不带学员的手动入口形态；
- report_tour_state：done/skipped 落库回执、幂等覆盖写、未知导览 KeyError；
- 空状态注册表完备性：恰好 8 类、scene_id 唯一、中文文案与动作建议字段齐全、
  页面路由均以 / 开头、与内置导览路由空间对齐。
"""
from __future__ import annotations

import pytest

from services.training.core.guidance.config import FIRST_LOGIN_TOUR, build_default_registry
from services.training.core.guidance.service import EMPTY_STATE_REGISTRY, GuidanceService
from services.training.core.guidance.state import InMemoryGuidanceStateStore

#: 任务书约定的空状态场景数（8 类：无问答记录/无缺口/无会话等）
EXPECTED_SCENE_COUNT = 8

#: 8 类场景标识（与任务书口径一一对应；预留场景随 Phase 1 后续迭代页面就绪）
EXPECTED_SCENE_IDS = {
    "tutor_no_qa",  # 无问答记录
    "tutor_no_session",  # 无会话
    "gap_empty",  # 无缺口
    "simulate_empty",  # 无模拟会话（预留）
    "drill_empty",  # 无演练记录（预留）
    "radar_empty",  # 无雷达数据（预留）
    "path_empty",  # 无学习路径（预留）
    "exam_empty",  # 无考试记录（预留）
}


@pytest.fixture()
def service() -> GuidanceService:
    """每个用例独立服务实例（内置 v1 导览 + 干净内存存储）。"""
    return GuidanceService(tours=build_default_registry(), states=InMemoryGuidanceStateStore())


# --------------------------------------------------------------------------- 导览获取与版本比较


class TestCurrentTour:
    """当前导览获取（含版本比较）。"""

    def test_get_current_tour_builtin(self, service: GuidanceService) -> None:
        """默认取内置 v1 首登导览（first-login 1.0）。"""
        tour = service.get_current_tour()
        assert tour is FIRST_LOGIN_TOUR
        assert service.get_current_tour("first-login").version == "1.0"

    def test_get_current_tour_missing(self, service: GuidanceService) -> None:
        """未知导览 → KeyError（API 层映射 400404）。"""
        with pytest.raises(KeyError):
            service.get_current_tour("no-such-tour")

    def test_tour_view_without_trainee(self, service: GuidanceService) -> None:
        """不带学员：仅配置 + 当前版本（「使用指引」手动入口形态，无门控字段）。"""
        view = service.tour_view("first-login")
        assert view["tour_id"] == "first-login"
        assert view["current_version"] == "1.0"
        assert view["tour"]["steps"], "导览配置应含步骤"
        assert "should_show" not in view

    def test_tour_view_with_trainee_version_compare(self, service: GuidanceService) -> None:
        """带学员：附版本比较三键——首访 seen_version=None、major_changed=False、
        should_show=True。"""
        view = service.tour_view("first-login", trainee_id=1)
        assert view["seen_version"] is None
        assert view["major_changed"] is False
        assert view["should_show"] is True

    def test_tour_view_after_skip_not_reshown(self, service: GuidanceService) -> None:
        """跳过后：seen_version=1.0、major_changed=False、should_show=False
        （版本未变不打扰回访用户）。"""
        service.report_tour_state("first-login", trainee_id=1, outcome="skipped", last_step_seq=2)
        view = service.tour_view("first-login", trainee_id=1)
        assert view["seen_version"] == "1.0"
        assert view["major_changed"] is False
        assert view["should_show"] is False


# --------------------------------------------------------------------------- 状态上报


class TestReportState:
    """导览终态上报：落库 + 门控回执。"""

    def test_report_done(self, service: GuidanceService) -> None:
        """完成上报：state.done=True、回执 should_show=False（不再自动弹）。"""
        receipt = service.report_tour_state("first-login", trainee_id=11, outcome="done")
        assert receipt["state"]["done"] is True
        assert receipt["state"]["seen_version"] == "1.0"
        assert receipt["should_show"] is False
        assert receipt["current_version"] == "1.0"

    def test_report_skipped_with_step_seq(self, service: GuidanceService) -> None:
        """跳过上报：携带中断步骤号落库（流失分析）。"""
        receipt = service.report_tour_state(
            "first-login", trainee_id=12, outcome="skipped", last_step_seq=3
        )
        assert receipt["state"]["done"] is False
        assert receipt["state"]["last_step_seq"] == 3

    def test_report_idempotent_overwrite(self, service: GuidanceService) -> None:
        """重复上报覆盖写：终态以最后一次为准（同键幂等）。"""
        service.report_tour_state("first-login", trainee_id=13, outcome="skipped")
        final = service.report_tour_state("first-login", trainee_id=13, outcome="done")
        assert final["state"]["done"] is True
        stored = service.get_state("first-login", 13)
        assert stored is not None and stored.done is True

    def test_report_seen_version_passthrough(self, service: GuidanceService) -> None:
        """seen_version 透传上报：如实记录（迟到上报口径），服务端当前版本随回执给出。"""
        receipt = service.report_tour_state(
            "first-login", trainee_id=14, outcome="skipped", seen_version="0.9"
        )
        assert receipt["state"]["seen_version"] == "0.9"
        assert receipt["current_version"] == "1.0"

    def test_report_missing_tour(self, service: GuidanceService) -> None:
        """未知导览上报 → KeyError。"""
        with pytest.raises(KeyError):
            service.report_tour_state("no-such-tour", trainee_id=1, outcome="done")

    def test_report_bad_seen_version(self, service: GuidanceService) -> None:
        """非法已看版本串上报 → ValueError（拒收脏数据）。"""
        with pytest.raises(ValueError):
            service.report_tour_state(
                "first-login", trainee_id=1, outcome="done", seen_version="one.zero"
            )


# --------------------------------------------------------------------------- 空状态注册表完备性


class TestEmptyStateRegistry:
    """8 类空状态引导文案注册表：完备性是本验收面的硬口径。"""

    def test_exactly_eight_scenes(self) -> None:
        """场景数恰为 8，且 scene_id 与任务书口径一一对应。"""
        assert len(EMPTY_STATE_REGISTRY) == EXPECTED_SCENE_COUNT
        assert {spec.scene_id for spec in EMPTY_STATE_REGISTRY} == EXPECTED_SCENE_IDS

    def test_scene_ids_unique(self) -> None:
        """scene_id 唯一（前端按页面取用的稳定键）。"""
        ids = [spec.scene_id for spec in EMPTY_STATE_REGISTRY]
        assert len(ids) == len(set(ids))

    def test_chinese_copy_and_action_complete(self) -> None:
        """每类场景：中文标题/解释文案 + 动作建议（按钮文案 + 跳转路由）齐全。"""
        for spec in EMPTY_STATE_REGISTRY:
            assert spec.title.strip() and not spec.title.isascii(), f"{spec.scene_id} 标题应为中文"
            assert spec.hint.strip(), f"{spec.scene_id} 解释文案为空"
            assert spec.action_label.strip() and not spec.action_label.isascii(), (
                f"{spec.scene_id} 动作按钮文案应为中文"
            )
            assert spec.action_route.startswith("/"), f"{spec.scene_id} 动作路由非法"
            assert spec.sample_preview.strip(), f"{spec.scene_id} 示例预览说明为空"

    def test_page_routes_align_with_tour_routes(self) -> None:
        """空态页面路由与内置导览路由同空间（/tutor、/gaps 等两套引导互相兜底成环）。"""
        tour_routes = {step.route for step in FIRST_LOGIN_TOUR.steps}
        scene_routes = {spec.page_route for spec in EMPTY_STATE_REGISTRY}
        assert scene_routes <= tour_routes | {"/simulate", "/path", "/exam"}, (
            f"空态页面超出导览覆盖的路由空间：{scene_routes - tour_routes}"
        )

    def test_service_query(self, service: GuidanceService) -> None:
        """服务查询：全量表有序返回；scene_id 命中/未命中。"""
        all_scenes = service.empty_states()
        assert [spec.scene_id for spec in all_scenes] == [
            spec.scene_id for spec in EMPTY_STATE_REGISTRY
        ]
        hit = service.empty_state("tutor_no_qa")
        assert hit is not None and hit.page_route == "/tutor"
        assert service.empty_state("no-such-scene") is None
