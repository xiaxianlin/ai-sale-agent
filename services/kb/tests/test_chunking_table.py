# 知识产权归甲方所有（COM-IP-01）
"""表格整块保护用例（FR-KB-012 / SR-KB-012 §9：test_table_whole / test_table_row_group_split）。

覆盖：表格独立成块不拆分、超保护上限受控行组切分（每块重复表头 + degrade
留痕）、小表格不参与合并也不计孤儿。
"""
from __future__ import annotations

from services.kb.core.pipeline.chunking import ChunkConfig, SemanticChunker
from services.kb.tests._chunking_testlib import FakeDoc, para, table_block

# 参数表：表头 + 5 行数据（含中文单元格，用于整块保护断言）
_PARAM_ROWS = [
    ["参数", "数值", "单位"],
    ["额定承重", "五千", "千克"],
    ["材质", "碳钢", "—"],
    ["表面处理", "镀锌", "—"],
    ["安装孔距", "八十", "毫米"],
    ["包装尺寸", "六十乘四十", "厘米"],
]


def test_table_whole_protection() -> None:
    """14 行参数表场景的最小化版本：表格独立成块不拆分，前后文各自成块。"""
    doc = FakeDoc(blocks=[
        para("前文说明。" + "本节给出该支架的详细规格，供选型参考。"),
        table_block(_PARAM_ROWS),
        para("后文说明。" + "如需定制请直接联系销售团队。"),
    ])
    cfg = ChunkConfig(min_tokens=1, max_tokens=800, table_hard_cap=1600)
    chunks, metrics = SemanticChunker(config=cfg).chunk(doc)
    assert len(chunks) == 3
    table_chunk = chunks[1]
    assert table_chunk.is_table
    assert "table_whole" in table_chunk.boundary_reasons
    # 整表不拆：全部单元格值都在同一块内
    for row in _PARAM_ROWS:
        for cell in row:
            assert cell in table_chunk.content
    assert table_chunk.context_meta["table_summary"] == "表格 6行×3列"
    assert "table_row_group_split" not in table_chunk.boundary_reasons  # 未超限：无受控切分
    # 前后文各自成块，且不混入表格内容（无管道符、无表格单元格值）
    assert "前文说明" in chunks[0].content
    assert "|" not in chunks[0].content and "千克" not in chunks[0].content
    assert "后文说明" in chunks[2].content
    assert "|" not in chunks[2].content
    # 质量自检：表格完整性 + 账目对账
    assert metrics["table_integrity"] is True
    assert metrics["lossless_blocks"] is True
    assert "table_row_group_split" not in metrics["degrade"]


def test_table_row_group_split_with_repeated_header() -> None:
    """token 超保护上限的长表：按行组切分，每块首行重复表头，degrade 留痕。"""
    rows = [["项目", "规格说明"]] + [
        [f"项目{index:02d}", "规格" + "说明" * 4] for index in range(8)
    ]
    doc = FakeDoc(blocks=[table_block(rows)])
    cfg = ChunkConfig(min_tokens=1, max_tokens=800, table_hard_cap=60)
    chunks, metrics = SemanticChunker(config=cfg).chunk(doc)
    table_chunks = [c for c in chunks if c.is_table]
    assert len(table_chunks) >= 2  # 超限必然行组切分
    header_line = "| 项目 | 规格说明 |"
    for chunk in table_chunks:
        # 每块首行重复表头（单块仍可独立检索参数）
        assert chunk.content.splitlines()[0] == header_line
        assert {"table_whole", "table_row_group_split"} <= set(chunk.boundary_reasons)
        assert chunk.tokens <= cfg.table_hard_cap  # 行组贪心聚合保证不超保护上限
    # 全部数据行不丢失：每个项目编号都能在某行组块中找到（账目文本级核对由 lossless 保证）
    for index in range(8):
        assert any(f"项目{index:02d}" in c.content for c in table_chunks)
    # 行区间平铺覆盖 [0, 8)：不重不漏
    spans = sorted(
        (s for c in table_chunks for s in c.block_spans),
        key=lambda s: s["start"],
    )
    assert spans[0]["start"] == 0 and spans[-1]["end"] == 8
    for prev, nxt in zip(spans, spans[1:]):
        assert prev["end"] == nxt["start"]
    # degrade 留痕 + 告警（受控例外绝不静默发生）
    assert "table_row_group_split" in metrics["degrade"]
    assert metrics["table_integrity"] is True
    assert metrics["lossless_blocks"] is True


def test_small_table_never_merges_and_not_orphan() -> None:
    """过短表格块：不参与合并（保持独立成块），也不计孤儿；文本邻块无法合并则标孤儿。"""
    doc = FakeDoc(blocks=[
        para("短前文。"),                       # 文本块，两侧邻居均为表格 → 无法合并
        table_block([["项目", "数值"], ["承重", "五百"]]),
        para("短后文。"),
    ])
    cfg = ChunkConfig(min_tokens=100, max_tokens=800)  # 全部块均低于下限
    chunks, metrics = SemanticChunker(config=cfg).chunk(doc)
    assert len(chunks) == 3
    table_chunk = chunks[1]
    assert table_chunk.is_table
    assert table_chunk.context_meta.get("orphan") is not True  # 表格独立成块是设计行为，非孤儿
    assert "merge" not in table_chunk.boundary_reasons
    # 文本块无法合并（相邻块为表格）→ 保留并标孤儿，交质量报告暴露
    assert chunks[0].context_meta.get("orphan") is True
    assert chunks[2].context_meta.get("orphan") is True
    assert metrics["orphan_rate"] == 1.0  # 两个文本块均为孤儿（表格块不计入分母）
    assert metrics["table_integrity"] is True
    assert metrics["lossless_blocks"] is True
