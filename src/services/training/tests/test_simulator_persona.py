# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟客户 Persona 层单元测试（FR-TR-002，SR-TR-002 §6.1/§9；迭代 1.2 TDD 阶段①）。

五类场景覆盖（开发规范 §9.1）：
- 等价类：内置六类型（C-01~C-06）对齐 SRS §6.3、扩展注册至 ≥10、System Prompt 组装；
- 边界：难度/立场/情绪/耐心四参数阈值两侧取值；
- 异常：非法 persona_id、重复注册、未知类型、目录守卫不足；
（并发/降级由 engine/store/api 测试文件承载。）

验收 KPI 映射（测试即验收证据）：
- SRS §16.2/§6.3「≥10 种客户类型」→ test_register_demo_extensions_reaches_ten /
  test_catalog_guard_fails_below_ten；
- SRS §6.3 类型清单对齐 → test_builtin_specs_align_srs_63。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.training.core.simulator.persona import (
    BUILT_IN_PERSONAS,
    BUILT_IN_SCENARIOS,
    DEMO_EXTENSION_PERSONAS,
    MIN_PERSONA_TYPES,
    CatalogInsufficientError,
    DuplicatePersonaError,
    PersonaNotFoundError,
    PersonaRegistry,
    PersonaSpec,
    ScenarioNotFoundError,
    StateInit,
    build_system_prompt,
)

#: SRS §6.3 表逐行对齐口径（编号/类型名/训练目标/行为特征锚点词）
EXPECTED_SRS_63: dict[str, dict[str, object]] = {
    "C-01": {"name": "印度砍价型", "points": ["坚守底价", "价值传递", "让步节奏"], "trait_kw": "最低价"},
    "C-02": {"name": "欧美专业型", "points": ["专业度展示", "信任建立"], "trait_kw": "认证"},
    "C-03": {"name": "中东关系型", "points": ["关系营销", "耐心跟进"], "trait_kw": "感情"},
    "C-04": {"name": "东南亚价格敏感型", "points": ["灵活组合方案", "风险控制"], "trait_kw": "MOQ"},
    "C-05": {"name": "大客户谈判型", "points": ["长期策略", "高层互动"], "trait_kw": "独家代理"},
    "C-06": {"name": "投诉型", "points": ["危机处理", "情绪安抚"], "trait_kw": "赔偿"},
}


def make_spec(persona_id: str = "C-90", **overrides) -> PersonaSpec:
    """构造合法 PersonaSpec（测试基线样本，按需覆盖字段）。"""
    fields: dict = dict(
        persona_id=persona_id,
        name="测试客户型",
        profile="用于单测的合成客户画像",
        behavior_traits=["特征甲", "特征乙"],
        hidden_needs="真实在意交期稳定性与返修率",
        difficulty_base=3,
        state_init=StateInit(price_stance=0.5, emotion=0, patience=100),
        assessment_points=["考核点甲"],
    )
    fields.update(overrides)
    return PersonaSpec(**fields)


class TestBuiltInSix:
    """等价类：内置六类型（SRS §6.3 客户类型库最低配置）。"""

    def test_registry_default_contains_six_builtin(self) -> None:
        """默认注册表恰好内置 C-01~C-06（内置 6 类 + 扩展注册到 ≥10 的分工口径）。"""
        registry = PersonaRegistry()
        assert registry.persona_ids() == [f"C-0{i}" for i in range(1, 7)]
        assert registry.count() == 6

    def test_builtin_specs_align_srs_63(self) -> None:
        """六内置 persona 与 SRS §6.3 逐行对齐：类型名/训练目标/行为特征/画像非空。"""
        assert set(BUILT_IN_PERSONAS) == set(EXPECTED_SRS_63)
        for persona_id, expected in EXPECTED_SRS_63.items():
            spec = BUILT_IN_PERSONAS[persona_id]
            assert spec.name == expected["name"], persona_id
            assert spec.assessment_points == expected["points"], persona_id
            traits_joined = "；".join(spec.behavior_traits)
            assert str(expected["trait_kw"]) in traits_joined, persona_id
            assert len(spec.profile) >= 8 and len(spec.hidden_needs) >= 8, persona_id
            assert 1 <= spec.difficulty_base <= 5, persona_id
            assert 0.0 <= spec.state_init.price_stance <= 1.0, persona_id
            assert -2 <= spec.state_init.emotion <= 2, persona_id
            assert 0 <= spec.state_init.patience <= 100, persona_id


class TestExtensionToTen:
    """等价类 + 异常：扩展注册接口与目录守卫（SRS §16.2 ≥10 类型 KPI）。"""

    def test_register_demo_extensions_reaches_ten(self) -> None:
        """经扩展注册接口注册 4 个 demo 类型后达 10 类（≥10 守卫通过）。"""
        assert len(DEMO_EXTENSION_PERSONAS) == 4
        registry = PersonaRegistry.with_demo_extensions()
        assert registry.count() == MIN_PERSONA_TYPES == 10
        registry.ensure_min_types()  # 不抛即通过

    def test_catalog_guard_fails_below_ten(self) -> None:
        """目录守卫：仅内置 6 类（<10）时启动断言失败，报 CatalogInsufficientError。"""
        registry = PersonaRegistry()
        with pytest.raises(CatalogInsufficientError):
            registry.ensure_min_types()

    def test_register_and_get_roundtrip(self) -> None:
        """register → get 往返一致（扩展接口可用性）。"""
        registry = PersonaRegistry()
        spec = make_spec("C-77")
        registry.register(spec)
        assert registry.get("C-77") is spec
        assert registry.count() == 7

    def test_duplicate_register_rejected(self) -> None:
        """重复注册同 persona_id 拒绝（DuplicatePersonaError）。"""
        registry = PersonaRegistry()
        registry.register(make_spec("C-77"))
        with pytest.raises(DuplicatePersonaError):
            registry.register(make_spec("C-77"))

    def test_unknown_persona_raises(self) -> None:
        """未知类型 get 抛 PersonaNotFoundError（含 C-99/小写非法 id）。"""
        registry = PersonaRegistry()
        for bad in ("C-99", "X-01", "c-01"):
            with pytest.raises(PersonaNotFoundError):
                registry.get(bad)


