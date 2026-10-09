# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""PDF 解析器实测用例（FR-KB-010，依赖 PyMuPDF；未安装自动跳过）。

覆盖（对应实现方案 §9 测试要点）：
- 文本层抽取 + 大字号短行标题判定（test_pdf_text_and_heading）；
- 真实线框表格整表抽取 + 正文与表格内容不重复（表格 bbox 剔除）；
- 加密 PDF → PARSE-1002（test_pdf_encrypted）；
- 扫描版（无文本层）→ OCR 降级判定：stats.degrade 含 no_text_layer->ocr；
  OCR 引擎不可用时抛带中文提示的 UnsupportedFormatError（import 守护）。

跨页表格合并的纯逻辑用例见 test_parsers.py（不依赖 PyMuPDF）。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.2、§9
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
from services.kb.core.pipeline.parsers.ocr_parser import OcrLine, OcrResult
from services.kb.core.pipeline.parsers.pdf_parser import DEGRADE_NO_TEXT_LAYER_TO_OCR, PdfParser

fitz = pytest.importorskip("fitz", reason="PyMuPDF 未安装，PDF 实测跳过")

# 测试用字号：标题 24pt / 正文 11pt（加权均值 ≈14.3 → 标题/正文比 ≈1.7 → 2 级标题）
_TITLE_SIZE = 24
_BODY_SIZE = 11


def _make_text_pdf(path: str) -> str:
    """生成 2 页文本型 PDF：大字号标题 + 正文段落（无表格）。"""
    doc = fitz.open()
    for page_no in range(2):
        page = doc.new_page()
        page.insert_text((72, 80), f"Product Specification {page_no + 1}", fontsize=_TITLE_SIZE)
        page.insert_text((72, 140), "This is the first body paragraph.", fontsize=_BODY_SIZE)
        page.insert_text((72, 170), "This is the second body paragraph.", fontsize=_BODY_SIZE)
    doc.save(path)
    doc.close()
    return path


