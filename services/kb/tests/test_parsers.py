# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""解析器基础组件与 Word/registry 单元测试（FR-KB-010）。

覆盖（对应实现方案 §9 测试要点）：
- BaseParser 模板方法：统计汇总 / 异常包装 / emit_table 原子块契约；
- Word：标题层级与段落/表格穿插顺序（test_word_heading_order）；
- 表格整块不拆分契约（FR-KB-012 衔接）：大表必须单个 table 块完整产出；
- registry：扩展名路由、未注册格式错误码 PARSE-1001、旧版 .doc/.xls 拦截、魔数嗅探；
- import 守护：可选依赖缺失时抛带中文提示的 UnsupportedFormatError。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.1、§6.3、§6.6、§9
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
    BaseParser,
    Block,
    ParseError,
    UnsupportedFormatError,
    UnstructuredDoc,
)
from services.kb.core.pipeline.parsers.pdf_parser import PdfParser
from services.kb.core.pipeline.parsers.registry import REGISTRY
from services.kb.core.pipeline.parsers.word_parser import WordParser

# ─────────────────────────────── BaseParser 基础 ───────────────────────────────


class _DummyParser(BaseParser):
    """测试替身：按预设产出 blocks，或抛预设异常。"""

    parser_name = "dummy_parser"
    fmt = "dummy"

    def __init__(self, blocks: list[Block] | None = None, error: Exception | None = None) -> None:
        self.preset_blocks = blocks or []
        self.preset_error = error

    def _do_parse(self, path: str) -> list[Block]:
        if self.preset_error is not None:
            raise self.preset_error
        return self.preset_blocks


class TestBaseParser:
    """模板方法：统计汇总与异常包装。"""

    def test_stats_summary(self) -> None:
        """chars/tables 统计正确，source_format 取解析器 fmt。"""
        blocks = [
            Block(type=BLOCK_TYPE_HEADING, heading_level=1, text="标题甲"),
            Block(type=BLOCK_TYPE_PARAGRAPH, text="正文十个字符的内容！"),
            BaseParser.emit_table([["a", "b"], ["c", "d"]], {}),
        ]
        doc = _DummyParser(blocks).parse("/tmp/x.dummy")
        assert isinstance(doc, UnstructuredDoc)
        assert doc.source_file == "/tmp/x.dummy"
        assert doc.source_format == "dummy"
        assert doc.stats["chars"] == len("标题甲") + len("正文十个字符的内容！")  # table 块 text 为空不计入
        assert doc.stats["tables"] == 1

    def test_generic_error_wrapped_as_parse_error(self) -> None:
        """第三方库异常统一包装为 ParseError，且保留原始异常链与中文前缀。"""
        with pytest.raises(ParseError) as exc_info:
            _DummyParser(error=RuntimeError("boom")).parse("/tmp/x.dummy")
        assert "解析失败（dummy_parser）" in str(exc_info.value)
        assert exc_info.value.__cause__ is not None

    def test_parse_error_passthrough(self) -> None:
        """本模块业务异常（如 UnsupportedFormatError）原样上抛，不重复包装。"""
        original = UnsupportedFormatError("未安装测试依赖")
        with pytest.raises(UnsupportedFormatError) as exc_info:
            _DummyParser(error=original).parse("/tmp/x.dummy")
        assert exc_info.value is original

    def test_extra_stats_merged(self) -> None:
        """子类经 _set_stats 追加的统计（degrade 等）合并进 doc.stats。"""

        class _StatParser(_DummyParser):
            def _do_parse(self, path: str) -> list[Block]:
                self._set_stats(degrade=["no_text_layer->ocr"], needs_manual_review=True)
                return []

        stats = _StatParser().parse("/tmp/x.dummy").stats
        assert stats["degrade"] == ["no_text_layer->ocr"]
        assert stats["needs_manual_review"] is True

    def test_to_dict_serializable(self) -> None:
        """UnstructuredDoc.to_dict 输出可 JSON 归档结构（OSS 解析产物，§4）。"""
        doc = _DummyParser([BaseParser.emit_table([["a"]], {"page": 1})]).parse("/tmp/x.dummy")
        payload = doc.to_dict()
        assert payload["source_format"] == "dummy"
        assert payload["blocks"][0]["type"] == BLOCK_TYPE_TABLE
        assert payload["blocks"][0]["table"] == [["a"]]


