# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练评分引擎（FR-TR-003，SR-TR-003 §6.2 多维评分 + 优秀回复差距分析；决策 D4 可解释）。

围绕同一可插拔协议 :class:`DrillScorer`（与 tutor LlmClient 同模式）：

- :class:`FakeDrillScorer`——确定性评分桩（demo/测试）：按 rubric 评分点关键词
  覆盖度打 0-100 三维分（产品准确性/话术/商务策略），逐命中点给出作答原文依据
  （evidence.quote 为作答子串），未命中点计入 missed_points；不发起任何网络请求；
- :class:`DeepSeekDrillScorer`——DeepSeek-V3 生产形态（OpenAI 兼容 JSON mode，
 低温 0.1）：未配置 api_key 时 :meth:`score` 即抛 :class:`ScorerConfigError`（中文）；
  transport 可注入（httpx.MockTransport）离线验证解析/结构校验路径；
- :func:`build_gap_analysis`——差距分析：新人作答 vs 优秀回复逐维对比，
  未命中评分点构成缺失要点清单，双向携带原文引用（evidence / excellent_quote）。

评分失败口径（决策 D4）：评分器抛 :class:`ScoringError`（含 LLM 超时/解析失败）时，
服务层降级 status=pending_manual（TR-2001），绝不产出无依据分数。

依据文档：docs/07-实现方案/TR/SR-TR-003-实战演练系统.md §6.2、§9（test_score_explainable/gap_analysis）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from services.training.core.drill.casebank import (
    DIM_LABELS,
    DRILL_DIMS,
    DrillCase,
    RubricPoint,
)
from foundation.net.egress import EgressGuard

# --------------------------------------------------------------------------- 常量与异常

#: DeepSeek API Key 环境变量（demo 环境未配置即视为不可用，调用时中文报错）
ENV_API_KEY: str = "TR_DRILL_DEEPSEEK_API_KEY"
#: DeepSeek 服务入口环境变量（OpenAI 兼容端点；缺省官方域名）
ENV_BASE_URL: str = "TR_DRILL_DEEPSEEK_BASE_URL"
#: 缺省服务入口（DeepSeek-V3 官方 OpenAI 兼容端点）
DEFAULT_BASE_URL: str = "https://api.deepseek.com"
#: 出域用途编码（foundation 出域目的地注册表 purposes 口径，llm_chat；CR S3 修复）
EGRESS_PURPOSE_LLM_CHAT: str = "llm_chat"
#: 出域调用方标识（egress_call_log 留痕 caller 字段；CR S3 修复）
EGRESS_CALLER: str = "training.drill"
#: 评分 Prompt 版本（校准回路 bump，SR §7 tr.drill.score.prompt_version）
SCORE_PROMPT_VERSION: str = "tr.drill.score@v1"
#: 证据引用窗口（命中关键词左右各取字符数，保证 quote 为作答子串）
EVIDENCE_WINDOW: int = 12
#: 优秀回复侧引用的最大句长（截断保护）
EXCERPT_MAX: int = 60

#: 评分 Prompt（DeepSeek 形态；三维 rubric 逐点判定 + 原文依据强制，决策 D4）
SCORE_PROMPT: str = """你是外贸销售培训评分官。基于【评分要点 rubric】对【新人作答】逐维度评分。
维度：product_accuracy(产品准确性) / script(话术) / strategy(商务策略)。
要求：① 严格对照 rubric 每个评分点判定命中与否；② 每个维度分数必须附 evidence：
引用作答原文片段并说明对应评分点；③ 未命中的评分点列入 missed_points；
④ 严禁输出无原文依据的分数。
输入：询盘上下文、rubric、新人作答。输出 JSON：
{"dims":[{"dim":"product_accuracy","score":82,"evidence":[{"point_id":"P1","quote":"…","note":"…"}],
"missed_points":["P3"]}],"total":80,"comment":"…"}"""


class ScoringError(RuntimeError):
    """评分下游故障（LLM 超时 / 响应不可解析 / 结构非法）：服务层据此降级 pending_manual。"""


class ScorerConfigError(ScoringError):
    """评分器配置缺失（如未配置 DeepSeek API Key）：调用前即失败。"""


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class Evidence:
    """单条评分依据：命中评分点 + 作答原文引用（决策 D4：无依据不产出分数）。"""

    point_id: str
    quote: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"point_id": self.point_id, "quote": self.quote, "note": self.note}


@dataclass(frozen=True)
class DimScore:
    """单维度得分：分数 + 依据清单 + 未命中评分点。"""

    dim: str
    label: str
    score: int
    evidence: tuple[Evidence, ...]
    missed_points: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dim": self.dim,
            "label": self.label,
            "score": self.score,
            "evidence": [item.to_dict() for item in self.evidence],
            "missed_points": list(self.missed_points),
        }


@dataclass(frozen=True)
class ScoreOutput:
    """一次评分输出：三维分 + 总分（三维均分）+ 总评。"""

    dims: tuple[DimScore, ...]
    total: int
    comment: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dims": [dim.to_dict() for dim in self.dims],
            "total": self.total,
            "comment": self.comment,
        }


