# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""导览配置模型单测（NFR-USA-03，迭代 1.1；SR-NFR-USA-03 §6.1/§6.6/§9）。

覆盖：
- 内置 v1 首登导览结构：tour_id/version/步骤 seq 连续递增、核心路径覆盖
  （提问→看来源引用→查缺口清单→（预留）演练/雷达）；
- CI 选择器守护门禁：逐步骤断言 target_selector 为 [data-tour=...] 形态且
  锚点值唯一（class/文本/data-guide 定位一律拒载——模型层 + 本断言双重拦截）；
- 版本单调递增校验：同 tour_id 注册相等/更低版本 → TourVersionError；
  更高版本/不同 tour_id → 通过；
- 版本与路由格式校验：非法版本串、非 / 开头路由 → ValidationError。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.training.core.guidance.config import (
    DEFAULT_TOUR_ID,
    FIRST_LOGIN_TOUR,
    TOUR_SELECTOR_PATTERN,
    TourConfig,
    TourRegistry,
    TourStep,
    TourVersionError,
    build_default_registry,
    parse_tour_version,
)


# --------------------------------------------------------------------------- 内置 v1 导览结构


class TestBuiltinTourStructure:
    """内置 v1 首登导览：结构完整性与核心路径覆盖。"""

    def test_builtin_tour_identity(self) -> None:
        """内置导览标识为 first-login、版本 1.0、启用且受众为 trainee。"""
        assert FIRST_LOGIN_TOUR.tour_id == DEFAULT_TOUR_ID == "first-login"
        assert FIRST_LOGIN_TOUR.version == "1.0"
        assert FIRST_LOGIN_TOUR.major == 1 and FIRST_LOGIN_TOUR.minor == 0
        assert FIRST_LOGIN_TOUR.enabled is True
        assert "trainee" in FIRST_LOGIN_TOUR.audience

    def test_steps_seq_continuous(self) -> None:
        """步骤 seq 从 1 连续递增（无跳号/重复）。"""
        seqs = [step.seq for step in FIRST_LOGIN_TOUR.steps]
        assert seqs == list(range(1, len(FIRST_LOGIN_TOUR.steps) + 1))
        assert len(FIRST_LOGIN_TOUR.steps) >= 5  # 核心路径至少覆盖 5 个页面节点

    def test_core_path_covered(self) -> None:
        """核心路径覆盖：提问（/tutor 输入）→ 看来源引用 →（导师历史）→ 查缺口清单 →
        预留演练/雷达（route 与锚点逐项断言，锚定 SR-TR-001 §5 导师端点对应页面）。"""
        routes = [step.route for step in FIRST_LOGIN_TOUR.steps]
        anchors = [step.anchor for step in FIRST_LOGIN_TOUR.steps]
        assert "/tutor" in routes
        assert "tutor-ask" in anchors  # 提问输入区
        assert "tutor-sources" in anchors  # 来源引用块
        assert "/gaps" in routes and "gap-list" in anchors  # 缺口清单
        assert "/exercise" in routes and "drill-entry" in anchors  # 预留演练
        assert "/radar" in routes and "radar-chart" in anchors  # 预留雷达

    def test_steps_chinese_copy(self) -> None:
        """全部步骤标题/正文为中文非空（NFR-USA-02 中文化）。"""
        for step in FIRST_LOGIN_TOUR.steps:
            assert step.title.strip(), f"步骤 {step.seq} 标题为空"
            assert step.body.strip(), f"步骤 {step.seq} 正文为空"
            assert not step.title.isascii(), f"步骤 {step.seq} 标题应为中文：{step.title}"

    def test_default_registry_contains_builtin(self) -> None:
        """默认注册表灌入内置导览；require 可取、tour_ids 含 first-login。"""
        registry = build_default_registry()
        assert registry.require("first-login") is FIRST_LOGIN_TOUR
        assert "first-login" in registry.tour_ids()


# --------------------------------------------------------------------------- CI 选择器守护门禁


