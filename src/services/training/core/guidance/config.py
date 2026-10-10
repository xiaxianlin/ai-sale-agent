# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""分步导览配置模型与内置 v1 导览（NFR-USA-03 培训端引导式交互，迭代 1.1）。

模块职责（SR-NFR-USA-03 §6.1 导览配置版本化 + §6.6 CI 选择器守护门禁的后端基座）：
- ``TourStep``：单步导览——次序（seq）、前端路由（route）、锚点选择器
  （target_selector）、交互动作（action）、标题/正文、单步跳过策略（skippable）；
- ``TourConfig``：导览整体——tour_id / 版本（major.minor 语义版本）/ 受众 /
  跳过策略（SkipPolicy，克制原则：一切引导可跳过、仅主版本升级重显一次）；
- ``TourRegistry``：注册表——**同 tour_id 版本单调递增校验**（防止配置回滚或
  静默覆盖，界面改版必须升版本以触发「主版本重显」门控）；
- ``FIRST_LOGIN_TOUR``：内置 v1 首登导览，覆盖培训端核心路径
  **提问（AI 导师）→ 看来源引用 → 查缺口清单 →（预留）演练 / 雷达**，
  前端路由锚定 SR-TR-001 §5 导师端点形态所对应的学员页面。

锚点规约（CI 守护门禁的结构性基座）：
- 导览定位一律挂 ``data-tour`` 稳定锚点，禁止 class / 文本定位——class 随样式
  重构漂移，锚点随功能存活（SR-NFR-USA-03 §2 关键决策，同 SR-NFR-PER-05
  ``data-perf-ready`` 思路）；
- ``target_selector`` 必须是 ``[data-tour='xxx']``（或双引号形态）的完整选择器，
  锚点值为 kebab-case（``[a-z0-9][a-z0-9-]*``）；模型校验层即拒载，
  测试层再对内置导览逐步断言（test_guidance_config.py，CI 门禁）。

版本规约：
- ``version`` 为 ``major.minor`` 字符串（如 ``1.0``）；主版本 = 界面改版
  （允许对已跳过用户重显一次），次版本 = 步骤/文案微调（不重显）；
- 同 tour_id 重复注册时，新版本必须**严格大于**已有版本，否则 ``TourVersionError``。

依据文档：
- docs/07-实现方案/NFR/SR-NFR-USA-03-引导式交互.md §2（四层引导体系/关键决策）、
  §6.1（导览配置 JSON 版本化）、§6.6（选择器守护 CI 门禁）、§9（单测口径）
- docs/07-实现方案/TR/SR-TR-001-AI导师系统.md §5（导师端点形态——tour 步骤
  锚定的 /tutor、/gaps 页面与之对应）
- docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）
"""
from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: 锚点选择器唯一合法形态：[data-tour='xxx'] / [data-tour="xxx"]，锚点值 kebab-case
TOUR_SELECTOR_PATTERN: re.Pattern[str] = re.compile(r"^\[data-tour=['\"]([a-z0-9][a-z0-9-]*)['\"]\]$")

#: 版本唯一合法形态：major.minor（非负整数，无前导零歧义时按整型比较）
TOUR_VERSION_PATTERN: re.Pattern[str] = re.compile(r"^\d+\.\d+$")

#: 步骤交互动作枚举（引导气泡中提示学员的操作类型）
TourAction = Literal["view", "click", "input", "navigate"]

#: 导览受众（SRS §6.2：培训端学员角色 trainee；后续如需主管导览再扩）
DEFAULT_AUDIENCE: tuple[str, ...] = ("trainee",)


class TourVersionError(ValueError):
    """导览版本违规：格式非法、或同 tour_id 注册版本未严格单调递增。"""


def parse_tour_version(version: str) -> tuple[int, int]:
    """解析 ``major.minor`` 版本串为整型二元组（比较口径：字典序 → 整型序）。

    格式非法即抛 :class:`TourVersionError`（中文文案，供配置校验与 CI 报错直读）。
    """
    if not isinstance(version, str) or not TOUR_VERSION_PATTERN.match(version):
        raise TourVersionError(f"导览版本必须为 major.minor 形态（如 1.0），收到：{version!r}")
    major_s, minor_s = version.split(".")
    return int(major_s), int(minor_s)


class TourStep(BaseModel):
    """单步导览：锚定一个 data-tour 锚点，给出标题/正文与操作提示。

    字段口径（任务书「target_selector/action/标题/正文/次序/跳过策略」）：
    - ``seq``：次序，从 1 起连续递增（连续性由 TourConfig 整体校验）；
    - ``target_selector``：锚点选择器，必须 ``[data-tour=...]`` 形态（模型层拒载）；
    - ``action``：引导学员在该步完成的交互（view 看一眼 / click 点击 / input 输入 /
      navigate 前往页面）；
    - ``title`` / ``body``：中文标题与兜底正文（课程模板文案联动 body_key 为
      SR-NFR-USA-03 §6.5 后续迭代接入项，本迭代以内置文案为准）；
    - ``skippable``：单步跳过策略（克制原则默认可跳过）。
    """

    model_config = ConfigDict(frozen=True)

    seq: int = Field(ge=1, description="步骤次序，从 1 起连续递增")
    route: str = Field(min_length=1, description="前端路由（培训 Web 学员页面路径）")
    target_selector: str = Field(min_length=1, description="锚点选择器，仅允许 [data-tour=...] 形态")
    title: str = Field(min_length=1, description="步骤标题（中文）")
    body: str = Field(min_length=1, description="步骤正文（中文，内置兜底文案）")
    body_key: str | None = Field(default=None, description="课程模板文案联动键（SR §6.5，后续迭代）")
    action: TourAction = Field(default="view", description="该步引导的操作类型")
    skippable: bool = Field(default=True, description="单步跳过策略：True=可跳过（克制原则默认）")

    @field_validator("target_selector")
    @classmethod
    def _selector_must_anchor_data_tour(cls, value: str) -> str:
        """CI 选择器守护的模型层基座：非 [data-tour=...] 形态直接拒载（中文报错）。"""
        if not TOUR_SELECTOR_PATTERN.match(value):
            raise ValueError(
                f"导览步骤 target_selector 必须为 [data-tour='xxx'] 形态（禁止 class/文本定位），收到：{value!r}"
            )
        return value

    @field_validator("route")
    @classmethod
    def _route_must_be_path(cls, value: str) -> str:
        """前端路由必须以 / 开头（与前端 router 配置对齐，避免相对路径漂移）。"""
        if not value.startswith("/"):
            raise ValueError(f"导览步骤 route 必须为以 / 开头的前端路由，收到：{value!r}")
        return value

    @property
    def anchor(self) -> str:
        """锚点值（去选择器壳后的 data-tour 属性值），供前端挂载与 CI 守护比对。"""
        matched = TOUR_SELECTOR_PATTERN.match(self.target_selector)
        assert matched is not None  # 模型校验已保证；断言仅为类型收窄
        return matched.group(1)


class SkipPolicy(BaseModel):
    """导览整体跳过策略（SR-NFR-USA-03 §2 克制原则的配置化）。

    - ``skippable``：导览可跳过（跳过是用户权利，不惩罚性重弹）；
    - ``reshow_on_major``：仅主版本升级（= 界面改版）时对已跳过/已看完用户重显一次。
    """

    model_config = ConfigDict(frozen=True)

    skippable: bool = Field(default=True, description="导览整体是否可跳过")
    reshow_on_major: bool = Field(default=True, description="主版本升级后是否重显一次")


class TourConfig(BaseModel):
    """一份分步导览配置（版本化 JSON 的内存形态，随码入库、界面改版同 PR 升版本）。"""

    model_config = ConfigDict(frozen=True)

    tour_id: str = Field(min_length=1, description="导览标识（如 first-login）")
    version: str = Field(min_length=1, description="版本（major.minor 语义版本串）")
    title: str = Field(min_length=1, description="导览标题（中文）")
    enabled: bool = Field(default=True, description="总开关（紧急止血用，对应 guide.tour.enabled）")
    audience: list[str] = Field(default_factory=lambda: list(DEFAULT_AUDIENCE), description="受众角色")
    skip_policy: SkipPolicy = Field(default_factory=SkipPolicy, description="跳过策略")
    steps: list[TourStep] = Field(min_length=1, description="步骤序列（按 seq 升序）")

    @field_validator("version")
    @classmethod
    def _version_must_be_semver(cls, value: str) -> str:
        """版本串格式校验（复用 parse_tour_version 的 TourVersionError 口径）。"""
        parse_tour_version(value)
        return value

    @model_validator(mode="after")
    def _steps_ordered_and_unique_anchor(self) -> "TourConfig":
        """整体校验：seq 从 1 连续递增；同导览内锚点值唯一（一个锚点只讲一件事）。"""
        seqs = [step.seq for step in self.steps]
        if seqs != list(range(1, len(self.steps) + 1)):
            raise ValueError(f"导览步骤 seq 必须从 1 连续递增，收到：{seqs}")
        anchors = [step.anchor for step in self.steps]
        duplicated = {a for a in anchors if anchors.count(a) > 1}
        if duplicated:
            raise ValueError(f"同导览内 data-tour 锚点值必须唯一，重复：{sorted(duplicated)}")
        return self

    @property
    def major(self) -> int:
        """主版本号（界面改版口径：升级则允许重显一次）。"""
        return parse_tour_version(self.version)[0]

    @property
    def minor(self) -> int:
        """次版本号（步骤/文案微调口径：升级不重显）。"""
        return parse_tour_version(self.version)[1]

    def step(self, seq: int) -> TourStep:
        """按次序取步骤；越界抛 KeyError（中文文案）。"""
        for step in self.steps:
            if step.seq == seq:
                return step
        raise KeyError(f"导览 {self.tour_id} 不存在步骤 seq={seq}（有效范围 1..{len(self.steps)}）")


#: 内置 v1 首登导览：培训端核心路径（提问→看来源引用→查缺口清单→（预留）演练/雷达）。
#: 路由与端点对应（SR-TR-001 §5）：/tutor ↔ POST /api/v1/training/tutor/sessions/{uid}/ask
#: （SSE）与 GET .../sessions 会话列表；/gaps ↔ GET /api/v1/training/knowledge-gaps；
#: /exercise、/radar 为 Phase 1 后续迭代的预留锚点（步骤先占位，页面就绪即生效）。
FIRST_LOGIN_TOUR: TourConfig = TourConfig(
    tour_id="first-login",
    version="1.0",
    title="新人上手导览",
    enabled=True,
    audience=list(DEFAULT_AUDIENCE),
    skip_policy=SkipPolicy(skippable=True, reshow_on_major=True),
    steps=[
        TourStep(
            seq=1,
            route="/home",
            target_selector="[data-tour='workbench']",
            title="欢迎加入",
            body="成长路线：先向 AI 导师提问 → 查看回答的知识来源 → 看知识缺口清单，"
            "模拟演练与能力雷达将在后续迭代开放，全程约 5 分钟走完。",
            action="view",
        ),
        TourStep(
            seq=2,
            route="/tutor",
            target_selector="[data-tour='tutor-ask']",
            title="向 AI 导师提问",
            body="任何产品、流程问题先问 AI 导师：在输入框输入问题后发送，"
            "回答全部基于企业知识库生成，不会编造。",
            body_key="tour.tutor",
            action="input",
        ),
        TourStep(
            seq=3,
            route="/tutor",
            target_selector="[data-tour='tutor-sources']",
            title="查看回答的来源引用",
            body="每条回答都标注 [来源N] 引用，点击即可展开对应知识块原文核对；"
            "当知识库没有可靠依据时，导师会明确告知并把问题记录为知识缺口，而不是猜答案。",
            body_key="tour.sources",
            action="click",
        ),
        TourStep(
            seq=4,
            route="/tutor",
            target_selector="[data-tour='tutor-history']",
            title="回看历史问答",
            body="历史会话与问答记录会保留，随时回看之前的回答与引用，复习不用重新提问。",
            body_key="tour.history",
            action="view",
        ),
        TourStep(
            seq=5,
            route="/gaps",
            target_selector="[data-tour='gap-list']",
            title="查看知识缺口清单",
            body="学员提出而知识库暂未覆盖的问题，会聚合到缺口清单；"
            "知识库补充后即可得到准确回答——你的提问在帮助知识库变完整。",
            body_key="tour.gaps",
            action="view",
        ),
        TourStep(
            seq=6,
            route="/exercise",
            target_selector="[data-tour='drill-entry']",
            title="实战演练（预留）",
            body="本入口为预留：模拟客户对话与复盘报告将在 Phase 1 后续迭代开放，"
            "开放后可先用历史询盘练手，再看 AI 评分与优秀回复的对比。",
            body_key="tour.drill",
            action="navigate",
            skippable=True,
        ),
        TourStep(
            seq=7,
            route="/radar",
            target_selector="[data-tour='radar-chart']",
            title="能力雷达（预留）",
            body="本入口为预留：产品知识、沟通技巧、议价能力、平台规则四维能力雷达"
            "将在演练与考试上线后自动更新，先完成提问即可积累学习记录。",
            body_key="tour.radar",
            action="navigate",
            skippable=True,
        ),
    ],
)

#: 默认导览标识（首登分步导览；API 与前端的缺省 tour_id）
DEFAULT_TOUR_ID: str = FIRST_LOGIN_TOUR.tour_id


class TourRegistry:
    """导览配置注册表：同 tour_id 版本单调递增校验（版本化的守门员）。

    - ``register``：新配置入库；同 tour_id 已存在时，新版本必须严格大于旧版本
      （整型 (major, minor) 比较），否则 :class:`TourVersionError`——既防配置回滚，
      也强制「界面改版必须升主版本」以驱动重显门控；
    - 线程安全（读多写少，锁只护写路径与注册表结构）；
    - 生产形态为随码入库的版本化 JSON 在启动期灌入本注册表（SR §2 双层配置），
      本迭代以内置常量 FIRST_LOGIN_TOUR 灌入，JSON 外置随前端构建链路迭代接入。
    """

    def __init__(self, configs: Sequence[TourConfig] = ()) -> None:
        self._lock = threading.Lock()
        self._tours: dict[str, TourConfig] = {}
        for config in configs:
            self.register(config)

    def register(self, config: TourConfig) -> TourConfig:
        """注册/升级一份导览：同 tour_id 版本必须严格递增，返回入册配置。"""
        with self._lock:
            existing = self._tours.get(config.tour_id)
            if existing is not None:
                new_pair = parse_tour_version(config.version)
                old_pair = parse_tour_version(existing.version)
                if new_pair <= old_pair:
                    raise TourVersionError(
                        f"导览 {config.tour_id} 版本必须单调递增："
                        f"已有 {existing.version}，收到 {config.version}"
                    )
            self._tours[config.tour_id] = config
            return config

    def get(self, tour_id: str) -> TourConfig | None:
        """按标识取导览配置；不存在返回 None。"""
        with self._lock:
            return self._tours.get(tour_id)

    def require(self, tour_id: str) -> TourConfig:
        """按标识取导览配置；不存在抛 KeyError（中文文案，API 层映射 40404）。"""
        config = self.get(tour_id)
        if config is None:
            raise KeyError(f"导览不存在：tour_id={tour_id}")
        return config

    def tour_ids(self) -> list[str]:
        """已注册导览标识列表（排序稳定，供诊断与测试）。"""
        with self._lock:
            return sorted(self._tours)

    def __iter__(self) -> Iterator[TourConfig]:
        """遍历当前全部导览配置（快照迭代，避免遍历期并发写）。"""
        with self._lock:
            return iter(list(self._tours.values()))


def build_default_registry() -> TourRegistry:
    """构建默认注册表：灌入内置 v1 首登导览（进程内可多次调用，互不影响）。"""
    return TourRegistry([FIRST_LOGIN_TOUR])
