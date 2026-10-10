# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""Word 文档解析器（FR-KB-010）：python-docx 按正文 XML 顺序遍历。

段落与表格（w:p / w:tbl）按 body 顺序交替产出，保持原文穿插顺序；
表格整块发射、不跨块拆分（FR-KB-012 契约）。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.3
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .base import (
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_PARAGRAPH,
    BaseParser,
    Block,
    CorruptOrEncryptedError,
    UnsupportedFormatError,
)


def _load_docx() -> Any:
    """惰性加载 python-docx（import 守护）：未安装时抛带中文提示的不支持格式错误。"""
    try:
        import docx  # noqa: F401 —— 仅探测可导入性
    except ImportError as exc:
        raise UnsupportedFormatError(
            "未安装 python-docx，无法解析 Word 文档（.docx）；请安装依赖：pip install python-docx"
        ) from exc
    return docx


@dataclass
class _BodyNode:
    """body 顺序遍历节点：段落或表格二选一（is_table 区分）。"""

    is_table: bool
    para: Any = None    # docx.text.paragraph.Paragraph
    table: Any = None   # docx.table.Table


class WordParser(BaseParser):
    """python-docx：按 body XML 顺序遍历，段落与表格保持原文穿插顺序。"""

    parser_name = "word_parser"
    fmt = "word"

    #: 标题样式前缀：英文模板 'Heading 1' 与中文模板 '标题 1' 均识别
    _HEADING_STYLE_PREFIXES = ("heading", "标题")

    def _do_parse(self, path: str) -> list[Block]:
        docx = _load_docx()
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            document = docx.Document(path)
        except Exception as exc:
            # .doc 旧版二进制/损坏文件会让 python-docx 抛包错误 → PARSE-1002
            raise CorruptOrEncryptedError(f"Word 文件无法打开（可能为旧版 .doc 或已损坏）：{exc}") from exc

        blocks: list[Block] = []
        for node in self._iter_body(document, Paragraph, Table, qn):
            if node.is_table:
                # 表格整块发射，不跨块（FR-KB-012 契约）；合并单元格在 docx 中表现为跨格重复文本
                rows = [[cell.text.strip() for cell in row.cells] for row in node.table.rows]
                blocks.append(self.emit_table(self._clean_rows(rows), {"origin": "word_table"}))
                continue
            para = node.para
            style_name = (para.style.name or "") if para.style is not None else ""
            if style_name.lower().startswith(self._HEADING_STYLE_PREFIXES):
                # 'Heading 2' / '标题 2' → heading_level=2；无数字的标题样式默认 1 级
                blocks.append(
                    Block(
                        type=BLOCK_TYPE_HEADING,
                        heading_level=self._heading_level(style_name),
                        text=para.text.strip(),
                        meta={"style": style_name},
                    )
                )
            elif para.text.strip():
                blocks.append(Block(type=BLOCK_TYPE_PARAGRAPH, text=para.text.strip()))
        return blocks

    @staticmethod
    def _iter_body(document: Any, paragraph_cls: Any, table_cls: Any, qn: Any) -> list[_BodyNode]:
        """按 body XML 顺序遍历，段落与 w:tbl 交替产出（保持原文穿插顺序）。"""
        nodes: list[_BodyNode] = []
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                nodes.append(_BodyNode(is_table=False, para=paragraph_cls(child, document)))
            elif child.tag == qn("w:tbl"):
                nodes.append(_BodyNode(is_table=True, table=table_cls(child, document)))
        return nodes

    @staticmethod
    def _heading_level(style_name: str) -> int:
        """从样式名提取标题层级：'Heading 2'/'标题 3' → 2/3；无数字默认 1，并收敛到 1~6。"""
        matched = re.search(r"(\d+)", style_name)
        level = int(matched.group(1)) if matched else 1
        return max(1, min(6, level))

    @staticmethod
    def _clean_rows(rows: list[list[str]]) -> list[list[str]]:
        """表格行清洗：去首尾全空行（表内空行保留，属原表结构的一部分）。"""
        start, end = 0, len(rows)
        while start < end and all(cell == "" for cell in rows[start]):
            start += 1
        while end > start and all(cell == "" for cell in rows[end - 1]):
            end -= 1
        return rows[start:end]
