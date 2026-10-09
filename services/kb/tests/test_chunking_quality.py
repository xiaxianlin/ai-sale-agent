# 知识产权归甲方所有（COM-IP-01）
"""账目对账与质量自检用例（FR-KB-012 / SR-KB-012 §9：test_lossless_reconcile /
test_merge_oversmall / test_idempotent 指针部分）。

覆盖：块文本拼接≈原文的对账自检、缺失/重复/文本篡改的检出、过短块合并与
相邻指针回填、空文本 block 覆盖、指标字典完整性、非法输入（CHUNK-1001）。
"""
from __future__ import annotations

import copy

import pytest

from services.kb.core.pipeline.chunking import (
    ChunkConfig,
    ChunkInputError,
    SemanticChunker,
    reconcile,
    reconcile_blocks,
    reconcile_text,
)
from services.kb.tests._chunking_testlib import (
    FakeDoc,
    cn_sentence,
    heading,
    para,
    table_block,
)


def _rich_doc() -> FakeDoc:
    """标题 + 段落 + 表格 + 段落的多形态文档（对账需覆盖全部形态）。"""
    return FakeDoc(blocks=[
        heading("产品手册", 1),
        heading("技术参数", 2),
        para("本节列出关键技术参数。" + cn_sentence(0)),
        table_block([["项目", "数值"], ["承重", "五百"], ["材质", "碳钢"]]),
        para("更多信息请联系销售。" + cn_sentence(1)),
    ])


def test_lossless_reconcile_on_rich_doc() -> None:
    """多形态文档：block 账目不重不漏 + 块文本按序拼接≈原文。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    chunks, metrics = SemanticChunker(config=cfg).chunk(_rich_doc())
    assert metrics["lossless_blocks"] is True
    assert metrics["block_ledger_ok"] is True
    assert metrics["text_ledger_ok"] is True
    # 手工复核：block 覆盖恰为全部 block 序号
    report = reconcile_blocks(chunks, 5)
    assert report["missing_blocks"] == [] and report["duplicated_blocks"] == []
    # 文本账目：拼接长度与原文重建一致
    text_report = reconcile_text(_rich_doc(), chunks)
    assert text_report["ok"] and text_report["expected_chars"] == text_report["actual_chars"]


def test_reconcile_detects_missing_block() -> None:
    """抽掉一个块 → block 账目出现缺失，对账失败（CHUNK-2001 阻断依据）。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    chunks, _ = SemanticChunker(config=cfg).chunk(_rich_doc())
    tampered = copy.deepcopy(chunks[:-1])  # 丢弃最后一个块
    report = reconcile(_rich_doc(), tampered)
    assert report["ok"] is False
    assert report["blocks"]["ok"] is False
    assert len(report["blocks"]["missing_blocks"]) >= 1


def test_reconcile_detects_duplicated_span() -> None:
    """同一 block 被两个块重复覆盖（行区间交叠）→ 账目对账失败。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    chunks, _ = SemanticChunker(config=cfg).chunk(_rich_doc())
    tampered = copy.deepcopy(chunks)
    tampered[-1].block_spans.append(dict(tampered[0].block_spans[0]))  # 重复计入首块覆盖
    report = reconcile(_rich_doc(), tampered)
    assert report["ok"] is False
    assert tampered[0].block_spans[0]["idx"] in report["blocks"]["duplicated_blocks"]


def test_reconcile_detects_text_tamper() -> None:
    """块内容被篡改（关键信息凭空增删）→ 文本账目失守，对账失败。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    chunks, _ = SemanticChunker(config=cfg).chunk(_rich_doc())
    tampered = copy.deepcopy(chunks)
    tampered[0].content += "被篡改后凭空新增的关键信息"
    report = reconcile(_rich_doc(), tampered)
    assert report["ok"] is False
    assert report["text"]["ok"] is False
    assert report["text"]["actual_chars"] > report["text"]["expected_chars"]


def test_merge_oversmall_with_pointer_backfill() -> None:
    """80 token 孤段场景最小化：同标题过短块合并为一块，账目合并、指针回填。"""
    doc = FakeDoc(blocks=[
        heading("规格", 1),
        para(cn_sentence(0) + cn_sentence(1)),   # 约 26 token（< 下限）
        heading("规格", 1),                        # 同名同级标题 → heading_path 相同
        para(cn_sentence(2) + cn_sentence(3)),
    ])
    cfg = ChunkConfig(min_tokens=40, max_tokens=800)  # 两个短块各约 28 token，合并后约 56 ≥ 下限
    chunks, metrics = SemanticChunker(config=cfg).chunk(doc)
    assert len(chunks) == 1  # 两个同路径短块合并
    merged = chunks[0]
    assert "merge" in merged.boundary_reasons
    assert cn_sentence(0) in merged.content and cn_sentence(3) in merged.content
    # 合并块覆盖全部 4 个上游 block（账目不重不漏）
    covered = sorted(s["idx"] for s in merged.block_spans)
    assert covered == [0, 1, 2, 3]
    # 单块：首尾指针均为空
    assert merged.context_meta["prev_chunk_uid"] is None
    assert merged.context_meta["next_chunk_uid"] is None
    assert metrics["lossless_blocks"] is True
    assert metrics["orphan_rate"] == 0.0


