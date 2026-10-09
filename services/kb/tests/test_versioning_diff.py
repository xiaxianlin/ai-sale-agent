# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
"""字段分类对比引擎单元测试（FR-KB-022 §9：文本/dict/列表三类正确性）。

覆盖（SR-KB-022 §9 测试要点 diff 部分）：
- text：行级双栏片段红绿标注正确，等值上下文两侧共显，附行级差异摘要；
- tree（dict/JSON）：递归叶子路径 op 正确（$.x.y / $[n]，added/removed/modified）；
- list：集合差（多重集计数，dict 元素键序无关，保持原始顺序）；
- 快照级 diff_snapshots：顶层字段增/删/改三分类 + summary 计数 + UI 契约；
- kind 分类与类型漂移（str→dict 按 scalar 整字段替换）；系统字段（_ 前缀）不进对比。

实现方案：docs/07-实现方案/KB/SR-KB-022-版本控制.md §5、§6.2、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from typing import Any

from services.kb.core.versioning import (
    KIND_LIST,
    KIND_SCALAR,
    KIND_TEXT,
    KIND_TREE,
    classify_kind,
    diff_snapshots,
    list_diff,
    text_diff,
    tree_diff,
)


# ── kind 分类 ───────────────────────────────────────────────────────────────


def test_classify_kind_by_value_type() -> None:
    """值类型 → kind：str=text、dict=tree、list=list、标量=scalar。"""
    assert classify_kind("文本") == KIND_TEXT
    assert classify_kind({"k": 1}) == KIND_TREE
    assert classify_kind(["a", "b"]) == KIND_LIST
    assert classify_kind(100) == KIND_SCALAR
    assert classify_kind(True) == KIND_SCALAR
    assert classify_kind(None) == KIND_SCALAR


# ── 文本类：行级双栏片段 ────────────────────────────────────────────────────


def test_text_diff_segments_sides_and_context() -> None:
    """多行文本：等值上下文 both 两侧共显，被改行拆 a_only/b_only 相邻成对。"""
    a = "产品概述\n技术参数\n交付说明"
    b = "产品概述\n技术参数（含认证）\n交付说明"
    out = text_diff(a, b)
    segments = out["segments"]
    assert [seg["side"] for seg in segments] == ["both", "a_only", "b_only", "both"]
    assert segments[0]["text"] == "产品概述"      # 等值上下文
    assert segments[1]["text"] == "技术参数"      # 仅旧（红）
    assert segments[2]["text"] == "技术参数（含认证）"  # 仅新（绿）
    assert segments[3]["text"] == "交付说明"
    assert out["a_only_lines"] == 1 and out["b_only_lines"] == 1  # 行级差异摘要


def test_text_diff_insert_and_delete_blocks() -> None:
    """整块插入/删除：红绿块各成段，摘要行数与增删行数一致。"""
    a = "第一段\n第二段"
    b = "第一段\n插入段一\n插入段二\n第二段\n追加段"
    out = text_diff(a, b)
    sides = [seg["side"] for seg in out["segments"]]
    assert sides.count("b_only") == 2  # 插入块 + 追加块
    assert sides.count("a_only") == 0
    assert out["b_only_lines"] == 3 and out["a_only_lines"] == 0

    reverse = text_diff(b, a)
    assert [seg["side"] for seg in reverse["segments"]].count("a_only") == 2
    assert reverse["a_only_lines"] == 3


def test_text_diff_identical_all_both() -> None:
    """完全一致：仅 both 段，差异摘要为零。"""
    out = text_diff("同一行\n又一行", "同一行\n又一行")
    assert {seg["side"] for seg in out["segments"]} == {"both"}
    assert out["a_only_lines"] == 0 and out["b_only_lines"] == 0


# ── 结构类：递归叶子路径 ────────────────────────────────────────────────────


def test_tree_diff_nested_leaf_paths() -> None:
    """嵌套 dict/数组：叶子级增删改，路径形如 $.cert.level / $.tags[1]。"""
    a = {"region": "华东", "tags": ["询价", "议价"], "cert": {"iso": "9001", "level": 2}}
    b = {"region": "华南", "tags": ["询价", "售后"], "cert": {"iso": "9001", "level": 3}}
    leaves = {(leaf["path"]): leaf for leaf in tree_diff(a, b)}
    assert set(leaves) == {"$.region", "$.tags[1]", "$.cert.level"}
    assert leaves["$.region"] == {"path": "$.region", "a": "华东", "b": "华南", "op": "modified"}
    assert leaves["$.tags[1]"] == {"path": "$.tags[1]", "a": "议价", "b": "售后", "op": "modified"}
    assert leaves["$.cert.level"]["op"] == "modified"


def test_tree_diff_added_removed_leaves() -> None:
    """叶子增删：一侧缺失的键/长出的数组元素按 added/removed 归类。"""
    added = tree_diff({"x": {"y": 1}}, {"x": {"y": 1, "z": 2}})
    assert added == [{"path": "$.x.z", "a": None, "b": 2, "op": "added"}]

    removed = tree_diff({"x": {"y": 1}}, {"x": {}})
    assert removed == [{"path": "$.x.y", "a": 1, "b": None, "op": "removed"}]

    grown = tree_diff({"arr": [1]}, {"arr": [1, 2]})
    assert grown == [{"path": "$.arr[1]", "a": None, "b": 2, "op": "added"}]


def test_tree_diff_equal_returns_empty() -> None:
    """等值（含嵌套数组逐元素相等）无差异。"""
    assert tree_diff({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []


# ── 列表类：集合差 ──────────────────────────────────────────────────────────


def test_list_diff_set_semantics_with_counts() -> None:
    """集合差：重复元素按多重集计数抵消，各自保持原始顺序。"""
    a = ["询价", "议价", "售后", "询价"]
    b = ["询价", "售后", "培训"]
    out = list_diff(a, b)
    assert out["only_in_old"] == ["议价", "询价"]  # 询价×2 抵消×1 余 1
    assert out["only_in_new"] == ["培训"]


def test_list_diff_dict_elements_key_order_agnostic() -> None:
    """dict 元素按 canonical JSON 比较：键序无关，内容不同即两侧差集。"""
    a = [{"sku": "A", "moq": 100}, {"sku": "B"}]
    b = [{"moq": 200, "sku": "A"}, {"sku": "C"}]  # A 行内容变化（键序不同）
    out = list_diff(a, b)
    assert out["only_in_old"] == [{"sku": "A", "moq": 100}, {"sku": "B"}]
    assert out["only_in_new"] == [{"moq": 200, "sku": "A"}, {"sku": "C"}]


def test_list_diff_identical_empty() -> None:
    """完全一致：双侧差集为空。"""
    out = list_diff([1, {"k": "v"}], [{"k": "v"}, 1])
    assert out == {"only_in_old": [], "only_in_new": []}


# ── 快照级：三分类 + summary + UI 契约 ─────────────────────────────────────


def _snap_a() -> dict[str, Any]:
    return {
        "kb_type": "product",
        "title": "ST-3200 支架",
        "content": "# 概述\n工业支架。",
        "metadata": {"sku": "ST-3200", "tags": ["询价", "报价"]},
        "status": "draft",
        "moq": 100,
        "legacy_note": "旧字段",
        "_meta": {"schema_rev": 1},
    }


def _snap_b() -> dict[str, Any]:
    return {
        "kb_type": "product",
        "title": "ST-3200 支架（含认证）",
        "content": "# 概述\n工业支架（已通过 ISO 认证）。",
        "metadata": {"sku": "ST-3200", "tags": ["询价", "报价", "售后"]},
        "status": "published",
        "moq": 200,
        "_meta": {"schema_rev": 2},
    }


def test_diff_snapshots_three_way_classification() -> None:
    """顶层字段三分类：added/removed 字段名列表 + changed 携带分类 kind/detail。"""
    report = diff_snapshots(_snap_a(), _snap_b(), from_version=1, to_version=2)
    assert report.added == () and report.removed == ("legacy_note",)
    kinds = {change.field: change.kind for change in report.changed}
    assert kinds == {
        "title": KIND_TEXT,
        "content": KIND_TEXT,
        "metadata": KIND_TREE,
        "status": KIND_TEXT,   # str → text
        "moq": KIND_SCALAR,    # 数值 → scalar
    }
    assert not report.is_empty()
    assert report.summary() == {
        "changed_fields": 5 + 1,  # 5 改 + 1 删（legacy_note）
        "modified": 5,
        "added": 0,
        "removed": 1,
    }


def test_diff_snapshots_kind_details() -> None:
    """changed 各自携带分类细化产物：文本段/叶子路径/标量无细化。"""
    report = diff_snapshots(_snap_a(), _snap_b())
    by_field = {change.field: change for change in report.changed}

    title = by_field["title"]
    assert title.old == "ST-3200 支架" and title.new == "ST-3200 支架（含认证）"
    assert {seg["side"] for seg in title.detail["segments"]} >= {"a_only", "b_only"}
    assert title.change_type == "modified"

    metadata = by_field["metadata"]
    assert metadata.detail["leaves"] == [
        {"path": "$.tags[2]", "a": None, "b": "售后", "op": "added"}
    ]

    moq = by_field["moq"]
    assert moq.detail == {}  # scalar 无细化，old/new 直出


def test_diff_snapshots_type_drift_falls_back_to_scalar() -> None:
    """两侧类型漂移（str→dict）：按 scalar 整字段替换展示（防渲染歧义）。"""
    report = diff_snapshots({"spec": "简述"}, {"spec": {"rows": [1]}})
    assert report.changed[0].kind == KIND_SCALAR
    assert report.changed[0].detail == {}


def test_diff_snapshots_identical_empty_report() -> None:
    """两版一致：空报告 + 零计数（前端「内容完全一致」成功态）。"""
    report = diff_snapshots(_snap_a(), _snap_a(), from_version=3, to_version=3)
    assert report.is_empty()
    assert report.summary() == {"changed_fields": 0, "modified": 0, "added": 0, "removed": 0}
    assert report.ui_payload()["fields"] == []


def test_ui_payload_contract_shape() -> None:
    """UI 数据契约（SR-KB-022 §5）：from/to/summary/added_fields/removed_fields/fields，
    fields 元素展开 kind 专属键（text→segments、tree→leaves）。"""
    report = diff_snapshots(_snap_a(), _snap_b(), from_version=1, to_version=2)
    payload = report.ui_payload()
    assert payload["from"] == {"version": 1} and payload["to"] == {"version": 2}
    assert payload["summary"]["modified"] == 5
    assert payload["removed_fields"] == ["legacy_note"]
    fields = {entry["field"]: entry for entry in payload["fields"]}
    assert "segments" in fields["title"] and "leaves" in fields["metadata"]
    assert fields["moq"]["old"] == 100 and fields["moq"]["new"] == 200
    for entry in payload["fields"]:
        assert entry["change_type"] == "modified"
