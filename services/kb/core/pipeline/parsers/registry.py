# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""格式注册表（FR-KB-010）：扩展名/魔数 → 解析器路由。

- resolve(path)：按扩展名路由到对应解析器（惰性导入，可选依赖缺失不影响其他格式）；
- 未注册格式抛 UnsupportedFormatError（错误码 PARSE-1001，上游不重试直接转人工兜底）；
- sniff_format(path)：魔数嗅探（%PDF / PK\\x03\\x04 / PNG / JPEG / RIFF 等），
  供编排层与扩展名做三重校验（实现方案 §6.6②）。

新增格式：实现 BaseParser 子类并调用 REGISTRY.register() 注册（NFR-MAI-01 可维护性）。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §3、§6.6
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .base import BaseParser, UnsupportedFormatError

# 解析器工厂：惰性导入被路由模块，避免某一可选依赖缺失拖垮整个注册表
ParserFactory = Callable[[], BaseParser]


@dataclass(frozen=True)
class FormatRule:
    """单条格式路由规则：扩展名集合 → 解析器工厂与格式枚举。"""

    extensions: tuple[str, ...]
    fmt: str           # ingest_pipeline_log.source_format 枚举值
    parser_name: str
    factory: ParserFactory


def _make_pdf_parser() -> BaseParser:
    from .pdf_parser import PdfParser

    return PdfParser()


def _make_word_parser() -> BaseParser:
    from .word_parser import WordParser

    return WordParser()


def _make_excel_parser() -> BaseParser:
    from .excel_parser import ExcelParser

    return ExcelParser()


def _make_ocr_parser() -> BaseParser:
    from .ocr_parser import OcrParser

    return OcrParser()


def _make_asr_parser() -> BaseParser:
    from .asr_parser import AsrParser

    return AsrParser()


def _raise_legacy_doc() -> BaseParser:
    """旧版 .doc 二进制格式拦截：python-docx 不支持，提示转换后走 .docx。"""
    raise UnsupportedFormatError(
        "暂不支持旧版 .doc 格式（Word 97-2003 二进制）；请先在 Word 中另存为 .docx 后重新导入"
    )


def _raise_legacy_xls() -> BaseParser:
    """旧版 .xls 二进制格式拦截：openpyxl 不支持，提示转换后走 .xlsx。"""
    raise UnsupportedFormatError(
        "暂不支持旧版 .xls 格式（Excel 97-2003 二进制）；请先在 Excel 中另存为 .xlsx 后重新导入"
    )


class ParserRegistry:
    """扩展名 → 解析器注册表（格式路由唯一入口）。"""

    def __init__(self) -> None:
        self._rules: dict[str, FormatRule] = {}

    def register(self, rule: FormatRule) -> None:
        """注册一条格式路由规则（同一扩展名后注册者覆盖先注册者）。"""
        for ext in rule.extensions:
            self._rules[ext.lower()] = rule

    def rule_of(self, path: str) -> FormatRule:
        """按文件扩展名取路由规则；未注册格式抛 UnsupportedFormatError（PARSE-1001）。"""
        ext = self._ext(path)
        rule = self._rules.get(ext)
        if rule is None:
            raise UnsupportedFormatError(
                f"不支持的文件格式：{ext or path}（PARSE-1001）；"
                "当前支持 pdf / docx / xlsx / xlsm / png / jpg / jpeg / bmp / tiff / webp / mp3 / wav / m4a / flac / ogg"
            )
        return rule

    def resolve(self, path: str) -> BaseParser:
        """按扩展名路由并构造解析器实例（可选依赖缺失时由解析器 import 守护抛错）。"""
        return self.rule_of(path).factory()

    def fmt_of(self, path: str) -> str:
        """取 source_format 枚举值（与 ingest_pipeline_log.source_format 一致）。"""
        return self.rule_of(path).fmt

    def name_of(self, path: str) -> str:
        """取解析器名（如 'pdf_parser'，写入 log.action_detail.parser）。"""
        return self.rule_of(path).parser_name

    @staticmethod
    def _ext(path: str) -> str:
        """提取小写扩展名（含点）；无扩展名返回空串。"""
        idx = path.rfind(".")
        if idx <= 0 or idx == len(path) - 1:
            return ""
        return path[idx:].lower()

    @staticmethod
    def sniff_format(path: str) -> str | None:
        """魔数嗅探文件真实格式：返回格式标识（pdf/word/excel/image/audio），无法识别返回 None。

        供编排层与扩展名做三重校验（扩展名 + mimetypes + 魔数，实现方案 §6.6②）；
        docx/xlsx 同为 PK\\x03\\x04 ZIP 头，此处统一返回 zip，由扩展名进一步区分。
        """
        try:
            with open(path, "rb") as fh:
                head = fh.read(12)
        except OSError:
            return None
        if head.startswith(b"%PDF"):
            return "pdf"
        if head.startswith(b"PK\x03\x04"):
            return "zip"  # docx / xlsx 共用 ZIP 头，交由扩展名区分
        if head.startswith(b"\x89PNG"):
            return "image"
        if head.startswith(b"\xff\xd8\xff"):
            return "image"  # JPEG
        if head.startswith(b"BM"):
            return "image"  # BMP
        if head.startswith(b"RIFF") and head[8:12] in (b"WEBP", b"WAVE"):
            return "image" if head[8:12] == b"WEBP" else "audio"
        if head.startswith(b"ID3") or head.startswith(b"\xff\xfb") or head.startswith(b"\xff\xf3"):
            return "audio"  # MP3（ID3v2 / MPEG 帧）
        if head[4:8] == b"ftyp":
            return "audio"  # M4A / AAC 等 ISO 基媒体格式
        if head.startswith(b"fLaC"):
            return "audio"
        if head.startswith(b"OggS"):
            return "audio"
        return None


def build_default_registry() -> ParserRegistry:
    """构造默认注册表：五种来源格式 + 旧版格式显式拦截。"""
    registry = ParserRegistry()
    # PDF（PyMuPDF 直读；可选依赖，缺失时由 PdfParser import 守护抛中文提示）
    registry.register(FormatRule((".pdf",), "pdf", "pdf_parser", _make_pdf_parser))
    # Word（python-docx；.doc 旧版显式拦截）
    registry.register(FormatRule((".docx",), "word", "word_parser", _make_word_parser))
    registry.register(FormatRule((".doc",), "word", "word_parser", _raise_legacy_doc))
    # Excel（openpyxl；.xls 旧版显式拦截）
    registry.register(FormatRule((".xlsx", ".xlsm"), "excel", "excel_parser", _make_excel_parser))
    registry.register(FormatRule((".xls",), "excel", "excel_parser", _raise_legacy_xls))
    # 图片 OCR（PaddleOCR 本地推理）
    registry.register(
        FormatRule(
            (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"),
            "image_ocr",
            "ocr_parser",
            _make_ocr_parser,
        )
    )
    # 语音 ASR（本地 Whisper）
    registry.register(
        FormatRule(
            (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"),
            "audio_asr",
            "asr_parser",
            _make_asr_parser,
        )
    )
    return registry


# 全局默认注册表（编排层 parse_service 直接消费，实现方案 §6.6）
REGISTRY = build_default_registry()