def test_neighbor_pointers_and_uid_backfill() -> None:
    """多块定稿后统一回填：prev/next 指针取相邻 chunk_index 的 uid，端点为 None。"""
    doc = FakeDoc(blocks=[
        heading("总览", 1),
        heading("第一节", 2),
        para(cn_sentence(0) + cn_sentence(1)),
        heading("第二节", 2),
        para(cn_sentence(2) + cn_sentence(3)),
        heading("第三节", 2),
        para(cn_sentence(4) + cn_sentence(5)),
    ])
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    chunks, _ = SemanticChunker(config=cfg).chunk(doc)
    assert len(chunks) == 3
    for index, chunk in enumerate(chunks):
        assert chunk.chunk_index == index                      # 输出顺序统一赋值
        assert chunk.uid.startswith("c-") and len(chunk.uid) == 10  # c-xxxxxxxx
        expect_prev = chunks[index - 1].uid if index > 0 else None
        expect_next = chunks[index + 1].uid if index + 1 < len(chunks) else None
        assert chunk.context_meta["prev_chunk_uid"] == expect_prev
        assert chunk.context_meta["next_chunk_uid"] == expect_next
    # 父标题与结构化 headings 回填
    assert chunks[1].context_meta["headings"] == ["总览", "第二节"]
    assert chunks[1].context_meta["parent_heading"] == "总览"
    # char_span 连续平铺
    spans = [c.context_meta["char_span"] for c in chunks]
    assert spans[0][0] == 0 and spans[-1][1] == sum(len(c.content) for c in chunks)
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert end == start


def test_empty_text_block_still_covered() -> None:
    """空文本 block：不产句但账目仍覆盖（对账不漏）。"""
    doc = FakeDoc(blocks=[
        para("开头内容。" + cn_sentence(0)),
        para(""),  # 清洗后残留的空段
        para("结尾内容。" + cn_sentence(1)),
    ])
    chunks, metrics = SemanticChunker(config=ChunkConfig(min_tokens=1, max_tokens=800)).chunk(doc)
    assert len(chunks) == 1
    covered = sorted(s["idx"] for s in chunks[0].block_spans)
    assert covered == [0, 1, 2]
    assert metrics["lossless_blocks"] is True


def test_quality_metrics_dict_shape() -> None:
    """指标字典完整性：六项指标 + 工程告警扩展（供流水报告 FR-KB-024 消费）。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    _, metrics = SemanticChunker(config=cfg).chunk(_rich_doc())
    for key in ("lossless_blocks", "table_integrity", "token_dist", "boundary_mix",
                "orphan_rate", "heading_path_coverage", "blocks", "chunks", "degrade"):
        assert key in metrics
    for key in ("min", "p50", "p95", "max", "out_of_range_rate"):
        assert key in metrics["token_dist"]
    assert metrics["heading_path_coverage"] == 1.0
    assert metrics["hard_limit_alert"] is False  # 纯标题边界文档无兜底切点
    assert metrics["out_of_range_alert"] is False
    assert metrics["boundary_mix"].get("heading", 0) >= 1


def test_empty_blocks_raises_chunk_1001() -> None:
    """非法输入：blocks 为空 / 缺失 → CHUNK-1001（禁止裸吞，统一错误码）。"""
    with pytest.raises(ChunkInputError, match="CHUNK-1001"):
        SemanticChunker().chunk(FakeDoc(blocks=[]))
    with pytest.raises(ChunkInputError, match="CHUNK-1001"):
        SemanticChunker().chunk(object())  # 缺少 blocks 属性


def test_deterministic_replay_same_uids() -> None:
    """幂等重放（FR-KB-017）：同一文档重复分块产生完全一致的 uid 与索引。"""
    doc = _rich_doc()
    cfg = ChunkConfig(min_tokens=1, max_tokens=800)
    first, first_metrics = SemanticChunker(config=cfg).chunk(doc)
    second, second_metrics = SemanticChunker(config=cfg).chunk(doc)
    assert [c.uid for c in first] == [c.uid for c in second]
    assert [c.content for c in first] == [c.content for c in second]
    assert [c.chunk_index for c in first] == [c.chunk_index for c in second]
    assert first_metrics["lossless_blocks"] == second_metrics["lossless_blocks"]
