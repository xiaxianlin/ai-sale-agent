# 知识产权归甲方所有（COM-IP-01）
"""纠错规则引擎：确定性白名单替换，不引入 LLM（SR-KB-011 §6.4、决策 D4）。

规则可注册（CorrectionRule 协议）：内置格式规整 / 日期统一 / 错别字映射 / 单位归一化
四类规则，执行顺序为 ① 格式规整 → ② 日期统一 → ③ 错别字（长词优先）→ ④ 单位归一化；
每处改动向块级留痕链追加一条 CleanAction(stage=correct)；规则集版本化
（§7 clean.correct.ruleset_version，随 app_config 切换，改动可枚举可回放）。

依据文档：docs/07-实现方案/KB/SR-KB-011-数据清洗.md §6.4；
         docs/03-技术文档/02-开发规范.md §3。
"""
from __future__ import annotations

import re
from typing import Callable, Mapping, Protocol, Sequence

from .base import CleanAction, STAGE_CORRECT, now_iso

# 留痕记录器：(rule_id, before, after) → 由引擎包装为 CleanAction 并维护块内 seq
ActionRecorder = Callable[[str, str, str], None]

# 默认单位归一化映射（示例集；完整表随规则集版本下发，§6.4 注）
DEFAULT_UNIT_MAP: dict[str, str] = {
    "公斤": "kg",
    "千克": "kg",
    "克": "g",
    "PCS": "pcs",
    "Pcs": "pcs",
    "pcs": "pcs",
    "吨": "t",
    "厘米": "cm",
    "毫米": "mm",
}

# 默认错别字映射（示例集；完整表随规则集版本下发，§6.4 注）
DEFAULT_TYPO_MAP: dict[str, str] = {
    "帐号": "账号",
    "登陆": "登录",
    "定单": "订单",
    "联係": "联系",
    "産品": "产品",
}


class CorrectionRule(Protocol):
    """纠错规则协议：apply 返回改写后文本，逐处改动经 record 回调留痕。"""

    rule_id: str

    def apply(self, text: str, record: ActionRecorder) -> str: ...


class FormatNormalizeRule:
    """① 格式规整：全角 → 半角 + 连续空格折叠（§6.4）。整段一次留痕。"""

    rule_id = "format.normalize"

    # 全角→半角映射（示例集：数字 / 字母 / 常用符号；全表入规则集）
    _TRANS = str.maketrans(
        {
            **{ord("０") + i: chr(ord("0") + i) for i in range(10)},  # ０-９ → 0-9
            **{ord("Ａ") + i: chr(ord("A") + i) for i in range(26)},  # Ａ-Ｚ → A-Z
            **{ord("ａ") + i: chr(ord("a") + i) for i in range(26)},  # ａ-ｚ → a-z
            "．": ".",
            "％": "%",
            "（": "(",
            "）": ")",
            "＋": "+",
            "－": "-",
            "：": ":",
            "＃": "#",
            "　": " ",  # 全角空格 → 半角（随后被空白折叠合并）
        }
    )

    def apply(self, text: str, record: ActionRecorder) -> str:
        new = re.sub(r"[ \t]{2,}", " ", text.translate(self._TRANS))
        if new != text:
            record(self.rule_id, text, new)
        return new


class DateFormatRule:
    """② 日期统一：YYYY[./年]M[./月]D[日] → YYYY-MM-DD（§6.4）。整段一次留痕。"""

    rule_id = "format.date"
    _DATE = re.compile(r"(\d{4})[./年](\d{1,2})[./月](\d{1,2})日?")

    def apply(self, text: str, record: ActionRecorder) -> str:
        new = self._DATE.sub(lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", text)
        if new != text:
            record(self.rule_id, text, new)
        return new


class TypoMapRule:
    """③ 错别字映射表替换：长词优先防子串误替换（§6.4），每词一次留痕（替换全部出现）。"""

    rule_id = "typo"

    def __init__(self, typo_map: Mapping[str, str] | None = None) -> None:
        self.typo_map: dict[str, str] = dict(typo_map or DEFAULT_TYPO_MAP)

    def apply(self, text: str, record: ActionRecorder) -> str:
        for wrong in sorted(self.typo_map, key=len, reverse=True):  # 长词优先
            if wrong in text:
                record(f"{self.rule_id}:{wrong}", wrong, self.typo_map[wrong])
                text = text.replace(wrong, self.typo_map[wrong])
        return text


class UnitNormalizeRule:
    """④ 单位归一化：数字 + 单位 → 数字 + 标准单位（§6.4），逐次出现留痕。

    正则交替顺序保证「公斤 / 千克 / 厘米」先于其子串「克 / 米」匹配，防止部分误替换。
    """

    rule_id = "unit"
    _UNIT = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<u>公斤|千克|厘米|毫米|克|PCS|Pcs|pcs|吨)")

    def __init__(self, unit_map: Mapping[str, str] | None = None) -> None:
        self.unit_map: dict[str, str] = dict(unit_map or DEFAULT_UNIT_MAP)

    def apply(self, text: str, record: ActionRecorder) -> str:
        def _sub(match: re.Match[str]) -> str:
            num, unit = match.group("num"), match.group("u")
            std = self.unit_map.get(unit, unit)
            if unit != std:  # 已是标准单位的（如 pcs）不留痕，避免噪声
                record(f"{self.rule_id}:{unit}", match.group(0), f"{num}{std}")  # before=完整匹配段（含空白）
            return f"{num}{std}"

        return self._UNIT.sub(_sub, text)


class Corrector:
    """纠错引擎：按注册顺序执行规则；每处改动追加 CleanAction（seq 块内严格递增）。"""

    def __init__(
        self,
        rules: Sequence[CorrectionRule] | None = None,
        *,
        version: str = "clean.rules@v3",
    ) -> None:
        self._rules: list[CorrectionRule] = list(rules or [])
        self.version = version  # 规则集版本（§7 clean.correct.ruleset_version）

    def register(self, rule: CorrectionRule) -> None:
        """注册纠错规则（规则可注册扩展点：新规则不改引擎代码）。"""
        self._rules.append(rule)

    def run(self, text: str, actions: list[CleanAction] | None = None) -> str:
        """执行全部规则并留痕，返回纠错后文本。

        actions 传入块级留痕链以延续块内 seq（与 dedup / mask 段共用一条链）。
        """
        actions = actions if actions is not None else []

        def _record(rule_id: str, before: str, after: str) -> None:
            actions.append(
                CleanAction(
                    seq=len(actions) + 1,
                    stage=STAGE_CORRECT,
                    rule_id=rule_id,
                    before=before,
                    after=after,
                    ts=now_iso(),
                )
            )

        for rule in self._rules:
            text = rule.apply(text, _record)
        return text


def default_corrector() -> Corrector:
    """默认纠错器：格式规整 → 日期统一 → 错别字 → 单位归一化（§6.4 ①②③④ 顺序）。"""
    return Corrector(
        [
            FormatNormalizeRule(),
            DateFormatRule(),
            TypoMapRule(),
            UnitNormalizeRule(),
        ]
    )
