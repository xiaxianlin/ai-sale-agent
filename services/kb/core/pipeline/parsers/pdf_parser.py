# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""PDF 解析器（FR-KB-010）：PyMuPDF 文本层直读 + 表格整表抽取 + 跨页合并 + OCR 降级。

处理顺序（实现方案 §6.2）：
① 每页表格优先抽取：整表出块并记录 bbox，供文本流剔除（防表格内容重复抽取）；
② 文本块按阅读顺序输出：跳过表格 bbox 内文本；字号>正文均值×1.2 且行长≤40 → 标题；
③ 跨页表格合并：相邻页尾/页首 table 列数一致且后表首行为重复表头 → 去重后并为一个 table 块；
④ OCR 降级：文本层字符数 < 阈值×页数 → 整档 300dpi 渲染逐页走 OCR（同 ocr_parser 版面后处理）。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.2、§7
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import tempfile
from typing import Any, Mapping

from .base import (
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_PARAGRAPH,
    BaseParser,
    Block,
    CorruptOrEncryptedError,
    UnsupportedFormatError,
    cfg_get,
)

# OCR 降级标记（写入 stats.degrade，最终落 ingest_pipeline_log.action_detail.degrade）
DEGRADE_NO_TEXT_LAYER_TO_OCR = "no_text_layer->ocr"


def _load_fitz() -> Any:
    """惰性加载 PyMuPDF（import 守护）：未安装时抛带中文提示的不支持格式错误。"""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise UnsupportedFormatError(
            "未安装 PyMuPDF（pymupdf），无法解析 PDF 文件；请安装依赖：pip install 'pymupdf>=1.24'"
        ) from exc
    return fitz


