# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""引导服务：当前导览获取（含版本比较）+ 空状态引导文案注册表（NFR-USA-03，迭代 1.1）。

模块职责（SR-NFR-USA-03 四层引导体系中 ① 导览与 ② 空状态的服务端支撑）：
- ``GuidanceService.get_current_tour`` / ``tour_view``：获取当前导览配置并完成
  版本比较（当前版本 / 已看版本 / 主版本是否变更 / 是否应显示）——前端
  ``GuidanceTour.vue`` 一次拉齐渲染与门控所需的全量信息；
- ``GuidanceService.report_tour_state``：承接前端上报的完成/跳过态，落
  GuidanceStateStore（mark_done / mark_skipped），返回落库后状态与门控回执；
- ``EmptyStateSpec`` + ``EMPTY_STATE_REGISTRY``：**8 类空状态引导文案注册表**
  （各学员页面无数据时的下一步指引，中文文案 + 动作建议，SR §6.3）：
  1. tutor_no_qa      导师问答页——无问答记录
  2. tutor_no_session 导师问答页——无会话
  3. gap_empty        缺口清单页——无缺口（含「这是好事」的正向解读）
  4. simulate_empty   模拟客户页——无模拟会话（预留）
  5. drill_empty      实战演练页——无演练记录（预留）
  6. radar_empty      能力雷达页——无雷达数据（预留）
  7. path_empty       学习路径页——无路径
  8. exam_empty       考试认证页——无考试记录/认证状态未知
- 示例预览（sample_preview）为合成脱敏样例说明（NFR-SEC-01 纪律：不引用真实
  客户数据），前端随静态资产发布图片，此处登记文案与资产名。

依据文档：
- docs/07-实现方案/NFR/SR-NFR-USA-03-引导式交互.md §2（② 空状态引导）、
  §6.1/§6.2（导览与版本比较）、§6.3（空态场景注册表）、§9（单测口径）
- docs/07-实现方案/TR/SR-TR-001-AI导师系统.md §5（导师/缺口端点对应页面）
- docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.training.core.guidance.config import (
    DEFAULT_TOUR_ID,
    TourConfig,
    TourRegistry,
    build_default_registry,
)
from services.training.core.guidance.state import (
    GuidanceStateStore,
    InMemoryGuidanceStateStore,
    TraineeGuidanceState,
    mark_done,
    mark_skipped,
    should_show,
)

#: 上报结果枚举（前端导览销毁回调的两种终态，SR §6.2 onDestroyed 口径）
TourOutcome = Literal["done", "skipped"]


class EmptyStateSpec(BaseModel):
    """空状态引导场景（SR-NFR-USA-03 §6.3 场景注册表的条目形态）。

    - ``scene_id``：场景标识（kebab/snake 稳定键，前端按页面取用）；
    - ``page_route``：所属学员页面前端路由（与 TourStep.route 同空间）；
    - ``title`` / ``hint``：中文标题与解释文案（空数据 ≠ 空页面）；
    - ``action_label`` / ``action_route``：动作建议（按钮文案 + 跳转路由）；
    - ``sample_preview``：示例数据预览说明（合成脱敏样例，让新人先「看到成品」）；
    - ``reserved``：True=对应功能为 Phase 1 后续迭代预留（文案口径降级为预告）。
    """

    model_config = ConfigDict(frozen=True)

    scene_id: str = Field(min_length=1, description="场景标识")
    page_route: str = Field(min_length=1, description="所属学员页面路由")
    title: str = Field(min_length=1, description="空状态标题（中文）")
    hint: str = Field(min_length=1, description="空状态解释文案（中文）")
    action_label: str = Field(min_length=1, description="动作建议按钮文案（中文）")
    action_route: str = Field(min_length=1, description="动作建议跳转路由")
    sample_preview: str = Field(min_length=1, description="示例数据预览说明（合成脱敏样例）")
    reserved: bool = Field(default=False, description="True=功能为后续迭代预留")
    tour_id: str | None = Field(default=None, description="可跳转的分步导览（可选）")

    @model_validator(mode="after")
    def _routes_must_be_path(self) -> "EmptyStateSpec":
        """路由字段必须以 / 开头（与前端 router 配置对齐）。"""
        for field_name in ("page_route", "action_route"):
            value = getattr(self, field_name)
            if not value.startswith("/"):
                raise ValueError(f"EmptyStateSpec.{field_name} 必须为以 / 开头的前端路由，收到：{value!r}")
        return self


