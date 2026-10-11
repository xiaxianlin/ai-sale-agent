# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""复盘报告生成器（FR-TR-002，SR-TR-002 §6.3 复盘自动生成）。

链路：对话全 transcript → LLM（协议 :class:`DebriefLlm`）→ DebriefReport
（scores 三维：目标达成/话术/策略；改进建议三段；knowledge_refs 关联带教
素材）→ 复盘存储（协议 + 内存实现，生产形态落 exercise.review_report，
03-数据库设计 §3.3；评分明细供 FR-TR-004 雷达图 / FR-TR-006 主管看板消费）。

FakeDebriefLlm 确定性规则（demo 口径，可回归测试）：
- 考核点逐项判定：关键词点按 trainee 消息扫描（证据 = 首条命中 seq）；
  行为规则点（坚守底价/让步节奏/耐心跟进）按让价次数与轮数判定；
- scores：goal_achievement=命中占比百分数；talk_skill=40+10×价值轮（≤95）；
  strategy=90-15×让价次数（下限 40）；
- suggestions 恒三段：价值短板 / 让步纪律 / 结局跟进（deal/breakdown/timeout）。

降级（demo 偏差标注）：SR-TR-002 §8 为「复盘失败落重试队列 + REPORT_NOT_READY」；
本迭代降级为**规则兜底报告**（degraded=true，model=rule-fallback）——保证学员
当场可取到结构完整的复盘，重试队列随联调迭代接入。

验收 KPI 映射：SRS §16.2「评分一致性 ≥85%」要求评分明细可复核——本报告的
goal_achievement 逐点携带 evidence_seq，供销售主管抽检对回。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from services.training.core.simulator.engine import (
    BARGAIN_WORDS,
    SimulatorLlmError,
    has_any_word,
)
from services.training.core.simulator.persona import PersonaSpec

# --------------------------------------------------------------------------- 考核点关键词表

#: 考核点 → 关键词表（命中任一即判达成；行为规则点为空表走专用规则）
POINT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "坚守底价": (),  # 行为规则点：整场让价次数 ≤1 判达成
    "价值传递": ("认证", "证书", "交期", "质量", "参数", "品质", "工艺", "标准", "优势", "检测"),
    "让步节奏": (),  # 行为规则点：整场让价次数 ≤2 判达成
    "专业度展示": ("参数", "认证", "标准", "工艺", "规格", "检测", "证书"),
    "信任建立": ("验厂", "案例", "客户", "合作", "参考", "口碑"),
    "关系营销": ("节日", "家庭", "文化", "问候", "当地", "市场"),
    "耐心跟进": (),  # 行为规则点：轮数 ≥3 判达成
    "灵活组合方案": ("方案", "组合", "账期", "MOQ", "包邮", "运输"),
    "风险控制": ("定金", "合同", "条款", "风险", "保险", "保障"),
    "长期策略": ("年度", "长期", "规划", "代理", "战略", "布局"),
    "高层互动": ("高层", "总经理", "老板", "拜访", "决策", "会面"),
    "危机处理": ("抱歉", "理解", "整改", "方案", "负责", "补救"),
    "情绪安抚": ("抱歉", "安抚", "理解", "优先", "尽快", "补偿"),
    "需求挖掘": ("需求", "用途", "市场", "目标", "计划", "预算"),
    "主动推进": ("建议", "下一步", "安排", "推进", "确认", "时间表"),
    "物流方案": ("交期", "物流", "运输", "清关", "运费", "船期"),
    "预期管理": ("时间表", "进度", "通知", "更新", "跟踪", "节点"),
    "差异化价值": ("差异", "独特", "优势", "对比", "不同", "领先"),
    "长期跟进": ("跟进", "回访", "维护", "联系", "复购", "关系"),
}

#: 价值词口径（话术评分用；与引擎 VALUE_WORDS 同源子集，中文口径）
VALUE_TERMS: tuple[str, ...] = POINT_KEYWORDS["价值传递"]