class TestEmitTableContract:
    """emit_table 表格原子块契约（FR-KB-012：表格不跨块拆分）。"""

    def test_emit_table_shape(self) -> None:
        """表格块 type='table' 且携带完整二维表。"""
        blk = BaseParser.emit_table([["h1", "h2"], ["v1", "v2"]], {"page": 3})
        assert blk.type == BLOCK_TYPE_TABLE
        assert blk.table == [["h1", "h2"], ["v1", "v2"]]
        assert blk.meta == {"page": 3}

    def test_emit_table_copies_rows(self) -> None:
        """行集做拷贝：调用方后续修改不污染已发射块。"""
        rows = [["a"]]
        blk = BaseParser.emit_table(rows, None)
        rows[0][0] = "changed"
        assert blk.table == [["a"]]

    def test_emit_table_rejects_none(self) -> None:
        """rows=None 直接抛错，禁止产生 table=None 的脏块。"""
        with pytest.raises(ParseError):
            BaseParser.emit_table(None, None)  # type: ignore[arg-type]


# ─────────────────────────────── Word 实测（python-docx）───────────────────────


def _build_docx(path: str, *, big_table_rows: int = 0) -> None:
    """用 python-docx 生成测试用 docx：标题/段落/表格混排，保持写入顺序。

    python-docx 未安装时整组实测用例自动跳过（importorskip）。
    """
    docx = pytest.importorskip("docx", reason="python-docx 未安装，Word 实测跳过")
    document = docx.Document()
    document.add_heading("产品参数说明", level=2)      # 'Heading 2' → heading_level=2
    document.add_paragraph("本段介绍产品的基本用途与适用场景。")
    document.add_paragraph("")                        # 空段落：解析时应被跳过
    if big_table_rows:
        table = document.add_table(rows=big_table_rows, cols=3)
        for r in range(big_table_rows):
            for c in range(3):
                table.cell(r, c).text = f"R{r}C{c}"
    else:
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "参数"
        table.cell(0, 1).text = "数值"
        table.cell(1, 0).text = "电压"
        table.cell(1, 1).text = "220V"
    document.add_paragraph("表格之后的收尾段落。")
    document.save(path)


