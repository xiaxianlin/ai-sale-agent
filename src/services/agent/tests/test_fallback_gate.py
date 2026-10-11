# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""兜底判定器单元测试（FR-AG-009，SR-AG-009 §6.1；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：双阈值矩阵全分支（auto_send / human_review / human_review_gap）；
- 边界类：conf==T_high（含）自动、conf==T_low（含）转人工、hit_floor 弱命中按零命中处置；
- 异常类：阈值配置非法 / 判定依赖抛异常 → GATE_ERROR fail-safe（宁误兜底不漏兜底）；
- 优先级：硬校验一票否决序 SENSITIVE > ZERO_HIT > KEY_FIELD_MISSING > FACT_CHECK_FAIL > 阈值；
- 配置化：按意图差异化阈值（询价从严、闲聊从宽，NFR-MAI-02）。

实现方案：docs/07-实现方案/AG/SR-AG-009-低置信度兜底.md §6.1、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import pytest

from services.agent.core.brain.fallback import (
    FallbackEvidence,
    FallbackGate,
    GateThresholds,
)


def ev(**overrides: object) -> FallbackEvidence:
    """判定输入构造器：默认「全绿高置信」证据，用例按需覆盖单字段。"""
    base: dict[str, object] = {
        "intent": "样品",
        "hit_count": 3,
        "top_score": 0.9,
        "self_eval": 1.0,
        "fact_check_ok": True,
        "key_fields_complete": True,
        "sensitive": False,
        "confidence": 0.85,
    }
    base.update(overrides)
    return FallbackEvidence(**base)  # type: ignore[arg-type]


def test_gray_zone_low_confidence_review() -> None:
    """等价类：T_low≤conf<T_high → 转人工（草稿供快速确认），不记缺口。"""
    verdict = FallbackGate().decide(ev(confidence=0.6))
    assert verdict.need_human is True
    assert verdict.action == "human_review"
    assert verdict.reason_code == "LOW_CONFIDENCE"
    assert verdict.gap_record is False


def test_below_t_low_gap_record() -> None:
    """等价类：conf<T_low → 转人工 + 知识缺口记录（human_review_gap）。"""
    verdict = FallbackGate().decide(ev(confidence=0.3))
    assert verdict.need_human is True
    assert verdict.action == "human_review_gap"
    assert verdict.reason_code == "LOW_CONFIDENCE"
    assert verdict.gap_record is True


def test_sensitive_top_priority() -> None:
    """优先级：敏感最优先——不生成不发送，交 FR-AG-005/009（其余证据再好也让位）。"""
    verdict = FallbackGate().decide(ev(sensitive=True))
    assert verdict.need_human is True
    assert verdict.reason_code == "SENSITIVE"


def test_weak_hit_treated_as_zero_hit() -> None:
    """边界类：top_score<hit_floor 弱命中按零命中处置（弱命中=无依据，R-03）。"""
    verdict = FallbackGate().decide(ev(top_score=0.29))
    assert verdict.reason_code == "ZERO_HIT"
    assert verdict.action == "human_review_gap"
    assert verdict.gap_record is True


def test_key_fields_missing() -> None:
    """优先级：关键字段无结构化来源（占位符悬空）→ KEY_FIELD_MISSING。"""
    verdict = FallbackGate().decide(ev(key_fields_complete=False))
    assert verdict.need_human is True
    assert verdict.reason_code == "KEY_FIELD_MISSING"


def test_fact_check_fail() -> None:
    """优先级：关键事实校验失败（价格残留/伪造引用）→ FACT_CHECK_FAIL。"""
    verdict = FallbackGate().decide(ev(fact_check_ok=False))
    assert verdict.need_human is True
    assert verdict.reason_code == "FACT_CHECK_FAIL"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        pytest.param({"sensitive": True, "hit_count": 0, "key_fields_complete": False,
                      "fact_check_ok": False}, "SENSITIVE", id="敏感压倒一切"),
        pytest.param({"hit_count": 0, "key_fields_complete": False,
                      "fact_check_ok": False}, "ZERO_HIT", id="零命中压过字段缺失"),
        pytest.param({"fact_check_ok": False}, "FACT_CHECK_FAIL", id="事实失败压过阈值"),
    ],
)
def test_hard_check_priority_matrix(overrides: dict, expected: str) -> None:
    """优先级矩阵：SENSITIVE > ZERO_HIT > KEY_FIELD_MISSING > FACT_CHECK_FAIL > 双阈值。"""
    verdict = FallbackGate().decide(ev(**overrides))
    assert verdict.reason_code == expected
    assert verdict.need_human is True


@pytest.mark.parametrize(
    "intent,expect_auto",
    [
        pytest.param("询价", False, id="询价从严T_high=0.80"),
        pytest.param("闲聊", True, id="闲聊从宽T_high=0.65"),
    ],
)
def test_intent_differentiated_thresholds(intent: str, expect_auto: bool) -> None:
    """配置化：同 conf=0.68 按意图差异化——询价转人工、闲聊自动（NFR-MAI-02）。"""
    gate = FallbackGate(
        by_intent={
            "询价": {"t_high": 0.80, "t_low": 0.50},
            "闲聊": {"t_high": 0.65, "t_low": 0.35},
        }
    )
    verdict = gate.decide(ev(intent=intent, confidence=0.68))
    assert verdict.need_human is (not expect_auto)
    assert (verdict.reason_code == "") is expect_auto


@pytest.mark.parametrize(
    "confidence,expected_action",
    [
        pytest.param(0.75, "auto_send", id="conf恰等于T_high含边界自动"),
        pytest.param(0.45, "human_review", id="conf恰等于T_low含边界转人工"),
        pytest.param(0.60, "human_review", id="灰区中点转人工"),
    ],
)
def test_threshold_boundaries(confidence: float, expected_action: str) -> None:
    """边界类：双阈值两侧与恰等值三分支（T_high=0.75 / T_low=0.45）。"""
    verdict = FallbackGate().decide(ev(confidence=confidence))
    assert verdict.action == expected_action


def test_invalid_thresholds_fail_safe() -> None:
    """异常类：t_low≥t_high 配置非法 → GATE_ERROR 转人工（不抛出、宁误兜底）。"""
    gate = FallbackGate(thresholds=GateThresholds(t_high=0.40, t_low=0.45))
    verdict = gate.decide(ev())
    assert verdict.need_human is True
    assert verdict.reason_code == "GATE_ERROR"


def test_dependency_exception_fail_safe() -> None:
    """异常类：判定依赖自身异常（脏阈值类型）→ GATE_ERROR fail-safe，不向调用方抛出。"""
    gate = FallbackGate(by_intent={"询价": {"t_high": "high", "t_low": 0.5}})
    verdict = gate.decide(ev(intent="询价"))
    assert verdict.need_human is True
    assert verdict.reason_code == "GATE_ERROR"