def _make_table_pdf(path: str) -> str:
    """生成含 1 张 3x3 线框表格的 PDF：正文在表格上方，验证表格内容不被重复抽取。"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Spec Table", fontsize=_TITLE_SIZE)
    page.insert_text((72, 140), "Body text above the table.", fontsize=_BODY_SIZE)
    # 3x3 表格线框：列边界 x=100/200/300/400，行边界 y=200/260/320/380
    x_edges = [100, 200, 300, 400]
    y_edges = [200, 260, 320, 380]
    for x in x_edges:
        page.draw_line((x, y_edges[0]), (x, y_edges[-1]), color=(0, 0, 0), width=1)
    for y in y_edges:
        page.draw_line((x_edges[0], y), (x_edges[-1], y), color=(0, 0, 0), width=1)
    for r in range(3):
        for c in range(3):
            page.insert_text((x_edges[c] + 10, y_edges[r] + 35), f"C{r}{c}", fontsize=_BODY_SIZE)
    doc.save(path)
    doc.close()
    return path


def _make_scanned_pdf(path: str, pages: int = 2) -> str:
    """生成扫描版 PDF（仅图片、无文本层），触发 OCR 降级判定。"""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 100))
        pix.clear_with(255)
        page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()
    return path


class _FakeOcrEngine:
    """OCR 引擎替身：记录调用并返回预设行结果（不依赖 PaddleOCR）。"""

    def __init__(self, result: OcrResult | None = None) -> None:
        self.result = result or OcrResult(lines=[])
        self.calls: list[str] = []

    def run(self, image_path: str) -> OcrResult:
        self.calls.append(image_path)
        return self.result


class TestPdfParserRealFile:
    """PyMuPDF 实测：文本层/表格/加密/降级。"""

    def test_pdf_text_and_heading(self, tmp_path: Any) -> None:
        """文本层抽取：大字号短行 → 2 级标题；正文成段落块；无表格块。"""
        path = _make_text_pdf(str(tmp_path / "text.pdf"))
        doc = PdfParser().parse(path)
        assert doc.source_format == "pdf"
        headings = [b for b in doc.blocks if b.type == BLOCK_TYPE_HEADING]
        paragraphs = [b for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH]
        assert len(headings) == 2                       # 每页 1 个标题
        assert headings[0].heading_level == 2           # 24pt / 加权均值≈14.3 ≈ 1.7 倍 → 2 级
        assert headings[0].text.startswith("Product Specification")
        assert headings[0].meta["page"] == 0            # 页码溯源
        assert len(paragraphs) == 4                     # 每页 2 个正文块
        assert doc.stats["tables"] == 0
        assert "body paragraph" in paragraphs[0].text

    def test_pdf_table_extraction_without_duplication(self, tmp_path: Any) -> None:
        """线框表格整表抽取：恰好 1 个 3 行 table 块；正文不含表格单元格文本（bbox 剔除防重复）。"""
        path = _make_table_pdf(str(tmp_path / "table.pdf"))
        doc = PdfParser().parse(path)
        table_blocks = [b for b in doc.blocks if b.type == BLOCK_TYPE_TABLE]
        assert len(table_blocks) == 1
        rows = table_blocks[0].table or []
        assert len(rows) == 3
        assert rows[0] == ["C00", "C01", "C02"]         # 完整二维表（表格不跨块拆分契约）
        assert table_blocks[0].meta["page"] == 0
        paragraph_text = " ".join(b.text for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH)
        assert "C01" not in paragraph_text              # 表格内容不在正文块重复出现
        assert "Body text above the table." in paragraph_text  # 正文完整保留（无信息丢失）

    def test_pdf_encrypted(self, tmp_path: Any) -> None:
        """加密 PDF：PARSE-1002（无法自动降级 → 上游转人工兜底队列）。"""
        path = str(tmp_path / "enc.pdf")
        doc = fitz.open()
        doc.new_page().insert_text((72, 100), "secret", fontsize=11)
        doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u1234", owner_pw="o1234")
        doc.close()
        from services.kb.core.pipeline.parsers.base import CorruptOrEncryptedError

        with pytest.raises(CorruptOrEncryptedError) as exc_info:
            PdfParser().parse(path)
        assert exc_info.value.code == "PARSE-1002"
        assert "加密" in str(exc_info.value)

    def test_pdf_no_text_layer_without_ocr_engine(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """扫描版 PDF + OCR 引擎不可用（模拟 paddleocr 未安装）→ 中文提示 UnsupportedFormatError。"""
        path = _make_scanned_pdf(str(tmp_path / "scan.pdf"))
        monkeypatch.setitem(sys.modules, "paddleocr", None)  # 模拟未安装（确定性，不随真实环境漂移）
        with pytest.raises(UnsupportedFormatError) as exc_info:
            PdfParser().parse(path)
        assert "PaddleOCR" in str(exc_info.value)
        assert exc_info.value.code == "PARSE-1001"

    def test_pdf_no_text_layer_degrade_to_ocr(self, tmp_path: Any) -> None:
        """扫描版 PDF + 注入 OCR 引擎 → 自动降级：stats.degrade 含 no_text_layer->ocr，逐页识别。"""
        path = _make_scanned_pdf(str(tmp_path / "scan.pdf"), pages=2)
        engine = _FakeOcrEngine(
            OcrResult(lines=[OcrLine(text="scanned text line", conf=0.92, bbox=(50, 40, 300, 60))])
        )
        doc = PdfParser(ocr_engine=engine, cfg={"parse.pdf.text_min_chars_per_page": 20}).parse(path)
        assert doc.stats["degrade"] == [DEGRADE_NO_TEXT_LAYER_TO_OCR]
        assert len(engine.calls) == 2                   # 2 页逐页 300dpi 渲染识别
        assert all(p.endswith(".png") for p in engine.calls)
        texts = [b.text for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH]
        assert any("scanned text line" in t for t in texts)  # 降级后文本非空
        # 降级后原文本层 blocks 被整体替换（无空结果残留）
        assert len(doc.blocks) == 2

    def test_pdf_low_threshold_no_false_degrade(self, tmp_path: Any) -> None:
        """文本层充足（>20 字/页）→ 不触发 OCR 降级，stats 无 degrade 标记。"""
        path = _make_text_pdf(str(tmp_path / "text.pdf"))
        doc = PdfParser().parse(path)
        assert "degrade" not in doc.stats
