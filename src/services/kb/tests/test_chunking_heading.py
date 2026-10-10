# 知识产权归甲方所有（COM-IP-01）
"""标题层级边界用例（FR-KB-012 / SR-KB-012 §9：test_heading_boundary / test_heading_stack_pop）。

覆盖：H2 前切块与 heading_path 构造、标题栈弹出回退、尾部孤悬标题归属、
无标题文档空路径、标题后紧跟表格的归属。
"""
from __future__ import annotations

from services.kb.core.pipeline.chunking import ChunkConfig, SemanticChunker
from services.kb.tests._chunking_testlib import (
    FakeDoc,
    cn_sentence,
    heading,
    para,
    table_block,
)


def _chunk(doc) -> tuple[list, dict]:
    """小阈值配置：避免过短合并干扰标题边界断言。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    return SemanticChunker(config=cfg).chunk(doc)


def test_heading_boundary_splits_and_builds_paths() -> None:
    """「H1>H2>正文>H2>正文」：每个 H2 前切块，heading_path 为两级路径。"""
    doc = FakeDoc(blocks=[
        heading("产品目录", 1),
        heading("支架系列", 2),
        para("第一段正文内容：" + cn_sentence(0)),
        heading("安装说明", 2),
        para("第二段正文内容：" + cn_sentence(1)),
    ])
    chunks, metrics = _chunk(doc)
    assert len(chunks) == 2
    # 第一个块：两级标题路径 + 两级标题只归属一次
    assert chunks[0].heading_path == "产品目录/支架系列"
    assert "产品目录" in chunks[0].content and "支架系列" in chunks[0].content
    assert chunks[0].context_meta["headings"] == ["产品目录", "支架系列"]
    assert chunks[0].context_meta["parent_heading"] == "产品目录"
    # 第二个块：同级新标题 → 新路径，旧标题不残留
    assert chunks[1].heading_path == "产品目录/安装说明"
    assert "支架系列" not in chunks[1].content
    assert "heading" in chunks[1].boundary_reasons
    # 账目对账
    assert metrics["lossless_blocks"] is True
    assert metrics["heading_path_coverage"] == 1.0


def test_heading_stack_pop_on_same_level() -> None:
    """H1>H2>H3 后接 H2：栈弹出 H3，路径回退为 H1/H2（不残留 H3）。"""
    doc = FakeDoc(blocks=[
        heading("总纲", 1),
        heading("规格", 2),
        heading("细节", 3),
        para(cn_sentence(0) + cn_sentence(1)),
        heading("包装", 2),
        para(cn_sentence(2) + cn_sentence(3)),
    ])
    chunks, _ = _chunk(doc)
    assert len(chunks) == 2
    assert chunks[0].heading_path == "总纲/规格/细节"
    assert chunks[1].heading_path == "总纲/包装"
    assert "细节" not in chunks[1].heading_path  # 弹栈后不残留


def test_bare_trailing_heading_attributed() -> None:
    """文档以标题结尾：孤悬标题独立成块（仍被账目覆盖），不丢 block。"""
    doc = FakeDoc(blocks=[
        heading("目录", 1),
        para("正文内容：" + cn_sentence(0)),
        heading("附录", 2),
    ])
    chunks, metrics = _chunk(doc)
    assert len(chunks) == 2
    assert "附录" in chunks[-1].content
    assert "heading" in chunks[-1].boundary_reasons
    assert metrics["lossless_blocks"] is True


def test_no_heading_doc_yields_empty_path() -> None:
    """无标题文档（纯文本/Excel 单表场景）：heading_path 为空串，headings=[]。"""
    doc = FakeDoc(blocks=[para("第一段。" + cn_sentence(0)), para("第二段。" + cn_sentence(1))])
    chunks, metrics = _chunk(doc)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ""
    assert chunks[0].context_meta["headings"] == []
    assert chunks[0].context_meta["parent_heading"] is None
    assert metrics["heading_path_coverage"] == 1.0
    assert metrics["lossless_blocks"] is True


def test_heading_before_table_attaches_to_table_chunk() -> None:
    """标题后紧跟表格：标题文本归属表格块（其后首个块），路径含该标题。"""
    doc = FakeDoc(blocks=[
        heading("技术参数", 2),
        table_block([["项目", "数值"], ["承重", "500kg"]]),
    ])
    chunks, metrics = _chunk(doc)
    assert len(chunks) == 1
    assert chunks[0].is_table
    assert chunks[0].content.startswith("技术参数")
    assert chunks[0].heading_path == "技术参数"
    assert metrics["table_integrity"] is True
    assert metrics["lossless_blocks"] is True