# --------------------------------------------------------------------------- 可插拔协议


@runtime_checkable
class DrillScorer(Protocol):
    """评分器协议（可插拔）：demo 注入 FakeDrillScorer，生产注入 DeepSeekDrillScorer。"""

    def score(self, case: DrillCase, answer: str) -> ScoreOutput:
        """对指定案例的作答评分（同步语义；异步由生产包装层承接）。"""
        ...


# --------------------------------------------------------------------------- 确定性评分桩（demo/测试）


class FakeDrillScorer:
    """确定性评分桩：rubric 关键词覆盖度 → 0-100 三维分 + 原文依据（demo 口径）。

    记录 ``calls``（调用次数）供幂等/降级测试断言；同输入输出完全一致。
    """

    def __init__(self) -> None:
        self.calls: int = 0

    def score(self, case: DrillCase, answer: str) -> ScoreOutput:
        self.calls += 1
        dims: list[DimScore] = []
        for dim in DRILL_DIMS:
            points = [p for p in case.rubric if p.dim == dim]
            evidence: list[Evidence] = []
            missed: list[str] = []
            for point in points:
                hit = next((kw for kw in point.keywords if kw in answer), None)
                if hit is None:
                    missed.append(point.point_id)
                    continue
                pos = answer.index(hit)
                quote = answer[max(0, pos - EVIDENCE_WINDOW) : pos + len(hit) + EVIDENCE_WINDOW]
                evidence.append(
                    Evidence(
                        point_id=point.point_id,
                        quote=quote,
                        note=f"命中评分点 {point.point_id}（关键词「{hit}」）：{point.desc}",
                    )
                )
            score = round(len(evidence) / len(points) * 100) if points else 0
            dims.append(DimScore(dim=dim, label=DIM_LABELS[dim], score=score,
                                 evidence=tuple(evidence), missed_points=tuple(missed)))
        total = round(sum(d.score for d in dims) / len(dims)) if dims else 0
        return ScoreOutput(dims=tuple(dims), total=total, comment="关键词覆盖度评分（Fake 确定性桩）")


# --------------------------------------------------------------------------- 差距分析（vs 优秀回复）


@dataclass(frozen=True)
class GapItem:
    """单条差距：维度归属 + 缺失评分点 + 优秀回复侧原文引用。"""

    dim: str
    label: str
    point_id: str
    desc: str
    excellent_quote: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dim": self.dim,
            "label": self.label,
            "point_id": self.point_id,
            "desc": self.desc,
            "excellent_quote": self.excellent_quote,
        }


@dataclass(frozen=True)
class GapAnalysis:
    """差距分析结构：缺失要点清单（新人作答 vs 优秀回复逐维对比）。"""

    total_points: int
    covered_points: int
    missing_points: tuple[GapItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_points": self.total_points,
            "covered_points": self.covered_points,
            "missing_points": [item.to_dict() for item in self.missing_points],
        }


def _excerpt_with_keyword(text: str, keywords: tuple[str, ...]) -> str:
    """取文本中首个包含任一关键词的句子（按句号切分，句子为原文子串）；兜底头部截断。"""
    for sentence in re.split(r"(?<=[。！？])", text):
        if any(kw in sentence for kw in keywords):
            return sentence.strip()[:EXCERPT_MAX]
    return text[:EXCERPT_MAX]


def build_gap_analysis(case: DrillCase, answer: str) -> GapAnalysis:
    """确定性差距分析：逐评分点关键词判定，未命中者构成缺失要点清单。

    双向引用口径：新人侧依据见评分 evidence（命中时），缺失侧引用优秀回复
    原文句（excellent_quote，为 excellent_reply 子串），供前端并排高亮展示。
    """

    def _covered(point: RubricPoint) -> bool:
        return any(kw in answer for kw in point.keywords)

    covered = sum(1 for point in case.rubric if _covered(point))
    missing = tuple(
        GapItem(
            dim=point.dim,
            label=DIM_LABELS[point.dim],
            point_id=point.point_id,
            desc=point.desc,
            excellent_quote=_excerpt_with_keyword(case.excellent_reply, point.keywords),
        )
        for point in case.rubric
        if not _covered(point)
    )
    return GapAnalysis(total_points=len(case.rubric), covered_points=covered, missing_points=missing)


# --------------------------------------------------------------------------- DeepSeek 生产形态


