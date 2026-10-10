# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""kb-service 知识库解析器包（FR-KB-010 多格式文档解析）。

五种来源格式（PDF / Word / Excel / 图片 OCR / 语音 ASR）统一经
registry.resolve(path) 路由到对应解析器，输出 UnstructuredDoc 中间格式。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §3、§6.1
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from .asr_parser import AsrParser, AsrSegment
from .base import (
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_PARAGRAPH,
    BLOCK_TYPE_TABLE,
    BaseParser,
    Block,
    CorruptOrEncryptedError,
    ParseError,
    UnsupportedFormatError,
    UnstructuredDoc,
)
from .excel_parser import ExcelParser
from .ocr_parser import OcrLine, OcrParser, OcrResult, OcrTable
from .pdf_parser import PdfParser
from .registry import REGISTRY, FormatRule, ParserRegistry
from .word_parser import WordParser

__all__ = [
    "AsrParser",
    "AsrSegment",
    "BaseParser",
    "Block",
    "BLOCK_TYPE_HEADING",
    "BLOCK_TYPE_PARAGRAPH",
    "BLOCK_TYPE_TABLE",
    "CorruptOrEncryptedError",
    "ExcelParser",
    "FormatRule",
    "OcrLine",
    "OcrParser",
    "OcrResult",
    "OcrTable",
    "ParseError",
    "ParserRegistry",
    "PdfParser",
    "REGISTRY",
    "UnsupportedFormatError",
    "UnstructuredDoc",
    "WordParser",
]
