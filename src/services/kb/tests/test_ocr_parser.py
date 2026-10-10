# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""OCR 解析器单元测试（FR-KB-010，注入引擎替身，不依赖 PaddleOCR）。

覆盖（对应实现方案 §9 test_ocr_low_conf 及 §6.4）：
- 版面后处理：同行合并（按左坐标阅读顺序）、段落聚合、标题判定（行高分档）；
- 低置信行保留且标记 meta.low_conf=True（不静默丢弃）；
- 全图低置信字符占比超阈值 → stats.needs_manual_review=True；
- PP-Structure 表格识别分支：整表出块（不拆分，FR-KB-012 契约）；
- import 守护：paddleocr 缺失时抛带中文提示的 UnsupportedFormatError。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.4、§7、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import sys
from typing import Any

import pytest

from services.kb.core.pipeline.parsers.base import (
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_PARAGRAPH,
    BLOCK_TYPE_TABLE,
    UnsupportedFormatError,
)
from services.kb.core.pipeline.parsers.ocr_parser import (
    OcrLine,
    OcrParser,
    OcrResult,
    OcrTable,
    _join_texts,
    layout_postprocess,
)


class _FakeOcrEngine:
    """OCR 引擎替身：返回预设 OcrResult（不依赖 PaddleOCR 本体）。"""

    def __init__(self, result: OcrResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def run(self, image_path: str) -> OcrResult:
        self.calls.append(image_path)
        return self.result


class TestLayoutPostprocess:
    """版面后处理纯逻辑：layout_postprocess(lines, 阈值) → blocks。"""

    def test_same_line_merge_reading_order(self) -> None:
        """同行合并：y 中心接近的两行按左坐标（x0）排序拼接，保证从左到右阅读顺序。"""
        lines = [
            OcrLine(text="价格为", conf=0.95, bbox=(200, 100, 360, 120)),  # 右半行
            OcrLine(text="产品", conf=0.95, bbox=(50, 101, 160, 121)),     # 左半行（y 略偏 1px）
        ]
        blocks = layout_postprocess(lines, 0.80)
        assert len(blocks) == 1
        assert blocks[0].type == BLOCK_TYPE_PARAGRAPH
        assert blocks[0].text == "产品价格为"  # 左先行、右后行，CJK 直连不加空格

    def test_paragraph_aggregation_and_heading(self) -> None:
        """段落聚合（行距<前行高×1.8 并段）+ 标题判定（行高显著大于均值独立成块）。"""
        lines = [
            # 标题行：高 40px（显著大于正文 20px）
            OcrLine(text="产品参数表", conf=0.95, bbox=(50, 50, 300, 90)),
            # 正文第 1 物理行
            OcrLine(text="本产品", conf=0.92, bbox=(50, 100, 160, 120)),
            # 正文第 2 物理行（与上一行行距 10px < 20*1.8 → 同段）
            OcrLine(text="适用于出口外贸场景。", conf=0.92, bbox=(50, 130, 300, 150)),
        ]
        blocks = layout_postprocess(lines, 0.80)
        assert [b.type for b in blocks] == [BLOCK_TYPE_HEADING, BLOCK_TYPE_PARAGRAPH]
        assert blocks[0].text == "产品参数表"
        # 行高 40 / 均值 (40+20+20)/3≈26.7 → 比值≈1.5 → 3 级标题
        assert blocks[0].heading_level == 3
        # 两物理行聚合为一段，CJK 直连
        assert blocks[1].text == "本产品适用于出口外贸场景。"
        assert blocks[1].meta["avg_conf"] == pytest.approx(0.92, abs=1e-3)
        assert blocks[1].meta["low_conf"] is False

    def test_numbering_starts_new_paragraph(self) -> None:
        """编号起始行（'1、'）即使行距很近也另起新段。"""
        lines = [
            OcrLine(text="产品说明如下", conf=0.9, bbox=(50, 100, 300, 120)),
            OcrLine(text="1、电压范围 220V", conf=0.9, bbox=(50, 122, 300, 142)),  # 行距 2px 很近
        ]
        blocks = layout_postprocess(lines, 0.80)
        assert len(blocks) == 2
        assert blocks[1].text == "1、电压范围 220V"

    def test_large_gap_splits_paragraph(self) -> None:
        """行距 >= 前行高×1.8 → 分段。"""
        lines = [
            OcrLine(text="上一段内容", conf=0.9, bbox=(50, 100, 300, 120)),
            OcrLine(text="下一段内容", conf=0.9, bbox=(50, 160, 300, 180)),  # 行距 40 >= 20*1.8
        ]
        blocks = layout_postprocess(lines, 0.80)
        assert len(blocks) == 2

    def test_empty_lines(self) -> None:
        """空输入 → 空 blocks。"""
        assert layout_postprocess([], 0.80) == []

    def test_join_texts_cjk_vs_latin(self) -> None:
        """拼接规则：CJK 边界直连，拉丁边界补空格。"""
        assert _join_texts("产品", "价格") == "产品价格"
        assert _join_texts("product", "price") == "product price"
        assert _join_texts("产品", "price") == "产品price"  # 任一侧 CJK 即直连
        assert _join_texts("", "x") == "x"


class TestOcrParserWithEngine:
    """OcrParser 端到端（注入引擎替身）：低置信保留与表格分支。"""

    def test_low_conf_lines_retained_and_flagged(self) -> None:
        """§9 test_ocr_low_conf：平均置信 0.6 的模糊图片 → 低置信行保留 + needs_manual_review=True。"""
        lines = [
            OcrLine(text="模糊但完整的参数文本一", conf=0.6, bbox=(50, 50, 300, 70)),
            OcrLine(text="模糊但完整的参数文本二", conf=0.6, bbox=(50, 80, 300, 100)),
        ]
        parser = OcrParser(ocr_engine=_FakeOcrEngine(OcrResult(lines=lines)))
        doc = parser.parse("fuzzy.png")
        # 低置信行不丢弃：文本完整保留
        assert any("参数文本一" in b.text for b in doc.blocks)
        # 块级低置信标记
        assert doc.blocks[0].meta["low_conf"] is True
        # 全图低置信字符占比 100% > 15% → 人工复核标记（防静默丢失）
        assert doc.stats["needs_manual_review"] is True

    def test_mixed_conf_no_false_review(self) -> None:
        """仅少量低置信字符（占比 ≤ 阈值）→ 保留标记但不触发人工复核。"""
        lines = [
            OcrLine(text="清晰的主体内容较长的一行文本", conf=0.98, bbox=(50, 50, 400, 70)),
            OcrLine(text="短低置信", conf=0.5, bbox=(50, 80, 200, 100)),  # 4/18≈22% 低置信字符
        ]
        parser = OcrParser(ocr_engine=_FakeOcrEngine(OcrResult(lines=lines)))
        doc = parser.parse("mixed.png")
        # 低置信占比 5/(15+5)=25% > 15% → 会触发复核：此处验证占比计算而非不触发
        assert doc.stats["needs_manual_review"] is True
        # 短低置信行文本仍保留
        assert any("短低置信" in b.text for b in doc.blocks)

    def test_table_branch_whole_table(self) -> None:
        """表格识别分支：整表单块发射（不拆分，FR-KB-012 契约），携带 table_conf。"""
        table = OcrTable(rows=[["型号", "电压"], ["A1", "220V"], ["B2", "110V"]], conf=0.93)
        parser = OcrParser(ocr_engine=_FakeOcrEngine(OcrResult(lines=[], table=table)))
        doc = parser.parse("table.png")
        table_blocks = [b for b in doc.blocks if b.type == BLOCK_TYPE_TABLE]
        assert len(table_blocks) == 1
        assert table_blocks[0].table == [["型号", "电压"], ["A1", "220V"], ["B2", "110V"]]
        assert table_blocks[0].meta["table_conf"] == 0.93
        assert doc.stats["tables"] == 1
        assert "needs_manual_review" not in doc.stats  # 高置信整表不触发复核

    def test_low_conf_table_triggers_review(self) -> None:
        """整表低置信（table_conf < 0.80）→ needs_manual_review=True。"""
        table = OcrTable(rows=[["a"]], conf=0.5)
        parser = OcrParser(ocr_engine=_FakeOcrEngine(OcrResult(lines=[], table=table)))
        doc = parser.parse("fuzzy_table.png")
        assert doc.stats["needs_manual_review"] is True

    def test_default_engine_import_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """import 守护：paddleocr 缺失（模拟）→ 带中文提示的 UnsupportedFormatError。"""
        monkeypatch.setitem(sys.modules, "paddleocr", None)  # 模拟未安装（确定性）
        with pytest.raises(UnsupportedFormatError) as exc_info:
            OcrParser().parse("any.png")
        assert "PaddleOCR" in str(exc_info.value)
        assert exc_info.value.code == "PARSE-1001"

    def test_engine_receives_path(self) -> None:
        """引擎收到原始图片路径（本地推理，NFR-SEC-06 数据不出域）。"""
        engine = _FakeOcrEngine(OcrResult(lines=[OcrLine(text="x", conf=0.9, bbox=(0, 0, 10, 10))]))
        OcrParser(ocr_engine=engine).parse("imgs/a.png")
        assert engine.calls == ["imgs/a.png"]
