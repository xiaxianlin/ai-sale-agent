# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# diff.py —— 字段级分类对比引擎（FR-KB-022 版本对比，SR-KB-022 §6.2 / §5）。
"""输入两个快照 dict（knowledge_version.snapshot_json），输出结构化 DiffReport。

    DiffReport{
      added[]    : 仅新快照出现的顶层字段名（字段级新增）
      removed[]  : 仅旧快照出现的顶层字段名（字段级删除）
      changed[]  : 两侧均有但值不同的字段 → FieldChange{field, old, new, kind, detail}
    }

三类字段分类（kind = 前端双栏渲染的分派依据，服务端算好、前端零算法，NFR-PER-05）：
    text   文本字段（str）→ 行级 SequenceMatcher → detail.segments：
           both=等值上下文（两侧共显）/ a_only=仅旧（红）/ b_only=仅新（绿），
           附 a_only_lines/b_only_lines 行级差异摘要；
    tree   dict/JSON 字段 → 递归叶子路径 diff → detail.leaves：
           [{path: "$.a.b"/"$[2]", a, b, op: added|removed|modified}]；
    list   列表字段 → 集合差（多重集计数，保持原始顺序）→
           detail.only_in_old / detail.only_in_new；
    scalar 数值/布尔/None 兜底（old/new 直出，无细化 detail）；
    两侧类型不一致（如 str → dict）→ 按 scalar 整字段替换展示（防渲染歧义）。

下划线前缀字段（如 _meta.schema_rev 系统字段）不进对比。
DiffReport.ui_payload() 输出 SR-KB-022 §5 的 UI 数据契约形态
（from/to/summary/fields），大体积 content 只下发差异不整体下发。

偏差标注（相对 SR-KB-022 §6.2 代码草稿）：等值段只产生一个 both 片段（草稿中
equal 命中双分支会连续追加两个相同 both 段，双栏各渲染一次即上下文重复），
渲染语义不变：左栏取 side != b_only，右栏取 side != a_only。

实现方案：docs/07-实现方案/KB/SR-KB-022-版本控制.md §5、§6.2、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import difflib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 文本字段 kind（input/textarea 控件）
KIND_TEXT: str = "text"
#: dict/JSON 结构字段 kind（object/select/multi_select/cascader 等控件）
KIND_TREE: str = "tree"
#: 列表字段 kind（标签数组等）
KIND_LIST: str = "list"
#: 标量兜底 kind（number/switch/date 等，或两侧类型不一致的整字段替换）
KIND_SCALAR: str = "scalar"

#: 双栏片段归属（SR-KB-022 §5 契约：红=a_only / 绿=b_only / 灰=both 两侧共显）
SIDE_BOTH: str = "both"
SIDE_A_ONLY: str = "a_only"
SIDE_B_ONLY: str = "b_only"


def classify_kind(value: Any) -> str:
    """按值类型分类字段：str→text、Mapping→tree、序列→list、其余→scalar。"""
    if isinstance(value, str):
        return KIND_TEXT
    if isinstance(value, Mapping):
        return KIND_TREE
    if isinstance(value, (list, tuple)):
        return KIND_LIST
    return KIND_SCALAR


def _resolve_kind(old: Any, new: Any) -> str:
    """两侧分类一致取该类；类型漂移（str→dict 等）按 scalar 整字段替换。"""
    kind_old, kind_new = classify_kind(old), classify_kind(new)
    return kind_old if kind_old == kind_new else KIND_SCALAR


def _is_system_key(key: str) -> bool:
    """系统字段判定：下划线前缀（如 _meta.schema_rev）不进业务对比。"""
    return key.startswith("_")


# ── 文本类：行级 diff → 双栏片段序列 + 差异摘要 ───────────────────────────────


def text_diff(a: str, b: str) -> dict[str, Any]:
    """文本字段对比：行级 SequenceMatcher → segments（契约见模块 docstring）。

    autojunk=False 防止长文本中高频行被当作垃圾噪声跳过（对比完整性优先）；
    a_only_lines/b_only_lines 为行级差异摘要（增删行计数）。
    """
    old_lines, new_lines = a.splitlines(), b.splitlines()
    segments: list[dict[str, str]] = []
    a_only_lines = 0
    b_only_lines = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append({"side": SIDE_BOTH, "text": "\n".join(old_lines[i1:i2])})
            continue
        if tag in ("delete", "replace"):  # 左栏可见：仅旧 + 替换旧部
            segments.append({"side": SIDE_A_ONLY, "text": "\n".join(old_lines[i1:i2])})
            a_only_lines += i2 - i1
        if tag in ("insert", "replace"):  # 右栏可见：仅新 + 替换新部
            segments.append({"side": SIDE_B_ONLY, "text": "\n".join(new_lines[j1:j2])})
            b_only_lines += j2 - j1
    return {"segments": segments, "a_only_lines": a_only_lines, "b_only_lines": b_only_lines}


# ── 结构类：递归叶子路径 diff ────────────────────────────────────────────────


def tree_diff(a: Any, b: Any, path: str = "$") -> list[dict[str, Any]]:
    """JSON/结构化字段：递归展开叶子路径（$.tags.region / $[2]），叶子级增删改。

    dict 按键并集递归、list 按下标递归（长出的一侧以 None 补齐）；叶子不等即
    记一条 {path, a, b, op}，op: removed=新侧缺失 / added=旧侧缺失 / modified。
    """
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out: list[dict[str, Any]] = []
        for key in sorted(set(a) | set(b)):
            out.extend(tree_diff(a.get(key), b.get(key), f"{path}.{key}"))
        return out
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        out = []
        for index in range(max(len(a), len(b))):
            old = a[index] if index < len(a) else None
            new = b[index] if index < len(b) else None
            out.extend(tree_diff(old, new, f"{path}[{index}]"))
        return out
    if a == b:
        return []
    if a is None and b is not None:
        op = "added"
    elif a is not None and b is None:
        op = "removed"
    else:
        op = "modified"
    return [{"path": path, "a": a, "b": b, "op": op}]


# ── 列表类：集合差（多重集计数，保持原始顺序）───────────────────────────────


def _element_key(element: Any) -> str:
    """列表元素规范化键：canonical JSON（键序无关），dict/标量元素统一可比。"""
    return json.dumps(element, ensure_ascii=False, sort_keys=True, default=str)


def list_diff(a: Sequence[Any], b: Sequence[Any]) -> dict[str, list[Any]]:
    """列表字段集合差：only_in_old / only_in_new（重复元素按多重集计数抵消）。

    保持各侧原始顺序；元素经 canonical JSON 归一比较（dict 元素键序无关）。
    """
    remaining_new = Counter(_element_key(element) for element in b)
    only_in_old: list[Any] = []
    for element in a:
        key = _element_key(element)
        if remaining_new[key] > 0:
            remaining_new[key] -= 1
            continue
        only_in_old.append(element)
    remaining_old = Counter(_element_key(element) for element in a)
    only_in_new: list[Any] = []
    for element in b:
        key = _element_key(element)
        if remaining_old[key] > 0:
            remaining_old[key] -= 1
            continue
        only_in_new.append(element)
    return {"only_in_old": only_in_old, "only_in_new": only_in_new}


# ── 结构化报告 ──────────────────────────────────────────────────────────────


def _detail_of(kind: str, old: Any, new: Any) -> dict[str, Any]:
    """按 kind 计算细化对比产物（UI 渲染数据契约的 kind 专属键）。"""
    if kind == KIND_TEXT:
        return text_diff(str(old), str(new))
    if kind == KIND_TREE:
        return {"leaves": tree_diff(old, new)}
    if kind == KIND_LIST:
        return list_diff(old, new)
    return {}


@dataclass(frozen=True)
class FieldChange:
    """一个变更字段的载体：field/old/new/kind + kind 对应的细化 detail。"""

    field: str
    kind: str
    old: Any
    new: Any
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def change_type(self) -> str:
        """顶层字段两侧均在 → 恒为 modified（added/removed 由 DiffReport 顶层列表表达）。"""
        return "modified"


@dataclass(frozen=True)
class DiffReport:
    """两个快照的结构化对比报告（SR-KB-022 §5 DiffReport 的内核形态）。

    added/removed 为顶层字段名（sorted，确定性输出）；changed 为 FieldChange
    序列（按字段名 sorted）。from_version/to_version 由调用方（service）注入。
    """

    from_version: int | None = None
    to_version: int | None = None
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[FieldChange, ...] = ()

    def is_empty(self) -> bool:
        """两版内容完全一致判定（前端展示「无差异」成功态）。"""
        return not (self.added or self.removed or self.changed)

    def summary(self) -> dict[str, int]:
        """差异摘要计数（SR-KB-022 §5 summary 契约）。"""
        return {
            "changed_fields": len(self.changed) + len(self.added) + len(self.removed),
            "modified": len(self.changed),
            "added": len(self.added),
            "removed": len(self.removed),
        }

    def ui_payload(self) -> dict[str, Any]:
        """双栏对比视图 UI 数据契约（前端按 kind 分派渲染，零算法）。

        fields 元素 = {field, kind, change_type, old, new} 展开 detail 的
        kind 专属键（text→segments/a_only_lines/b_only_lines，tree→leaves，
        list→only_in_old/only_in_new）；added_fields/removed_fields 供字段级
        增删行渲染。
        """
        fields: list[dict[str, Any]] = []
        for change in self.changed:
            entry: dict[str, Any] = {
                "field": change.field,
                "kind": change.kind,
                "change_type": change.change_type,
                "old": change.old,
                "new": change.new,
            }
            entry.update(change.detail)
            fields.append(entry)
        return {
            "from": {"version": self.from_version},
            "to": {"version": self.to_version},
            "summary": self.summary(),
            "added_fields": list(self.added),
            "removed_fields": list(self.removed),
            "fields": fields,
        }


def diff_snapshots(
    snap_a: Mapping[str, Any],
    snap_b: Mapping[str, Any],
    *,
    from_version: int | None = None,
    to_version: int | None = None,
) -> DiffReport:
    """两个快照 dict 的字段级分类对比：顶层字段并集 → 增/删/改三分类。

    仅一侧存在的字段进 added/removed（字段名列表）；两侧均在不等且非系统
    字段的进 changed（按 kind 计算细化 detail）。
    """
    keys_a = {key for key in snap_a if not _is_system_key(key)}
    keys_b = {key for key in snap_b if not _is_system_key(key)}
    added = tuple(sorted(keys_b - keys_a))
    removed = tuple(sorted(keys_a - keys_b))
    changed: list[FieldChange] = []
    for key in sorted(keys_a & keys_b):
        old, new = snap_a[key], snap_b[key]
        if old == new:
            continue
        kind = _resolve_kind(old, new)
        changed.append(
            FieldChange(field=key, kind=kind, old=old, new=new, detail=_detail_of(kind, old, new))
        )
    return DiffReport(
        from_version=from_version,
        to_version=to_version,
        added=added,
        removed=removed,
        changed=tuple(changed),
    )