#: 结局分支的第三段建议模板（deal/breakdown/timeout）
VERDICT_SUGGESTION: dict[str, str] = {
    "deal": "趁热打铁：24 小时内跟进 PI 与样品安排，锁定订单。",
    "breakdown": "先安抚情绪再谈方案：道歉、整改承诺、时间表三步走。",
    "timeout": "对话节奏偏慢，下一场主动抛出方案与时间表推进决策。",
}


# --------------------------------------------------------------------------- 数据契约


class PointResult(BaseModel):
    """单考核点判定（证据 = 首条命中的 trainee 消息 seq）。"""

    point: str
    hit: bool
    evidence_seq: list[int] = Field(default_factory=list)


class DebriefReport(BaseModel):
    """复盘报告（exercise.review_report 的进程内形态，SR-TR-002 §4/§6.3）。"""

    session_id: str
    persona_id: str
    scenario_id: str
    verdict: Literal["deal", "breakdown", "timeout"]
    rounds: int = Field(ge=0)
    goal_achievement: list[PointResult]
    scores: dict[str, int]
    strengths: list[str]
    weaknesses: list[str]
    suggestions: list[str]
    knowledge_refs: list[str]
    degraded: bool = False
    """LLM 故障降级兜底标记（demo 口径）。"""
    model: str = "fake-simulator-llm"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("scores")
    @classmethod
    def _scores_in_range(cls, value: dict[str, int]) -> dict[str, int]:
        """三维评分必须落在 0-100（评分一致性抽检的合法性前提）。"""
        for name, score in value.items():
            if not 0 <= score <= 100:
                raise ValueError(f"评分越界（0-100）：{name}={score}")
        return value


# --------------------------------------------------------------------------- 存储协议与内存实现


class DebriefStore(Protocol):
    """复盘存储协议（生产 PG：exercise.review_report；demo 内存实现）。"""

    def save(self, report: DebriefReport) -> None:
        """保存/覆盖报告（同会话重复生成覆盖写）。"""
        ...

    def get(self, session_id: str) -> DebriefReport | None:
        """按会话 ID 取报告；未生成返回 None（API 层映射 40401）。"""
        ...