class TestSpecValidation:
    """边界 + 异常：PersonaSpec pydantic 四参数校验。"""

    def test_difficulty_base_bounds(self) -> None:
        """难度 1/5 合法，0/6 越界拒绝。"""
        assert make_spec(difficulty_base=1).difficulty_base == 1
        assert make_spec(difficulty_base=5).difficulty_base == 5
        for bad in (0, 6, -1):
            with pytest.raises(ValidationError):
                make_spec(difficulty_base=bad)

    def test_persona_id_pattern_rejected(self) -> None:
        """非法编号（X-99/c-01/C-1/C-011）一律 ValidationError。"""
        for bad in ("X-99", "c-01", "C-1", "C-011", "C-"):
            with pytest.raises(ValidationError):
                make_spec(persona_id=bad)

    def test_state_init_bounds(self) -> None:
        """state_init 三参数边界：price_stance [0,1]、emotion [-2,2]、patience [0,100]。"""
        for stance in (0.0, 1.0):
            assert StateInit(price_stance=stance, emotion=0, patience=100).price_stance == stance
        for emotion in (-2, 2):
            assert StateInit(price_stance=0.5, emotion=emotion, patience=100).emotion == emotion
        for patience in (0, 100):
            assert StateInit(price_stance=0.5, emotion=0, patience=patience).patience == patience
        for bad_init in (
            {"price_stance": -0.1, "emotion": 0, "patience": 100},
            {"price_stance": 1.1, "emotion": 0, "patience": 100},
            {"price_stance": 0.5, "emotion": -3, "patience": 100},
            {"price_stance": 0.5, "emotion": 3, "patience": 100},
            {"price_stance": 0.5, "emotion": 0, "patience": -1},
            {"price_stance": 0.5, "emotion": 0, "patience": 101},
        ):
            with pytest.raises(ValidationError):
                StateInit(**bad_init)

    def test_empty_traits_or_points_rejected(self) -> None:
        """行为特征/考核点空列表拒绝（画像四要素不可缺）。"""
        with pytest.raises(ValidationError):
            make_spec(behavior_traits=[])
        with pytest.raises(ValidationError):
            make_spec(assessment_points=[])


class TestScenarioCatalog:
    """等价类：场景目录（≥50 场景由 KB 共创扩展承载，demo 内置覆盖全流程节点种子）。"""

    def test_builtin_scenarios_cover_flow_nodes(self) -> None:
        """内置场景覆盖询盘/报价/议价/样品/订单/物流/售后七个全流程节点。"""
        nodes = {sc.node for sc in BUILT_IN_SCENARIOS.values()}
        assert {"询盘", "报价", "议价", "样品", "订单", "物流", "售后"} <= nodes
        assert all(len(sc.opening) >= 8 for sc in BUILT_IN_SCENARIOS.values())

    def test_unknown_scenario_raises(self) -> None:
        """未知场景 get 抛 ScenarioNotFoundError。"""
        from services.training.core.simulator.persona import ScenarioCatalog

        with pytest.raises(ScenarioNotFoundError):
            ScenarioCatalog().get("sc-not-exist")


class TestSystemPrompt:
    """等价类：System Prompt 组装（SR-TR-002 §6.1 参数化模板）。"""

    def test_build_system_prompt_assembly(self) -> None:
        """画像/隐藏诉求/开场/难度/铁律/知识背景全部注入 Prompt。"""
        spec = BUILT_IN_PERSONAS["C-01"]
        scenario = BUILT_IN_SCENARIOS["sc-bargain-moq"]
        prompt = build_system_prompt(spec, scenario, 3, kb_ctx="（测试背景：CE 认证已取得）")
        assert "模拟客户" in prompt  # 角色锚定（不是 AI 助手）
        assert "铁律" in prompt  # 防越狱铁律段
        assert spec.name in prompt and spec.hidden_needs in prompt
        assert scenario.opening in prompt
        assert "难度：3/5" in prompt
        assert "（测试背景：CE 认证已取得）" in prompt
        assert "价值传递" in prompt  # 考核点仅供内心把握

    def test_build_system_prompt_difficulty_intensity(self) -> None:
        """难度档位映射强度描述：1 宽松 / 5 极难缠。"""
        spec = BUILT_IN_PERSONAS["C-01"]
        scenario = BUILT_IN_SCENARIOS["sc-bargain-moq"]
        assert "配合度高" in build_system_prompt(spec, scenario, 1)
        assert "极难缠" in build_system_prompt(spec, scenario, 5)
        with pytest.raises(ValueError):
            build_system_prompt(spec, scenario, 0)