class DeepSeekDrillScorer:
    """DeepSeek-V3 评分器（OpenAI 兼容 JSON mode，低温 0.1；demo 未配置即报错）。

    - api_key / base_url 解析顺序：构造参数 → 环境变量 → 缺省值；未配置 api_key
      时 :meth:`score` 首次调用即抛 :class:`ScorerConfigError`（中文提示），
      demo 环境应注入 :class:`FakeDrillScorer`；
    - 每次请求前经 ``guard.assert_egress`` 出域白名单断言（CR S3 修复，
      NFR-SEC-06 fail-closed）：guard 未注入时使用空注册表守卫（全部拒绝）——
      生产必须注入已注册 ``api.deepseek.com``（purposes 含 llm_chat、OI-09 已确认）
      的守卫实例；
    - transport 可注入（httpx.MockTransport），单测离线验证响应解析与结构校验；
    - 响应不可解析 / 维度残缺 / 分值越界一律抛 :class:`ScoringError`
      （服务层降级 pending_manual，不产出残缺分数）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        model: str = "deepseek-v3",
        guard: EgressGuard | None = None,
        timeout_s: float = 60.0,
        temperature: float = 0.1,
        transport: Any | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
        self._base_url = (
            base_url if base_url is not None else os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
        ).rstrip("/")
        self._model = model
        # CR S3 修复：未注入守卫时使用空注册表守卫——fail-closed，未注册目的地一律拒绝出域
        # （与 tutor/llm.py DeepSeekLlmClient 同模式；生产必须注入已注册 api.deepseek.com 的守卫）
        self._guard = guard if guard is not None else EgressGuard()
        self._timeout_s = timeout_s
        self._temperature = temperature
        self._transport = transport

    def score(self, case: DrillCase, answer: str) -> ScoreOutput:
        """评分：配置检查 → Prompt 拼装 → JSON mode 调用 → 结构校验 → ScoreOutput。"""
        if not self._api_key:
            raise ScorerConfigError(
                f"未配置环境变量 {ENV_API_KEY}，无法调用 DeepSeek-V3 评分；"
                "demo 环境请注入 FakeDrillScorer，生产环境由运维注入密钥后再启用真实调用"
            )
        prompt = self._build_prompt(case, answer)
        content = self._chat_json(prompt)
        return self._parse(content)

    def _build_prompt(self, case: DrillCase, answer: str) -> str:
        """拼装评分 Prompt（rubric 逐点下发 + 作答原文）。"""
        rubric_text = "\n".join(
            f"- {p.point_id}（{DIM_LABELS[p.dim]}）：{p.desc}" for p in case.rubric
        )
        return (
            f"{SCORE_PROMPT}\n【询盘上下文】{case.inquiry_text}\n{case.context}\n"
            f"【rubric】\n{rubric_text}\n【新人作答】{answer}"
        )

    def _chat_json(self, prompt: str) -> str:
        """调用 /chat/completions（JSON mode），返回首条消息文本；故障统一 ScoringError。

        CR S3 修复：发起连接前必须经 foundation 出域白名单断言（NFR-SEC-06
        fail-closed，与 tutor/llm.py 同模式）——白名单外 / OI-09 未确认即拒，
        无「先发后审」路径。
        """
        from foundation.core.llmhttp import post_chat_completions  # CR A3：公共骨架单点

        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            data = post_chat_completions(
                api_key=self._api_key, base_url=self._base_url, payload=payload,
                timeout_s=self._timeout_s, guard=self._guard,
                caller=EGRESS_CALLER, transport=self._transport,
            )
            return data["choices"][0]["message"]["content"]
        except ValueError as exc:  # 非流式骨架的非 200 转译点（"HTTP <status>"）
            raise ScoringError(f"DeepSeek 评分接口返回 {exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise ScoringError(f"DeepSeek 评分响应不可解析：{exc}") from exc

    def _parse(self, content: str) -> ScoreOutput:
        """结构校验：三维齐全、分值 0-100；不合法即 ScoringError（不产出残缺分数）。"""
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ScoringError(f"DeepSeek 评分输出不是合法 JSON：{exc}") from exc
        raw_dims = raw.get("dims")
        if not isinstance(raw_dims, list) or {d.get("dim") for d in raw_dims} != set(DRILL_DIMS):
            raise ScoringError(f"DeepSeek 评分维度残缺：{raw_dims!r}，要求 {DRILL_DIMS}")
        dims: list[DimScore] = []
        for item in raw_dims:
            score = item.get("score")
            if not isinstance(score, int) or not 0 <= score <= 100:
                raise ScoringError(f"维度 {item.get('dim')!r} 分值非法：{score!r}")
            evidence = tuple(
                Evidence(
                    point_id=str(ev.get("point_id", "")),
                    quote=str(ev.get("quote", "")),
                    note=str(ev.get("note", "")),
                )
                for ev in item.get("evidence", [])
            )
            dims.append(
                DimScore(
                    dim=str(item["dim"]),
                    label=DIM_LABELS[str(item["dim"])],
                    score=score,
                    evidence=evidence,
                    missed_points=tuple(str(p) for p in item.get("missed_points", [])),
                )
            )
        total = raw.get("total")
        if not isinstance(total, int) or not 0 <= total <= 100:
            raise ScoringError(f"总分非法：{total!r}")
        return ScoreOutput(dims=tuple(dims), total=total, comment=str(raw.get("comment", "")))


__all__ = [
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "DEFAULT_BASE_URL",
    "EGRESS_PURPOSE_LLM_CHAT",
    "EGRESS_CALLER",
    "SCORE_PROMPT",
    "SCORE_PROMPT_VERSION",
    "ScoringError",
    "ScorerConfigError",
    "Evidence",
    "DimScore",
    "ScoreOutput",
    "DrillScorer",
    "FakeDrillScorer",
    "GapItem",
    "GapAnalysis",
    "build_gap_analysis",
    "DeepSeekDrillScorer",
]