class InMemoryDebriefStore:
    """内存复盘存储（demo/测试）：线程安全，同会话覆盖写。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reports: dict[str, DebriefReport] = {}

    def save(self, report: DebriefReport) -> None:
        """保存报告（同会话覆盖写，幂等语义）。"""
        with self._lock:
            self._reports[report.session_id] = report

    def get(self, session_id: str) -> DebriefReport | None:
        """按会话 ID 取报告（未生成返回 None）。"""
        with self._lock:
            return self._reports.get(session_id)


# --------------------------------------------------------------------------- LLM 协议与确定性桩


class DebriefLlm(Protocol):
    """复盘 LLM 协议（可插拔）。

    生产形态注入 DeepSeek-V3 结构化输出（temperature=0.2，SR-TR-002 §6.3）；
    demo/测试注入 :class:`FakeDebriefLlm`。实现须把底层故障包装为
    :class:`SimulatorLlmError`（引擎同源异常）抛出。
    """

    def generate_debrief(
        self,
        *,
        system: str,
        persona: PersonaSpec,
        transcript: list[dict[str, Any]],
        assessment_points: list[str],
        verdict: str,
        final_state: dict[str, Any],
    ) -> dict[str, Any]:
        """产出复盘载荷字典（goal_achievement/scores/strengths/weaknesses/suggestions/knowledge_refs）。"""
        ...


def build_debrief_system_prompt(persona: PersonaSpec, verdict: str, points: list[str]) -> str:
    """组装复盘 System Prompt（教练角色 + 考核点 + 结局口径）。"""
    return (
        "你是外贸销售教练。依据对话全文与考核点输出复盘 JSON："
        "goal_achievement 逐考核点判定 hit 并引用消息 seq 作证据；"
        "scores 仅含 goal_achievement/talk_skill/strategy 三维（0-100）；"
        "strengths/weaknesses/suggestions 各不超过 3 条且建议可执行；"
        "knowledge_refs 给出应补学的带教素材。"
        f"结局判定：{verdict}。客户类型：{persona.name}。考核点：{'/'.join(points)}。"
    )


class FakeDebriefLlm:
    """确定性复盘桩（demo/测试）：按考核点逐项评分 + 改进建议三段。

    记录 calls / last_system 供测试断言（考核点与结局已注入 Prompt）。
    """

    def __init__(self) -> None:
        self.calls: int = 0
        self.last_system: str = ""

    def generate_debrief(
        self,
        *,
        system: str,
        persona: PersonaSpec,
        transcript: list[dict[str, Any]],
        assessment_points: list[str],
        verdict: str,
        final_state: dict[str, Any],
    ) -> dict[str, Any]:
        """确定性生成复盘载荷（规则见模块 docstring）。"""
        self.calls += 1
        self.last_system = system
        trainee_msgs = [
            (int(m.get("seq", 0)), str(m.get("content", "")))
            for m in transcript
            if m.get("role") == "trainee"
        ]
        bargain_count = sum(1 for _, content in trainee_msgs if has_any_word(content, BARGAIN_WORDS))
        value_count = sum(1 for _, content in trainee_msgs if has_any_word(content, VALUE_TERMS))

        goal = [
            {"point": point, "hit": hit, "evidence_seq": seqs}
            for point, hit, seqs in (
                (point, *self._judge(point, trainee_msgs, bargain_count, len(trainee_msgs), verdict))
                for point in assessment_points
            )
        ]
        hits = sum(1 for g in goal if g["hit"])
        unhit = [g["point"] for g in goal if not g["hit"]]
        scores = {
            "goal_achievement": round(100 * hits / len(goal)) if goal else 0,
            "talk_skill": min(95, 40 + 10 * value_count),
            "strategy": 90 if bargain_count == 0 else max(40, 90 - 15 * bargain_count),
        }
        return {
            "goal_achievement": goal,
            "scores": scores,
            "strengths": [f"「{g['point']}」达成到位" for g in goal if g["hit"]][:3],
            "weaknesses": [f"「{g['point']}」未达成" for g in goal if not g["hit"]][:3],
            "suggestions": self._suggestions(unhit, bargain_count, verdict),
            "knowledge_refs": [f"带教素材库·{point}案例" for point in unhit[:2]]
            + ["话术库·价值传递三步法", "知识库·FR-KB-030 产品资料检索"],
        }

    @staticmethod
    def _judge(
        point: str,
        trainee_msgs: list[tuple[int, str]],
        bargain_count: int,
        rounds: int,
        verdict: str,
    ) -> tuple[bool, list[int]]:
        """单考核点判定：关键词扫描（证据 seq）或行为规则（坚守底价/让步节奏/耐心跟进）。"""
        if point == "坚守底价":  # 行为规则点：整场让价次数 ≤1
            return bargain_count <= 1, []
        if point == "让步节奏":  # 行为规则点：整场让价次数 ≤2
            return bargain_count <= 2, []
        if point == "耐心跟进":  # 行为规则点：轮数 ≥3
            return rounds >= 3, []
        keywords = POINT_KEYWORDS.get(point, ())
        for seq, content in trainee_msgs:
            if any(word in content for word in keywords):
                return True, [seq]
        return False, []


    @staticmethod
    def _suggestions(unhit: list[str], bargain_count: int, verdict: str) -> list[str]:
        """改进建议三段：价值短板 / 让步纪律 / 结局跟进（恒 3 条，可执行口径）。"""
        first = (
            f"围绕「{unhit[0]}」补足数据与证据，先讲价值再谈价格。"
            if unhit
            else "保持当前节奏，把已验证的价值点写进跟进邮件固化。"
        )
        second = (
            f"本轮让价 {bargain_count} 次偏多，下次让步必须换取条件（增量订单/交期/账期）。"
            if bargain_count
            else "让步纪律良好，继续坚持先价值后价格的谈判顺序。"
        )
        return [first, second, VERDICT_SUGGESTION.get(verdict, VERDICT_SUGGESTION["timeout"])]


# --------------------------------------------------------------------------- 生成器


class DebriefGenerator:
    """复盘生成器：会话转录 → LLM → DebriefReport → 存储（LLM 故障降级兜底）。"""

    def __init__(self, llm: DebriefLlm, store: DebriefStore) -> None:
        self.llm = llm
        self.store = store

    def generate(
        self,
        *,
        persona: PersonaSpec,
        session_id: str,
        scenario_id: str,
        verdict: str,
        transcript: list[dict[str, Any]],
        final_state: dict[str, Any],
        difficulty: int,
    ) -> DebriefReport:
        """生成并落库复盘报告（difficulty 注入 Prompt 备考，报告维度由规则产出）。"""
        system = build_debrief_system_prompt(persona, verdict, list(persona.assessment_points))
        try:
            raw = self.llm.generate_debrief(
                system=system,
                persona=persona,
                transcript=transcript,
                assessment_points=list(persona.assessment_points),
                verdict=verdict,
                final_state=final_state,
            )
        except SimulatorLlmError:
            return self._fallback(persona, session_id, scenario_id, verdict, transcript)
        rounds = sum(1 for m in transcript if m.get("role") == "trainee")
        report = DebriefReport(
            session_id=session_id,
            persona_id=persona.persona_id,
            scenario_id=scenario_id,
            verdict=verdict,  # type: ignore[arg-type]  # verdict 由引擎三分支产出
            rounds=rounds,
            goal_achievement=[PointResult(**item) for item in raw["goal_achievement"]],
            scores={str(k): int(v) for k, v in raw["scores"].items()},
            strengths=[str(s) for s in raw.get("strengths", [])],
            weaknesses=[str(s) for s in raw.get("weaknesses", [])],
            suggestions=[str(s) for s in raw.get("suggestions", [])],
            knowledge_refs=[str(s) for s in raw.get("knowledge_refs", [])],
            degraded=False,
        )
        self.store.save(report)
        return report

    def _fallback(
        self,
        persona: PersonaSpec,
        session_id: str,
        scenario_id: str,
        verdict: str,
        transcript: list[dict[str, Any]],
    ) -> DebriefReport:
        """LLM 故障兜底：规则式最小报告（degraded=true，学员当场可取）。"""
        rounds = sum(1 for m in transcript if m.get("role") == "trainee")
        report = DebriefReport(
            session_id=session_id,
            persona_id=persona.persona_id,
            scenario_id=scenario_id,
            verdict=verdict,  # type: ignore[arg-type]
            rounds=rounds,
            goal_achievement=[
                PointResult(point=point, hit=False, evidence_seq=[])
                for point in persona.assessment_points
            ],
            scores={"goal_achievement": 0, "talk_skill": 0, "strategy": 0},
            strengths=[],
            weaknesses=[f"「{point}」待人工复核" for point in persona.assessment_points[:3]],
            suggestions=[
                "复盘生成降级：AI 复盘暂不可用，以下为规则兜底建议。",
                "请教练人工复核本段对话的考核点达成情况。",
                "稍后可重新触发复盘生成（或联系管理员检查模型服务）。",
            ],
            knowledge_refs=["带教素材库·人工复盘清单"],
            degraded=True,
            model="rule-fallback",
        )
        self.store.save(report)
        return report


__all__ = [
    "POINT_KEYWORDS",
    "VALUE_TERMS",
    "VERDICT_SUGGESTION",
    "PointResult",
    "DebriefReport",
    "DebriefStore",
    "InMemoryDebriefStore",
    "DebriefLlm",
    "FakeDebriefLlm",
    "build_debrief_system_prompt",
    "DebriefGenerator",
]
