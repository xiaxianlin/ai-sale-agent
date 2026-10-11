# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""跟进模板槽位填充（FR-AG-004 → FR-KB-003，SR-AG-004 §6.3；迭代 2.3 demo 口径）。

R-03 幻觉防护在跟进侧的落点（价格槽仅结构化来源）：
- 槽位解析顺序：结构化（产品库/客户档案权威字段）→ 自由（会话上下文非关键槽）；
- **结构化强制清单**（默认 {"price"}）：价格槽只接受结构化来源——仅出现在自由槽
  即违规，绝不外发（防 LLM/自由文本编造价格，§7.4/R-03）；
- 槽位缺失 → ok=False 且 text 为空串（不外发残文——渲染失败禁止带占位符外发）；
- 轮次 → 受控场景标签（SR-KB-003 §6.1 scene_taxonomy，词表固定随版本发布）。

纯函数边界：无 IO、无时钟——同入参恒同出参。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

#: 槽位占位符形态：{name} / {price} / {lead_time}（小写下划线）
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

#: 结构化强制槽位（默认价格槽；可注入扩充，如 lead_time——仅可增不可减）
DEFAULT_STRUCTURED_REQUIRED = frozenset({"price"})

#: 轮次 → 受控场景标签（SR-KB-003 §6.1；改词表走版本发布）
ROUND_SCENE: dict[int, str] = {
    1: "首次跟进",
    2: "二次跟进",
    3: "三次跟进",
    4: "最后唤醒",
}


def scene_for_round(round_no: int) -> str:
    """轮次 → 场景标签（词表外轮次拒绝，防越界编造场景）。"""
    if round_no not in ROUND_SCENE:
        raise ValueError(f"未知跟进轮次：{round_no}（受控词表 {sorted(ROUND_SCENE)}）")
    return ROUND_SCENE[round_no]


@dataclass(frozen=True)
class RenderResult:
    """渲染结果：ok=False 时 text 恒为空串（不外发残文）。"""

    ok: bool
    text: str
    missing: tuple[str, ...] = ()       # 缺失槽位（结构化/自由均无值）
    violations: tuple[str, ...] = ()    # 结构化强制槽位违规（仅自由来源或缺失）


def render_followup(
    template: str,
    *,
    structured: Mapping[str, object] | None = None,
    free: Mapping[str, object] | None = None,
    structured_required: frozenset[str] = DEFAULT_STRUCTURED_REQUIRED,
) -> RenderResult:
    """槽位填充（strict 模式）：结构化优先 → 强制槽校验 → 残文防护。

    :param structured: 结构化槽值（产品库/客户档案权威字段，价格唯一合法来源）；
    :param free: 自由槽值（称呼/会话上下文等非关键槽）；
    :param structured_required: 结构化强制清单（注入可扩充）。
    """
    structured = structured or {}
    free = free or {}
    values: dict[str, str] = {}
    missing: list[str] = []
    violations: list[str] = []
    for slot in PLACEHOLDER_RE.findall(template):
        if slot in structured and structured[slot] is not None:
            values[slot] = str(structured[slot])            # ① 结构化权威来源优先
        elif slot in structured_required:
            violations.append(slot)                          # ② 强制槽仅自由来源/缺失 → 违规
        elif slot in free and free[slot] is not None:
            values[slot] = str(free[slot])                   # ③ 自由槽兜底
        else:
            missing.append(slot)                             # ④ 全无来源 → 缺失
    if missing or violations:
        return RenderResult(
            ok=False, text="",
            missing=tuple(sorted(set(missing))),
            violations=tuple(sorted(set(violations))),
        )
    text = template
    for slot, value in values.items():
        text = text.replace("{" + slot + "}", value)
    return RenderResult(ok=True, text=text)


__all__ = [
    "PLACEHOLDER_RE",
    "DEFAULT_STRUCTURED_REQUIRED",
    "ROUND_SCENE",
    "RenderResult",
    "scene_for_round",
    "render_followup",
]