#: 空状态引导注册表：8 类（SR-NFR-USA-03 §6.3；预留场景口径=预告文案，页面就绪即生效）
EMPTY_STATE_REGISTRY: tuple[EmptyStateSpec, ...] = (
    EmptyStateSpec(
        scene_id="tutor_no_qa",
        page_route="/tutor",
        title="还没有问答记录",
        hint="先向 AI 导师提第一个产品或流程问题——回答全部基于企业知识库，"
        "并标注 [来源N] 引用可展开核对，不会编造。",
        action_label="去提第一个问题",
        action_route="/tutor",
        sample_preview="示例：输入「ST-3200 支架的最大承载是多少」→ 导师回答附知识块来源卡片",
    ),
    EmptyStateSpec(
        scene_id="tutor_no_session",
        page_route="/tutor",
        title="还没有会话",
        hint="新建一个会话即可开始提问；不同主题可分会话管理，历史问答随时回看。",
        action_label="新建会话",
        action_route="/tutor",
        sample_preview="示例：会话列表按最近活跃排序，含标题与轮数",
    ),
    EmptyStateSpec(
        scene_id="gap_empty",
        page_route="/gaps",
        title="暂无知识缺口",
        hint="说明你近期的问题都被知识库覆盖了。提问中知识库无法可靠回答的问题，"
        "会聚合到这里，知识库补充后即可得到准确回答。",
        action_label="去提问试试",
        action_route="/tutor",
        sample_preview="示例：缺口清单按提问频次降序，含最近提问时间",
    ),
    EmptyStateSpec(
        scene_id="simulate_empty",
        page_route="/simulate",
        title="还没有模拟会话",
        hint="模拟客户对话将在 Phase 1 后续迭代开放：选一位模拟客户开始对话，"
        "结束后自动生成复盘报告。",
        action_label="先去问 AI 导师",
        action_route="/tutor",
        sample_preview="示例：模拟客户类型卡（价格敏感型/质量优先型）+ 复盘报告示例",
        reserved=True,
    ),
    EmptyStateSpec(
        scene_id="drill_empty",
        page_route="/exercise",
        title="还没有演练记录",
        hint="实战演练将在 Phase 1 后续迭代开放：用真实历史询盘练手，"
        "AI 评分并与优秀回复逐维对比。",
        action_label="先去问 AI 导师",
        action_route="/tutor",
        sample_preview="示例：演练复盘报告（评分四维 + 与优秀回复对比段）",
        reserved=True,
    ),
    EmptyStateSpec(
        scene_id="radar_empty",
        page_route="/radar",
        title="还没有能力数据",
        hint="四维能力雷达（产品知识/沟通技巧/议价能力/平台规则）将在演练与考试"
        "上线后自动更新；当前先通过提问积累学习记录。",
        action_label="先去问 AI 导师",
        action_route="/tutor",
        sample_preview="示例雷达图：四维 0~100 分，薄弱维自动排入学习路径",
        reserved=True,
    ),
    EmptyStateSpec(
        scene_id="path_empty",
        page_route="/path",
        title="还没有学习路径",
        hint="学习路径将根据能力雷达的薄弱项自动生成；路径上线前，"
        "可先通过 AI 导师提问定向补齐产品与流程知识。",
        action_label="先去问 AI 导师",
        action_route="/tutor",
        sample_preview="示例：路径进度条 + 下一课时卡片（含预计用时）",
        reserved=True,
    ),
    EmptyStateSpec(
        scene_id="exam_empty",
        page_route="/exam",
        title="还没有考试记录",
        hint="认证考试是上岗前置条件（系统强制校验）；模拟考试将在 Phase 1 后续"
        "迭代开放，规则说明届时可就地展开查看。",
        action_label="先去问 AI 导师",
        action_route="/tutor",
        sample_preview="示例：认证状态卡（未考/通过/未通过）+ 前置条件清单",
        reserved=True,
    ),
)

#: 场景注册表的 dict 索引（启动期一次构建，O(1) 查询）
_EMPTY_STATE_INDEX: dict[str, EmptyStateSpec] = {spec.scene_id: spec for spec in EMPTY_STATE_REGISTRY}