class TestSelectorGuard:
    """CI 守护：target_selector 必须 [data-tour=...] 形态（SR §6.6 门禁的测试层）。"""

    def test_builtin_steps_all_anchor_data_tour(self) -> None:
        """内置导览逐步骤：选择器匹配 [data-tour='xxx'] 规约（含锚点值 kebab-case）。"""
        for step in FIRST_LOGIN_TOUR.steps:
            assert TOUR_SELECTOR_PATTERN.match(step.target_selector), (
                f"步骤 {step.seq} 选择器违反 data-tour 规约：{step.target_selector}"
            )

    def test_builtin_anchors_unique(self) -> None:
        """内置导览锚点值唯一（一个锚点只讲一件事）。"""
        anchors = [step.anchor for step in FIRST_LOGIN_TOUR.steps]
        assert len(anchors) == len(set(anchors))

    @pytest.mark.parametrize(
        "bad_selector",
        [
            ".workbench-button",  # class 定位（样式重构即漂移，禁止）
            "#app",  # id 定位
            "[data-guide='workbench']",  # 非 data-tour 前缀
            "data-tour='workbench'",  # 缺方括号
            "[data-tour='Workbench']",  # 锚点值非 kebab-case（大写）
            "[data-tour='work bench']",  # 锚点值含空格
            "[data-tour='']",  # 空锚点值
            "[data-tour='a'][role='button']",  # 复合选择器（锚点契约只允许单属性）
        ],
    )
    def test_model_rejects_bad_selector(self, bad_selector: str) -> None:
        """模型层拒载非法选择器：ValidationError 且报错含中文规约说明。"""
        with pytest.raises(ValidationError) as exc_info:
            TourStep(seq=1, route="/tutor", target_selector=bad_selector, title="t", body="b")
        assert "data-tour" in str(exc_info.value)

    def test_selector_double_quote_form_allowed(self) -> None:
        """双引号形态 [data-tour="xxx"] 合法（JSON 序列化常见形态），锚点解析一致。"""
        step = TourStep(
            seq=1, route="/tutor", target_selector='[data-tour="tutor-ask"]', title="t", body="b"
        )
        assert step.anchor == "tutor-ask"


# --------------------------------------------------------------------------- 版本化与单调递增


class TestVersioning:
    """版本规约：major.minor 格式 + 同 tour_id 单调递增校验。"""

    @pytest.mark.parametrize(
        "bad_version", ["3", "v3.0", "3.0.0", "3.0.", ".3", "一.零", "3.-1", ""]
    )
    def test_bad_version_rejected(self, bad_version: str) -> None:
        """非法版本串在字段校验即拒（ValidationError）。"""
        with pytest.raises(ValidationError):
            _config_of(bad_version)

    def test_parse_version_tuple(self) -> None:
        """版本解析为整型二元组（比较口径）。"""
        assert parse_tour_version("1.0") == (1, 0)
        assert parse_tour_version("10.23") == (10, 23)

    def test_registry_monotonic_increase(self) -> None:
        """同 tour_id：更高版本可注册（1.0 → 1.1 → 2.0）。"""
        registry = build_default_registry()  # 已含 first-login 1.0
        v1_1 = _bump(FIRST_LOGIN_TOUR, "1.1")
        v2_0 = _bump(v1_1, "2.0")
        assert registry.register(v1_1).version == "1.1"
        assert registry.register(v2_0).version == "2.0"

    @pytest.mark.parametrize("stale_version", ["1.0", "0.9"])
    def test_registry_rejects_non_increase(self, stale_version: str) -> None:
        """同 tour_id：相等或更低版本注册 → TourVersionError（防回滚/防静默覆盖）。"""
        registry = build_default_registry()  # 已含 first-login 1.0
        with pytest.raises(TourVersionError):
            registry.register(_bump(FIRST_LOGIN_TOUR, stale_version))

    def test_registry_minor_bump_must_be_strictly_greater(self) -> None:
        """次版本也须严格递增：1.1 之后不允许再注册 1.1。"""
        registry = build_default_registry()
        registry.register(_bump(FIRST_LOGIN_TOUR, "1.1"))
        with pytest.raises(TourVersionError):
            registry.register(_bump(FIRST_LOGIN_TOUR, "1.1"))

    def test_different_tour_ids_independent(self) -> None:
        """不同 tour_id 版本互不干扰（各自独立单调序列）。"""
        registry = build_default_registry()
        other = FIRST_LOGIN_TOUR.model_copy(update={"tour_id": "exam-intro", "version": "1.0"})
        assert registry.register(other).version == "1.0"
        assert set(registry.tour_ids()) == {"first-login", "exam-intro"}

    def test_registry_require_missing_raises(self) -> None:
        """require 未知导览 → KeyError（中文文案，API 层映射 40404）。"""
        registry = build_default_registry()
        with pytest.raises(KeyError):
            registry.require("no-such-tour")


# --------------------------------------------------------------------------- 测试工具


def _config_of(version: str) -> TourConfig:
    """构造最小合法导览（单步），专供版本格式校验用例隔离失败原因。"""
    return TourConfig(
        tour_id="t",
        version=version,
        title="t",
        steps=[TourStep(seq=1, route="/t", target_selector="[data-tour='a']", title="t", body="b")],
    )


def _bump(base: TourConfig, version: str) -> TourConfig:
    """基于既有导览生成指定版本的新配置（保持其余字段不变，用于版本递增用例）。"""
    return base.model_copy(update={"version": version})