class TestWordParser:
    """python-docx：段落与表格按 body 顺序保序产出。"""

    def test_word_heading_order(self, tmp_path: Any) -> None:
        """混排「标题 2」段落与表格：heading_level=2；段落/表格保持原文穿插顺序。"""
        path = str(tmp_path / "mixed.docx")
        _build_docx(path)
        doc = WordParser().parse(path)
        assert doc.source_format == "word"
        types = [b.type for b in doc.blocks]
        # 顺序契约：heading → paragraph → table → paragraph（空段落被跳过）
        assert types == [BLOCK_TYPE_HEADING, BLOCK_TYPE_PARAGRAPH, BLOCK_TYPE_TABLE, BLOCK_TYPE_PARAGRAPH]
        assert doc.blocks[0].heading_level == 2
        assert doc.blocks[0].text == "产品参数说明"
        assert doc.blocks[2].table == [["参数", "数值"], ["电压", "220V"]]

    def test_word_table_atomic_not_split(self, tmp_path: Any) -> None:
        """表格整块不拆分契约：12 行大表必须恰好 1 个 table 块且 12 行完整；
        表格单元格文本不得泄漏到任何 paragraph 块（无信息重复/丢失）。"""
        path = str(tmp_path / "big_table.docx")
        _build_docx(path, big_table_rows=12)
        doc = WordParser().parse(path)
        table_blocks = [b for b in doc.blocks if b.type == BLOCK_TYPE_TABLE]
        assert len(table_blocks) == 1
        assert len(table_blocks[0].table or []) == 12
        assert (table_blocks[0].table or [])[11] == ["R11C0", "R11C1", "R11C2"]
        paragraph_text = "".join(b.text for b in doc.blocks if b.type == BLOCK_TYPE_PARAGRAPH)
        assert "R11C2" not in paragraph_text  # 表格内容不在段落块重复出现

    def test_word_chinese_heading_style(self) -> None:
        """中文模板样式名 '标题 3' → heading_level=3。"""
        assert WordParser._heading_level("标题 3") == 3
        assert WordParser._heading_level("Heading 2") == 2
        assert WordParser._heading_level("Heading") == 1  # 无数字默认 1 级

    def test_word_corrupt_file(self, tmp_path: Any) -> None:
        """损坏/非 docx 文件 → PARSE-1002（转人工兜底队列依据）。"""
        path = tmp_path / "bad.docx"
        path.write_bytes(b"not a docx file")
        with pytest.raises(ParseError) as exc_info:
            WordParser().parse(str(path))
        assert exc_info.value.code == "PARSE-1002"

    def test_word_import_guard(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """import 守护：python-docx 缺失时抛带中文提示的 UnsupportedFormatError。"""
        path = tmp_path / "any.docx"
        path.write_bytes(b"")
        monkeypatch.setitem(sys.modules, "docx", None)  # 模拟未安装：import docx → ImportError
        with pytest.raises(UnsupportedFormatError) as exc_info:
            WordParser().parse(str(path))
        assert "python-docx" in str(exc_info.value)
        assert exc_info.value.code == "PARSE-1001"


# ─────────────────────────────── registry 路由 ───────────────────────────────


class TestRegistry:
    """扩展名/格式路由与魔数嗅探。"""

    @pytest.mark.parametrize(
        ("filename", "fmt", "parser_name"),
        [
            ("p0/raw/产品目录.pdf", "pdf", "pdf_parser"),
            ("报价单.docx", "word", "word_parser"),
            ("参数表.xlsx", "excel", "excel_parser"),
            ("参数表.xlsm", "excel", "excel_parser"),
            ("scan.png", "image_ocr", "ocr_parser"),
            ("photo.jpg", "image_ocr", "ocr_parser"),
            ("photo.JPEG", "image_ocr", "ocr_parser"),  # 扩展名大小写不敏感
            ("meeting.mp3", "audio_asr", "asr_parser"),
            ("voice.wav", "audio_asr", "asr_parser"),
        ],
    )
    def test_route_by_extension(self, filename: str, fmt: str, parser_name: str) -> None:
        """按扩展名路由：fmt_of/name_of 与 source_format 枚举一致。"""
        assert REGISTRY.fmt_of(filename) == fmt
        assert REGISTRY.name_of(filename) == parser_name

    def test_unsupported_format_error(self) -> None:
        """未注册格式：UnsupportedFormatError（PARSE-1001，上游不重试直接转人工队列）。"""
        with pytest.raises(UnsupportedFormatError) as exc_info:
            REGISTRY.resolve("材料.zip")
        assert exc_info.value.code == "PARSE-1001"
        assert "不支持的文件格式" in str(exc_info.value)

    @pytest.mark.parametrize("filename", ["旧文件.doc", "旧表格.xls"])
    def test_legacy_binary_formats_rejected(self, filename: str) -> None:
        """旧版 .doc/.xls 二进制格式显式拦截并给出转换提示。"""
        with pytest.raises(UnsupportedFormatError) as exc_info:
            REGISTRY.resolve(filename)
        assert "另存为" in str(exc_info.value)

    def test_resolve_returns_parser_instance(self) -> None:
        """resolve 构造对应解析器实例（惰性导入不影响其他格式）。"""
        parser = REGISTRY.resolve("a.docx")
        assert isinstance(parser, WordParser)
        assert parser.parser_name == "word_parser"

    def test_sniff_format_magic_bytes(self, tmp_path: Any) -> None:
        """魔数嗅探：%PDF / PK 头 / PNG / JPEG / MP3 识别正确，无法识别返回 None。"""
        samples = {
            "a.pdf": (b"%PDF-1.7\n...", "pdf"),
            "b.bin": (b"PK\x03\x04rest-of-zip", "zip"),  # docx/xlsx 共用 ZIP 头
            "c.png": (b"\x89PNG\r\n\x1a\n", "image"),
            "d.jpg": (b"\xff\xd8\xff\xe0", "image"),
            "e.mp3": (b"ID3\x04\x00tagdata", "audio"),
            "f.txt": (b"hello world", None),
        }
        for name, (content, expected) in samples.items():
            p = tmp_path / name
            p.write_bytes(content)
            assert REGISTRY.sniff_format(str(p)) == expected, name


# ─────────────────────────── PDF 纯逻辑（不依赖 PyMuPDF）───────────────────────


class TestPdfPureLogic:
    """跨页合并/清洗/标题分档的纯函数测试（pymupdf 缺失环境同样可回归）。"""

    @staticmethod
    def _table(rows: list[list[str]], page: int) -> Block:
        return BaseParser.emit_table(rows, {"page": page})

    def test_merge_cross_page_tables(self) -> None:
        """相邻页同构表格 + 重复表头 → 去重表头合并为 1 个 table 块，并记录覆盖页码。"""
        header = ["型号", "电压", "功率"]
        blocks = [
            self._table([header, ["A1", "220V", "10W"]], page=0),
            Block(type=BLOCK_TYPE_PARAGRAPH, text="跨页表格正文"),
            self._table([header, ["B2", "110V", "20W"]], page=1),
        ]
        merged = PdfParser._merge_cross_page_tables(blocks)
        table_blocks = [b for b in merged if b.type == BLOCK_TYPE_TABLE]
        assert len(table_blocks) == 1
        assert table_blocks[0].table == [header, ["A1", "220V", "10W"], ["B2", "110V", "20W"]]
        assert table_blocks[0].meta["pages"] == [0, 1]
        assert merged[1].type == BLOCK_TYPE_PARAGRAPH  # 中间正文块不受影响

    def test_no_merge_when_header_not_repeated(self) -> None:
        """列数一致但表头不重复（同构不同表）→ 不合并，保持两个独立 table 块。"""
        blocks = [
            self._table([["型号", "电压"], ["A1", "220V"]], page=0),
            self._table([["名称", "价格"], ["B2", "$9"]], page=1),
        ]
        merged = PdfParser._merge_cross_page_tables(blocks)
        assert len([b for b in merged if b.type == BLOCK_TYPE_TABLE]) == 2

    def test_no_merge_when_columns_differ(self) -> None:
        """列数不一致 → 不合并（防误并异构表）。"""
        blocks = [
            self._table([["型号", "电压", "功率"], ["A1", "220V", "10W"]], page=0),
            self._table([["型号", "电压", "功率", "重量"], ["B2", "110V", "20W", "1kg"]], page=1),
        ]
        merged = PdfParser._merge_cross_page_tables(blocks)
        assert len([b for b in merged if b.type == BLOCK_TYPE_TABLE]) == 2

    def test_clean_rows(self) -> None:
        """None→空串、单元格去空白、去首尾全空行（表内空行保留）。"""
        assert PdfParser._clean_rows([[" a ", None], [None, " b "], ["", ""]]) == [["a", ""], ["", "b"]]
        assert PdfParser._clean_rows([]) == []
        assert PdfParser._clean_rows(None) == []  # type: ignore[arg-type]

    def test_heading_level_mapping(self) -> None:
        """字号/正文均值倍率 → 标题层级分档。"""
        assert PdfParser._heading_level(2.2) == 1
        assert PdfParser._heading_level(1.7) == 2
        assert PdfParser._heading_level(1.4) == 3
        assert PdfParser._heading_level(1.25) == 4

    def test_bbox_overlap_detection(self) -> None:
        """表格 bbox 内文本判定：重叠面积占比 > 0.5 视为表内文本。"""
        tables = [(100, 200, 400, 380)]
        assert PdfParser._bbox_in_any((110, 210, 390, 370), tables) is True   # 完全在表内
        assert PdfParser._bbox_in_any((10, 20, 90, 60), tables) is False     # 表外正文
        assert PdfParser._bbox_in_any((80, 20, 120, 60), tables) is False    # 仅轻微越界

    def test_pdf_import_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """import 守护：pymupdf 缺失时抛带中文提示的 UnsupportedFormatError。"""
        monkeypatch.setitem(sys.modules, "fitz", None)  # 模拟未安装
        with pytest.raises(UnsupportedFormatError) as exc_info:
            PdfParser().parse("x.pdf")
        assert "PyMuPDF" in str(exc_info.value)
        assert exc_info.value.code == "PARSE-1001"
