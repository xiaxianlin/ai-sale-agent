# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""图片 OCR 解析器（FR-KB-010）：本地 PaddleOCR 推理 + 版面后处理。

数据完整性保障（§16.1）：低置信行不丢弃——保留文本并标 meta.low_conf=True；
全图低置信字符占比超阈值 → stats.needs_manual_review=True，转人工兜底复核，
防止静默丢失关键信息（实现方案 §6.4）。

引擎注入：OcrParser 接受任意实现 run(image_path) → OcrResult 的引擎
（默认为本地 PaddleOCR 引擎，NFR-SEC-06 数据不出域），便于测试与替换。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.4、§7
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .base import (
    BaseParser,
    Block,
    ParseError,
    UnsupportedFormatError,
    cfg_get,
)

# CJK 字符判定（中日韩统一表意文字 + 中文标点 + 全角符号），用于行内文本拼接时决定是否加空格
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
# 编号/项目符号起始模式：命中视为新段落起始，不并入上一段
_NUMBERING_RE = re.compile(
    r"^(?:[（(【\[]?\d+[）)】\]、.．]|[•·●○◆▪*※]|[一二三四五六七八九十]{1,3}[、.．])"
)


@dataclass
class OcrLine:
    """OCR 引擎单行识别结果契约（各引擎适配层须转换为本结构）。

    bbox 为像素坐标 (x0, y0, x1, y1)，行高由 y1-y0 推导，供版面后处理使用。
    """

    text: str
    conf: float                                  # 识别置信度 0~1
    bbox: tuple[float, float, float, float]


@dataclass
class OcrTable:
    """OCR 表格识别结果契约（PP-Structure 表格分支，整表输出）。"""

    rows: list[list[str]]
    conf: float


@dataclass
class OcrResult:
    """OCR 引擎 run() 返回契约：普通版面（lines）或表格（table）二选一。"""

    lines: list[OcrLine] = field(default_factory=list)
    table: OcrTable | None = None


class _PaddleOcrEngine:
    """默认本地 PaddleOCR 引擎封装（惰性初始化，首次 run 才加载模型防拖慢启动）。

    适配 PaddleOCR 经典 ocr(img, cls=True) 输出：[[bbox, (text, conf)], ...]。
    """

    needs_wav16k = False  # 与 ASR 引擎契约对齐的无关标记（OCR 无音频预处理）

    def __init__(self, cfg: Mapping[str, Any] | None = None) -> None:
        self._cfg = dict(cfg or {})
        self._ocr: Any = None

    def _ensure_model(self) -> Any:
        if self._ocr is None:
            from paddleocr import PaddleOCR  # 延迟导入：构造引擎不加载模型

            # det+rec+cls 方向矫正，本地推理（NFR-SEC-06 数据不出域）
            self._ocr = PaddleOCR(use_angle_cls=True, lang=self._cfg.get("parse.ocr.lang", "ch"), show_log=False)
        return self._ocr

    def run(self, image_path: str) -> OcrResult:
        """执行 det+rec，把 PaddleOCR 输出适配为 OcrResult 契约结构。"""
        ocr = self._ensure_model()
        try:
            raw = ocr.ocr(image_path, cls=True)
        except Exception as exc:  # noqa: BLE001 —— 引擎调用失败统一转 ParseError
            raise ParseError(f"PaddleOCR 推理失败：{exc}") from exc
        lines: list[OcrLine] = []
        page_items = raw[0] if raw else []
        for item in page_items or []:
            try:
                box, (text, conf) = item[0], item[1]
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                lines.append(OcrLine(text=str(text), conf=float(conf), bbox=(min(xs), min(ys), max(xs), max(ys))))
            except Exception:  # noqa: BLE001 —— 单行适配失败跳过，不拖垮整图
                continue
        return OcrResult(lines=lines)


def default_ocr_engine(cfg: Mapping[str, Any] | None = None) -> _PaddleOcrEngine:
    """构造默认本地 PaddleOCR 引擎；未安装 paddleocr 时抛带中文提示的不支持格式错误（import 守护）。"""
    try:
        import paddleocr  # noqa: F401 —— 仅探测可导入性
    except ImportError as exc:
        raise UnsupportedFormatError(
            "未安装 PaddleOCR（paddleocr），无法执行图片 OCR 解析；请安装依赖：pip install 'paddleocr>=2.7'"
        ) from exc
    return _PaddleOcrEngine(cfg)