class GuidanceService:
    """引导服务：导览获取（含版本比较）/ 状态上报 / 空状态文案查询的统一门面。

    组合两个可替换依赖（协议见各模块）：
    - ``tours``：TourRegistry（导览配置注册表，默认灌入内置 v1）；
    - ``states``：GuidanceStateStore（学员引导终态存储，默认内存实现）。
    API 层（api/guidance_api.py）经依赖注入取用本服务，路由层零业务实现。
    """

    def __init__(
        self,
        tours: TourRegistry | None = None,
        states: GuidanceStateStore | None = None,
    ) -> None:
        self.tours = tours if tours is not None else build_default_registry()
        self.states = states if states is not None else InMemoryGuidanceStateStore()

    # ── ① 分步导览 ─────────────────────────────────────────────────────────

    def get_current_tour(self, tour_id: str = DEFAULT_TOUR_ID) -> TourConfig:
        """取当前导览配置；不存在抛 KeyError（API 层映射 40404）。"""
        return self.tours.require(tour_id)

    def tour_view(self, tour_id: str, trainee_id: int | None = None) -> dict[str, Any]:
        """导览视图：配置 + 版本比较 + 门控结论（前端一次拉齐）。

        - 不带 trainee_id：仅返回配置与当前版本（「使用指引」手动入口直接用）；
        - 带 trainee_id：追加已看版本（seen_version，无记录为 None）、主版本是否
          变更（major_changed）、是否应自动显示（should_show）三键——版本比较
          口径与 state.should_show 同源（克制门控唯一实现）。
        """
        tour = self.get_current_tour(tour_id)
        view: dict[str, Any] = {
            "tour": tour.model_dump(),
            "current_version": tour.version,
            "tour_id": tour.tour_id,
        }
        if trainee_id is None:
            return view
        record = self.states.load(tour.tour_id, trainee_id)
        view["seen_version"] = record.seen_version if record is not None else None
        view["major_changed"] = bool(
            record is not None and tour.skip_policy.reshow_on_major and record.major < tour.major
        )
        view["should_show"] = should_show(self.states, tour, trainee_id)
        return view

    # ── ① 分步导览——状态上报 ──────────────────────────────────────────────

    def report_tour_state(
        self,
        tour_id: str,
        trainee_id: int,
        outcome: TourOutcome,
        last_step_seq: int | None = None,
        seen_version: str | None = None,
    ) -> dict[str, Any]:
        """上报导览终态（完成/跳过）并回执：落库状态 + 上报后门控结论。

        - ``outcome="done"``：完整走完 → mark_done（last_step_seq 恒为末步）；
        - ``outcome="skipped"``：跳过/中途终止 → mark_skipped（携带中断步骤号）；
        - ``seen_version``：前端上报的已看版本（localStorage 镜像口径）；缺省按
          当前配置版本落库。迟到上报（改版后旧页面才回调）按所看版本如实记录，
          不回滚配置——重显判定以「已看主版本 < 当前主版本」为准，自然收敛。
        - 未知导览抛 KeyError；非法 outcome 为 API 层入参校验拦截（Literal）。
        """
        tour = self.get_current_tour(tour_id)
        if outcome == "done":
            state = mark_done(self.states, tour, trainee_id, seen_version=seen_version)
        else:
            state = mark_skipped(
                self.states, tour, trainee_id, last_step_seq=last_step_seq, seen_version=seen_version
            )
        return {
            "state": state.model_dump(),
            "should_show": should_show(self.states, tour, trainee_id),
            "current_version": tour.version,
        }

    def get_state(self, tour_id: str, trainee_id: int) -> TraineeGuidanceState | None:
        """查某学员某导览的服务端终态；无记录返回 None。"""
        return self.states.load(tour_id, trainee_id)

    # ── ② 空状态引导 ───────────────────────────────────────────────────────

    def empty_states(self) -> list[EmptyStateSpec]:
        """全量空状态场景表（按注册序，API 直接序列化）。"""
        return list(EMPTY_STATE_REGISTRY)

    def empty_state(self, scene_id: str) -> EmptyStateSpec | None:
        """按场景标识取空状态文案；不存在返回 None（前端回退默认文案，不阻塞）。"""
        return _EMPTY_STATE_INDEX.get(scene_id)
