# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""语义分块（FR-KB-012 / SR-KB-012）TDD 补齐测试（五类场景补强，不改既有文件）。

按 TDD 新规五列审计既有 test_chunking_* 后补齐：
- 异常：blocks 不可迭代 / 缺少 blocks 属性 → CHUNK-1001（既有仅覆盖空列表分支）；
- 边界：句聚合恰满 max_tokens（等号入块）与差 1（切分）双端点；
- 空集/单元素：仅空文本 block 的账目覆盖、单短块 orphan 标记；
- 缺陷钉子（xfail strict）：_degrade 列表未随 chunk() 调用重置——长生命周期
  分块器（如增量同步 make_text_chunk_fn 复用单实例）上一个文档的
  table_row_group_split 降级会泄漏进后续无表格文档的质量指标（真实缺陷，
  测试固化，修复后此钉子将 XPASS 提醒摘除）。

KPI 映射：SRS §16.1「检索准确率」——分块质量是召回的地基；账目不重不漏
与降级留痕的准确性直接决定坏块能否被质量报告暴露。

依据文档：docs/07-实现方案/KB/SR-KB-012-语义分块.md §6.1、§6.3、§6.5、§9。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.kb.core.pipeline.chunking.chunker import (
    ChunkConfig,
    ChunkInputError,
    SemanticChunker,
    default_token_counter,
)


class _Doc:
    """UnstructuredDocLike 最小投影（鸭子类型契约）。"""

    def __init__(self, blocks: list) -> None:
        self.blocks = blocks
        self.source_file = "测试.docx"


class _Block:
    def __init__(self, type_: str, text: str, heading_level: int = 1, table=None) -> None:
        self.type = type_
        self.text = text
        self.heading_level = heading_level
        self.table = table


class TestInputValidation:
    """异常：输入契约 CHUNK-1001 的另两个分支（不可迭代 / 缺属性）。"""

    def test_blocks_not_iterable_raises(self) -> None:
        """blocks 为非可迭代标量：list() 抛 TypeError → 统一转 ChunkInputError。"""
        with pytest.raises(ChunkInputError) as exc_info:
            SemanticChunker().chunk(_Doc(blocks=123))  # type: ignore[arg-type]
        assert "CHUNK-1001" in str(exc_info.value)

    def test_missing_blocks_attribute_raises(self) -> None:
        """缺少 blocks 属性（上游契约破坏）：ChunkInputError 且指引契约文档。"""
        bad = SimpleNamespace(source_file="x.docx")
        with pytest.raises(ChunkInputError) as exc_info:
            SemanticChunker().chunk(bad)  # type: ignore[arg-type]
        assert "CHUNK-1001" in str(exc_info.value)
        assert "blocks" in str(exc_info.value)


class TestTokenBoundary:
    """边界：句聚合恰满上限（等号仍入同块）与差 1（触发长度兜底切分）。"""

    @staticmethod
    def _doc_two_sentences() -> _Doc:
        # 两句：按字符计 token 分别为 11 与 6（含句号），合计 17
        return _Doc([_Block("text", "abcdefghij。klmno。")])

    def test_exact_max_tokens_stays_in_one_chunk(self) -> None:
        """边界内恰满：buffer.tokens + incoming == max_tokens → 不切分（<= 语义）。"""
        config = ChunkConfig(
            min_tokens=1, max_tokens=17, token_counter=lambda s: len(s)
        )
        chunks, metrics = SemanticChunker(config).chunk(self._doc_two_sentences())
        assert len(chunks) == 1
        assert chunks[0].tokens == 17
        assert "klmno。" in chunks[0].content

    def test_one_over_max_tokens_splits_with_hard_limit(self) -> None:
        """边界外差 1：17 > 16 → 长度兜底切分，第二块切点依据 hard_limit。"""
        config = ChunkConfig(
            min_tokens=1, max_tokens=16, token_counter=lambda s: len(s)
        )
        chunks, _metrics = SemanticChunker(config).chunk(self._doc_two_sentences())
        assert len(chunks) == 2
        assert chunks[0].tokens == 11 and chunks[1].tokens == 6
        assert "hard_limit" in chunks[1].boundary_reasons


class TestDegenerateDocs:
    """空集/单元素：仅空文本块、单短块。"""

    def test_only_empty_blocks_produce_account_chunks(self) -> None:
        """仅空文本 block：正文为空但账目覆盖（block_spans 不重不漏，quality 对账基础）。"""
        doc = _Doc([_Block("text", "   "), _Block("text", "")])
        chunks, metrics = SemanticChunker().chunk(doc)
        assert metrics["blocks"] == 2
        covered = {span["idx"] for chunk in chunks for span in chunk.block_spans}
        assert covered == {0, 1}  # 两个空块均被账目覆盖
        assert all(chunk.content == "" for chunk in chunks)

    def test_single_small_block_marked_orphan(self) -> None:
        """单元素：唯一短块无法合并 → 保留并显式标 orphan（质量报告暴露口径）。"""
        doc = _Doc([_Block("text", "短句一句话。")])
        chunks, _metrics = SemanticChunker().chunk(doc)
        assert len(chunks) == 1
        assert chunks[0].context_meta.get("orphan") is True

    def test_default_token_counter_mixed_script(self) -> None:
        """token 计量口径（确定性纯函数，幂等重放基础）：汉字 1/字、其余 4 字符折 1。"""
        assert default_token_counter("产品参数") == 4
        assert default_token_counter("abcdefgh") == 2  # 8 个非空白字符 → 2 token
        assert default_token_counter("产品 abcd") == 3  # 2 汉字 + 4 拉丁折 1... 2 + 1 = 3
        assert default_token_counter("  \n\t ") == 0  # 纯空白计 0


class TestDegradeStateLeakDefect:
    """降级留痕不跨 chunk() 调用泄漏（2026-10-10 红→绿修复后转正）。"""

    def test_degrade_does_not_leak_across_chunk_calls(self) -> None:
        """同实例先分含大表文档（触发行组切分降级），再分纯文本文档 → degrade 应为空。"""
        chunker = SemanticChunker(ChunkConfig(table_hard_cap=200))
        big_table = [["参数", "说明很长的内容" * 20]] + [[f"参数{i}", "值" * 50] for i in range(40)]
        _chunks1, metrics1 = chunker.chunk(_Doc([_Block("table", "", table=big_table)]))
        assert metrics1["degrade"] == ["table_row_group_split"]  # 首文档降级成立
        _chunks2, metrics2 = chunker.chunk(
            _Doc([_Block("heading", "标题"), _Block("text", "普通段落内容一。普通段落内容二。")])
        )
        # 修复后：第二文档无表格、无降级 → degrade == []（当前实现泄漏首文档的降级token）
        assert metrics2["degrade"] == []