class PdfParser(BaseParser):
    """PyMuPDF：文本+表格+跨页合并+OCR 降级。"""

    parser_name = "pdf_parser"
    fmt = "pdf"

    def __init__(self, ocr_engine: Any = None, cfg: Mapping[str, Any] | None = None) -> None:
        # ocr_engine：OCR 引擎（duck-typing，须实现 run(image_path) → OcrResult）；
        # 为 None 时使用 ocr_parser 默认本地 PaddleOCR 引擎（首次降级时才加载，NFR-SEC-06 数据不出域）
        self.ocr_engine = ocr_engine
        self.cfg: dict[str, Any] = {
            "parse.pdf.text_min_chars_per_page": 20,  # §7：低于该值×页数判定无文本层
            "parse.pdf.ocr_dpi": 300,                 # §6.2④：整档 300dpi 渲染
            "parse.ocr.low_conf_threshold": 0.80,     # §7：OCR 低置信行阈值
        }
        if cfg:
            self.cfg.update(cfg)

    def _do_parse(self, path: str) -> list[Block]:
        fitz = _load_fitz()
        try:
            doc = fitz.open(path)
        except Exception as exc:
            raise CorruptOrEncryptedError(f"PDF 文件无法打开（可能已损坏）：{exc}") from exc

        with doc:
            if doc.needs_pass:
                # 加密 PDF 无法自动降级 → 抛 PARSE-1002，由上游转人工兜底队列（§6.2①）
                raise CorruptOrEncryptedError("PDF 已加密，无法自动解析；请转人工兜底队列处理")
            blocks: list[Block] = []
            text_chars = 0
            for page in doc:
                # ① 表格优先抽取：整表出块；bbox 记录供文本流剔除，防表格内容重复抽取
                table_bboxes: list[tuple[float, float, float, float]] = []
                for tbl in self._find_tables(page):
                    rows = self._clean_rows(tbl.extract())
                    if rows:
                        blocks.append(self.emit_table(rows, {"page": page.number}))
                        table_bboxes.append(tuple(tbl.bbox))
                # ② 文本块按阅读顺序输出（跳过表格 bbox 内文本；大字号短行 → 标题）
                for blk in self._text_blocks(page, table_bboxes):
                    blocks.append(blk)
                    text_chars += len(blk.text)
            page_count = max(doc.page_count, 1)
            # ③ 跨页表格合并：去重重复表头后并为一个 table 块
            blocks = self._merge_cross_page_tables(blocks)
            # ④ OCR 降级判定：文本层字符数 < 阈值×页数 → 整档渲染逐页走 OCR
            if text_chars < float(self.cfg["parse.pdf.text_min_chars_per_page"]) * page_count:
                blocks = self._ocr_fallback(doc)
                self._set_stats(degrade=[DEGRADE_NO_TEXT_LAYER_TO_OCR])
        return blocks

    # ── ① 表格抽取 ─────────────────────────────────────────────────────────
    @staticmethod
    def _find_tables(page: Any) -> list[Any]:
        """page.find_tables() 包装：个别页面线框异常时降级为无表格，不让单页失败拖垮整档。"""
        try:
            return list(page.find_tables())
        except Exception:  # noqa: BLE001 —— 表格探测失败按无表格处理，正文仍可抽取
            return []

    @staticmethod
    def _clean_rows(rows: list[list[Any]] | None) -> list[list[str]]:
        """表格行清洗：None→空串、去首尾全空行（表内空行保留，属原表结构）。"""
        if not rows:
            return []
        normalized = [["" if cell is None else str(cell).strip() for cell in row] for row in rows]
        start, end = 0, len(normalized)
        while start < end and all(cell == "" for cell in normalized[start]):
            start += 1
        while end > start and all(cell == "" for cell in normalized[end - 1]):
            end -= 1
        return normalized[start:end]

    # ── ② 文本块抽取与标题判定 ─────────────────────────────────────────────
    def _text_blocks(self, page: Any, exclude: list[tuple[float, float, float, float]]) -> list[Block]:
        """按阅读顺序输出文本块：跳过表格 bbox 内文本；大字号短行判为标题。

        标题判定（§6.2②）：块内最大字号 >= 正文加权均值×1.2 且行长<=40；
        层级按字号倍率分档（≥2.0→1 级，≥1.6→2 级，≥1.35→3 级，其余→4 级）。
        """
        raw = page.get_text("dict")
        # 正文基准字号：按字符数加权平均（正文占多数，标题不拉偏均值）
        total_chars = 0
        weighted = 0.0
        for blk in raw["blocks"]:
            if blk.get("type") != 0:
                continue
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    n = len(span.get("text", ""))
                    weighted += span.get("size", 0.0) * n
                    total_chars += n
        mean_size = weighted / total_chars if total_chars > 0 else 12.0

        blocks: list[Block] = []
        for blk in raw["blocks"]:
            if blk.get("type") != 0:
                continue
            if self._bbox_in_any(blk["bbox"], exclude):
                continue  # 表格 bbox 内文本：已随表格整表抽取，跳过防重复
            text = "".join(
                span.get("text", "") for line in blk.get("lines", []) for span in line.get("spans", [])
            ).strip()
            if not text:
                continue
            max_size = max(
                (span.get("size", 0.0) for line in blk.get("lines", []) for span in line.get("spans", [])),
                default=0.0,
            )
            if mean_size > 0 and max_size >= mean_size * 1.2 and len(text) <= 40:
                blocks.append(
                    Block(
                        type=BLOCK_TYPE_HEADING,
                        heading_level=self._heading_level(max_size / mean_size),
                        text=text,
                        meta={"page": page.number},
                    )
                )
            else:
                blocks.append(Block(type=BLOCK_TYPE_PARAGRAPH, text=text, meta={"page": page.number}))
        return blocks

    @staticmethod
    def _heading_level(ratio: float) -> int:
        """按字号/正文均值倍率映射标题层级 1~6。"""
        if ratio >= 2.0:
            return 1
        if ratio >= 1.6:
            return 2
        if ratio >= 1.35:
            return 3
        return 4

    @staticmethod
    def _bbox_in_any(
        bbox: tuple[float, float, float, float],
        tables: list[tuple[float, float, float, float]],
        overlap_ratio: float = 0.5,
    ) -> bool:
        """判断文本块是否落在任一表格 bbox 内（重叠面积/文本块面积 > overlap_ratio）。"""
        x0, y0, x1, y1 = bbox
        area = max((x1 - x0) * (y1 - y0), 1e-9)
        for tx0, ty0, tx1, ty1 in tables:
            ix = max(0.0, min(x1, tx1) - max(x0, tx0))
            iy = max(0.0, min(y1, ty1) - max(y0, ty0))
            if ix * iy / area > overlap_ratio:
                return True
        return False

    # ── ③ 跨页表格合并 ─────────────────────────────────────────────────────
    @staticmethod
    def _merge_cross_page_tables(blocks: list[Block]) -> list[Block]:
        """相邻页尾/页首 table 合并：列数一致且后表首行为重复表头 → 去重后并为一个 table 块。

        严格按「列数一致 + 表头重复」双条件合并，避免把同构的不同表格误并（§6.2③）。
        """
        # 记录每页最后一个 / 下一页第一个 table 块的下标
        page_last_table: dict[int, int] = {}
        page_first_table: dict[int, int] = {}
        for idx, blk in enumerate(blocks):
            if blk.type != "table":
                continue
            page = int(blk.meta.get("page", -1))
            page_last_table[page] = idx
            if page not in page_first_table:
                page_first_table[page] = idx

        removed: set[int] = set()
        for page, last_idx in sorted(page_last_table.items()):
            next_first = page_first_table.get(page + 1)
            if next_first is None or next_first == last_idx:
                continue
            cur, nxt = blocks[last_idx], blocks[next_first]
            cur_rows = cur.table or []
            nxt_rows = nxt.table or []
            if not cur_rows or not nxt_rows:
                continue
            # 列数一致（以各自行宽众数近似：直接比较首行宽度 + 至少 1 列）
            if len(cur_rows[0]) != len(nxt_rows[0]) or len(cur_rows[0]) < 1:
                continue
            # 后表首行为重复表头 → 去重后并入前表
            if cur_rows[0] != nxt_rows[0]:
                continue
            cur.table = cur_rows + nxt_rows[1:]
            cur.meta["pages"] = [page, page + 1]  # 溯源：合并覆盖的页码
            removed.add(next_first)
        return [blk for idx, blk in enumerate(blocks) if idx not in removed]

    # ── ④ OCR 降级 ─────────────────────────────────────────────────────────
    def _ocr_fallback(self, doc: Any) -> list[Block]:
        """整档 300dpi 渲染逐页走 OCR 引擎（版面后处理复用 ocr_parser.layout_postprocess）。

        OCR 引擎不可用时抛 UnsupportedFormatError（中文安装提示），
        由上游按 §6.6 转人工兜底队列，保证覆盖率 100% 闭环。
        """
        from .ocr_parser import default_ocr_engine, layout_postprocess

        engine = self.ocr_engine or default_ocr_engine()
        low_conf_threshold = float(self.cfg["parse.ocr.low_conf_threshold"])
        blocks: list[Block] = []
        with tempfile.TemporaryDirectory(prefix="kb_pdf_ocr_") as tmpdir:
            for page in doc:
                pix = page.get_pixmap(dpi=int(self.cfg["parse.pdf.ocr_dpi"]))
                image_path = f"{tmpdir}/page_{page.number}.png"
                pix.save(image_path)
                result = engine.run(image_path)
                if getattr(result, "table", None) is not None:
                    # PP-Structure 表格识别分支：整表出块
                    rows = [["" if cell is None else str(cell) for cell in row] for row in result.table.rows]
                    blocks.append(self.emit_table(rows, {"page": page.number, "table_conf": result.table.conf}))
                    continue
                for blk in layout_postprocess(list(getattr(result, "lines", [])), low_conf_threshold):
                    blk.meta.setdefault("page", page.number)
                    blocks.append(blk)
        return blocks