def _join_texts(left: str, right: str) -> str:
    """行内/段内文本拼接：任一侧边界为 CJK 字符则直接相连，否则补一个空格。"""
    if not left:
        return right
    if not right:
        return left
    if _CJK_RE.search(left[-1]) or _CJK_RE.search(right[0]):
        return left + right
    return left + " " + right


@dataclass
class _LayoutRow:
    """版面后处理中间结构：由同一物理行的若干 OCR 行合并而成。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    height: float
    avg_conf: float
    low_conf: bool


def layout_postprocess(lines: list[OcrLine], low_conf_threshold: float = 0.80) -> list[Block]:
    """OCR 行结果 → blocks 的版面后处理（§6.4）：

    ① 同行合并：y 中心差 < 行高×0.6 → 按左坐标排序拼接为一行；
    ② 段落聚合：行距 < 前行高×1.8 且非编号/缩进起始 → 并入上一段；
    ③ 标题判定：行长≤30 且行高显著大于正文均值（×1.25）→ heading，层级按行高分档。

    所有块携带 meta.avg_conf；含低置信行的块标 meta.low_conf=True（保留不丢弃）。
    """
    if not lines:
        return []
    # ── ① 同行合并：先按 y 中心聚类为物理行，再按左坐标排序拼接（保证阅读顺序）──
    sorted_lines = sorted(lines, key=lambda l: ((l.bbox[1] + l.bbox[3]) / 2, l.bbox[0]))
    groups: list[list[OcrLine]] = []  # 每组 = 同一物理行的若干 OCR 行
    for line in sorted_lines:
        x0, y0, x1, y1 = line.bbox
        height = max(y1 - y0, 1.0)
        y_center = (y0 + y1) / 2
        if groups:
            last = groups[-1][-1]
            last_height = max(last.bbox[3] - last.bbox[1], 1.0)
            last_center = (last.bbox[1] + last.bbox[3]) / 2
            if abs(y_center - last_center) < max(height, last_height) * 0.6:
                groups[-1].append(line)  # y 中心差 < 行高×0.6 → 同一物理行
                continue
        groups.append([line])

    rows: list[_LayoutRow] = []
    for group in groups:
        # 组内按左坐标排序后拼接，保证从左到右的阅读顺序
        ordered = sorted(group, key=lambda l: l.bbox[0])
        text = ""
        for member in ordered:
            text = _join_texts(text, member.text)
        xs0 = [m.bbox[0] for m in ordered]
        ys0 = [m.bbox[1] for m in ordered]
        xs1 = [m.bbox[2] for m in ordered]
        ys1 = [m.bbox[3] for m in ordered]
        rows.append(
            _LayoutRow(
                text=text,
                x0=min(xs0), y0=min(ys0), x1=max(xs1), y1=max(ys1),
                height=max(max(ys1) - min(ys0), 1.0),
                avg_conf=round(sum(m.conf for m in ordered) / len(ordered), 4),
                low_conf=any(m.conf < low_conf_threshold for m in ordered),
            )
        )

    # ── 正文行高基准（标题分档用）──
    mean_height = sum(r.height for r in rows) / len(rows)

    # ── ②③ 段落聚合 + 标题判定 ──
    return _flush_rows(rows, mean_height)


def _flush_rows(rows: list[_LayoutRow], mean_height: float) -> list[Block]:
    """段落聚合与标题判定主循环（从 layout_postprocess 拆出，便于独立测试）。

    标题判定先行（③：行长≤30 且行高显著大于正文均值×1.25），标题行独立成块，
    不与相邻正文行聚合，避免标题文本被并入段落。
    """
    # 段落中间结构：文本缓存 + 首行引用 + 低置信标记
    paragraphs: list[dict[str, Any]] = []
    buf: list[_LayoutRow] = []   # 当前段落缓存
    buf_x0 = 0.0                 # 当前段落首行左坐标（缩进判定基准）

    def emit_buf() -> None:
        """把缓存行聚合为一个段落记录（携带平均置信度与低置信标记）。"""
        if not buf:
            return
        text = ""
        for r in buf:
            text = _join_texts(text, r.text)
        paragraphs.append(
            {
                "text": text,
                "first_row": buf[0],
                "is_heading": False,
                "avg_conf": round(sum(r.avg_conf for r in buf) / len(buf), 4),
                "low_conf": any(r.low_conf for r in buf),
            }
        )
        buf.clear()

    for row in rows:
        # ③ 标题判定：行长≤30 且行高显著大于正文均值 → 独立成标题块
        if len(row.text) <= 30 and row.height > mean_height * 1.25:
            emit_buf()  # 标题前收尾当前段落
            paragraphs.append(
                {
                    "text": row.text,
                    "first_row": row,
                    "is_heading": True,
                    "avg_conf": row.avg_conf,
                    "low_conf": row.low_conf,
                }
            )
            continue
        new_paragraph = True
        if buf:
            prev = buf[-1]
            v_gap = row.y0 - prev.y1
            numbering_start = bool(_NUMBERING_RE.match(row.text))
            indented = (row.x0 - buf_x0) > row.height * 1.5  # 明显缩进起始 → 新段
            # 行距 < 前行高×1.8 且非编号/缩进起始 → 并入上一段
            new_paragraph = v_gap >= prev.height * 1.8 or numbering_start or indented
        if new_paragraph and buf:
            emit_buf()
        if not buf:
            buf_x0 = row.x0
        buf.append(row)
    emit_buf()  # 收尾：最后一段

    # 段落记录 → Block（标题按行高倍率分档层级）
    blocks: list[Block] = []
    for para in paragraphs:
        first_row: _LayoutRow = para["first_row"]
        blocks.append(
            Block(
                type="heading" if para["is_heading"] else "paragraph",
                heading_level=(
                    _heading_level_by_height(first_row.height / mean_height) if para["is_heading"] else 0
                ),
                text=para["text"],
                meta={"avg_conf": para["avg_conf"], "low_conf": para["low_conf"]},
            )
        )
    return blocks


def _heading_level_by_height(ratio: float) -> int:
    """按行高/正文均值倍率映射标题层级 1~6。"""
    if ratio >= 2.0:
        return 1
    if ratio >= 1.6:
        return 2
    if ratio >= 1.4:
        return 3
    return 4


class OcrParser(BaseParser):
    """图片 OCR 解析器：本地 PaddleOCR 引擎 + 版面后处理；引擎可注入替换。"""

    parser_name = "ocr_parser"
    fmt = "image_ocr"

    def __init__(self, ocr_engine: Any = None, cfg: Mapping[str, Any] | None = None) -> None:
        # 引擎为 None 时延迟取默认引擎（首次 _do_parse 才触发 import 守护，便于测试注入替身）
        self._injected_engine = ocr_engine
        self.cfg: dict[str, Any] = {
            "parse.ocr.low_conf_threshold": 0.80,   # §7：低置信行阈值（保留标记不丢弃）
            "parse.ocr.manual_review_ratio": 0.15,  # §7：低置信字符占比超限 → 人工复核
        }
        if cfg:
            self.cfg.update(cfg)

    @property
    def ocr(self) -> Any:
        """OCR 引擎惰性获取：注入引擎优先，否则默认本地 PaddleOCR（import 守护）。"""
        if self._injected_engine is not None:
            return self._injected_engine
        return default_ocr_engine(self.cfg)

    def _do_parse(self, path: str) -> list[Block]:
        engine = self.ocr
        result = engine.run(path)  # det+rec+cls 方向矫正，本地推理，数据不出域
        low_conf_threshold = float(self.cfg["parse.ocr.low_conf_threshold"])
        if getattr(result, "table", None) is not None:
            # PP-Structure 表格识别分支：整表出块（不拆分，FR-KB-012 契约）
            table = result.table
            rows = [["" if cell is None else str(cell) for cell in row] for row in table.rows]
            blocks = [self.emit_table(rows, {"table_conf": table.conf})]
            if float(table.conf) < low_conf_threshold:
                self._set_stats(needs_manual_review=True)  # 整表低置信 → 人工复核
            return blocks
        lines = list(getattr(result, "lines", []))
        blocks = layout_postprocess(lines, low_conf_threshold)
        # 完整性保障：低置信(conf<阈值)行不丢弃——已保留文本并标 meta.low_conf=True；
        # 全图低置信字符占比 > 阈值 → stats.needs_manual_review=True（转人工兜底复核，防静默丢失）
        low_chars = sum(len(l.text) for l in lines if l.conf < low_conf_threshold)
        total_chars = sum(len(l.text) for l in lines)
        if total_chars > 0 and low_chars / total_chars > float(self.cfg["parse.ocr.manual_review_ratio"]):
            self._set_stats(needs_manual_review=True)
        return blocks
