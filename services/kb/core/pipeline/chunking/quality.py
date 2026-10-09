# 知识产权归甲方所有（COM-IP-01）
"""分块质量自检（FR-KB-012 / SR-KB-012 §6.5）：账目对账 + 分布指标。

账目对账是 SRS §16.1「数据完整性——无关键信息丢失」的确定性保障：
    ① block 账目：全部 chunk.block_spans 不重不漏覆盖上游全部 block
       （同一 block 允许被多个行组块按行区间平铺覆盖，但区间不得交叠）；
    ② 文本账目：全部块文本按序拼接 ≈ 原文重建（空白不敏感）——
       heading 归属其后首块、表格按行组含重复表头，与分块器构造约定一一对应。
任何缺失/重复/文本失真都会使 lossless_blocks=False，编排层据此阻断入库并
转人工兜底（CHUNK-2001，SR-KB-012 §5/§6.6）。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from .boundary import rows_to_markdown

if TYPE_CHECKING:  # 仅为类型注解服务，运行时零依赖（避免与 chunker 循环导入）
    from .chunker import Chunk, ChunkConfig

_WS_RE = re.compile(r"\s+")


def _squeeze(text: str) -> str:
    """压缩全部空白后比较：对连接符（空行/空格/换行）差异鲁棒，只比内容字符。"""
    return _WS_RE.sub("", text)


def reconcile_blocks(chunks: list["Chunk"], n_blocks: int) -> dict:
    """① block 账目对账：覆盖不重不漏。

    判定规则：
        - 每个 block 序号至少被一个 span 覆盖（缺失 → missing_blocks）；
        - 不出现越界序号（unknown_blocks）；
        - 同一 block 上的行区间 [start, end) 两两不得交叠（交叠即内容被
          重复计入多个块 → duplicated_blocks；行组平铺覆盖是合法的）。
    """
    intervals: dict[int, list[tuple[int, int]]] = {}
    for chunk in chunks:
        for span in chunk.block_spans:
            idx = int(span.get("idx", -1))
            start = int(span.get("start", 0))
            end = int(span.get("end", start))
            intervals.setdefault(idx, []).append((start, end))
    missing = [i for i in range(n_blocks) if i not in intervals]
    unknown = sorted(i for i in intervals if i < 0 or i >= n_blocks)
    duplicated: set[int] = set()
    for idx, ivs in intervals.items():
        if idx in unknown:
            continue
        ivs_sorted = sorted(ivs)
        for (_, e1), (s2, _) in zip(ivs_sorted, ivs_sorted[1:]):
            if s2 < e1:  # 行区间交叠 → 同一 block 内容被多块重复覆盖
                duplicated.add(idx)
                break
    ok = not missing and not unknown and not duplicated
    return {
        "ok": ok,
        "missing_blocks": missing,
        "duplicated_blocks": sorted(duplicated),
        "unknown_blocks": unknown,
        "covered_blocks": len(intervals),
    }


def expected_doc_text(doc: Any, chunks: list["Chunk"]) -> str:
    """按 block 顺序重建期望文本（与分块器构造约定一一对应）。

    约定（SR-KB-012 §6.3，quality 与 chunker 必须同步维护）：
        - heading / 文本 block → 原文 text（标题文本归属其后首个块，只出现一次）；
        - table block → 整表 Markdown；被行组切分时按 span 行区间重建为
          Σ(表头 + 行组)，重复表头计入（与 TableBoundary.plan 构造一致）。
    """
    blocks = list(getattr(doc, "blocks", []) or [])
    # 收集每个 table block 的覆盖行区间（来自 span，行组切分时同 idx 多区间）
    table_spans: dict[int, list[tuple[int, int]]] = {}
    for chunk in chunks:
        for span in chunk.block_spans:
            idx = int(span.get("idx", -1))
            if 0 <= idx < len(blocks) and str(getattr(blocks[idx], "type", "") or "") == "table":
                table_spans.setdefault(idx, []).append(
                    (int(span.get("start", 0)), int(span.get("end", 0)))
                )
    parts: list[str] = []
    for idx, block in enumerate(blocks):
        if str(getattr(block, "type", "") or "") == "table":
            rows = [[str(cell) for cell in row] for row in (getattr(block, "table", None) or [])]
            spans = sorted(table_spans.get(idx, []))
            if len(spans) <= 1:
                parts.append(rows_to_markdown(rows))
            elif rows:
                data = rows[1:]
                for start, end in spans:  # 每个行组：重复表头 + 对应数据行
                    parts.append(rows_to_markdown([rows[0], *data[start:end]]))
        else:
            parts.append(str(getattr(block, "text", "") or ""))
    return "".join(parts)


def reconcile_text(doc: Any, chunks: list["Chunk"]) -> dict:
    """② 文本账目对账：块文本按序拼接 ≈ 原文重建（空白不敏感）。"""
    actual = _squeeze("".join(chunk.content for chunk in chunks))
    expected = _squeeze(expected_doc_text(doc, chunks))
    return {"ok": actual == expected, "expected_chars": len(expected), "actual_chars": len(actual)}


def reconcile(doc: Any, chunks: list["Chunk"]) -> dict:
    """账目对账总入口：block 不重不漏 + 文本拼接一致，任一失守即 ok=False。"""
    n_blocks = len(list(getattr(doc, "blocks", []) or []))
    blocks_report = reconcile_blocks(chunks, n_blocks)
    text_report = reconcile_text(doc, chunks)
    ok = blocks_report["ok"] and text_report["ok"]
    return {"ok": ok, "blocks": blocks_report, "text": text_report}


def _percentile(sorted_values: list[int], p: float) -> int:
    """线性插值分位数（p ∈ [0,1]），输入须已升序，返回整数 token 数。"""
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return int(sorted_values[0])
    rank = (len(sorted_values) - 1) * p
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return int(sorted_values[lo])
    return int(round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (rank - lo)))


class ChunkQuality:
    """分块质量自检（SR-KB-012 §6.5 六项指标 + 工程告警扩展）。

    指标口径：
        lossless_blocks        ① 账目对账（block 不重不漏 + 文本拼接≈原文）
        table_integrity        ② 整块保护的表格块数 == 上游 table block 总数
        token_dist             ③ token 分布 min/p50/p95/max + 区间外占比
        boundary_mix / hard_limit_alert  ④ 边界构成，hard_limit 占比过高判定语义检测失效
        orphan_rate            ⑤ 合并后仍 < min_tokens 的文本块占比（表格块按设计独立，不计孤儿）
        heading_path_coverage  ⑥ 每块必含 heading_path（无标题文档为显式空串），覆盖率恒 1.0
    """

    def __init__(self, config: "ChunkConfig | None" = None) -> None:
        self.cfg = config

    def assess(self, doc: Any, chunks: list["Chunk"]) -> dict:
        """评估并返回指标字典（编排层写入 ingest_pipeline_log.action_detail.metrics）。"""
        report = reconcile(doc, chunks)
        tokens = sorted(chunk.tokens for chunk in chunks)
        min_tokens = getattr(self.cfg, "min_tokens", None)
        max_tokens = getattr(self.cfg, "max_tokens", None)
        if tokens:
            out_of_range = sum(
                1 for t in tokens
                if (min_tokens is not None and t < min_tokens) or (max_tokens is not None and t > max_tokens)
            ) / len(tokens)
        else:
            out_of_range = 0.0
        mix: Counter[str] = Counter(reason for chunk in chunks for reason in chunk.boundary_reasons)
        total_reasons = sum(mix.values())
        hard_ratio = (mix.get("hard_limit", 0) / total_reasons) if total_reasons else 0.0
        # 孤儿块：仅文本块参与（表格块按设计独立成块，不计孤儿）；合并阶段已标 orphan，
        # 独立使用 quality 时按 tokens < min 兜底判定
        text_chunks = [chunk for chunk in chunks if not chunk.is_table]
        orphans = [
            chunk for chunk in text_chunks
            if chunk.context_meta.get("orphan")
            or (min_tokens is not None and chunk.tokens < min_tokens)
        ]
        hard_alert_ratio = getattr(self.cfg, "hard_limit_alert_ratio", None)
        range_alert_ratio = getattr(self.cfg, "out_of_range_alert_ratio", None)
        covered = sum(1 for chunk in chunks if isinstance(chunk.heading_path, str))
        return {
            "lossless_blocks": report["ok"],
            "block_ledger_ok": report["blocks"]["ok"],
            "text_ledger_ok": report["text"]["ok"],
            "table_integrity": self._table_integrity(doc, chunks),
            "token_dist": {
                "min": tokens[0] if tokens else 0,
                "p50": _percentile(tokens, 0.50),
                "p95": _percentile(tokens, 0.95),
                "max": tokens[-1] if tokens else 0,
                "out_of_range_rate": round(out_of_range, 4),
            },
            "boundary_mix": dict(mix),
            "hard_limit_ratio": round(hard_ratio, 4),
            "hard_limit_alert": bool(hard_alert_ratio is not None and hard_ratio > hard_alert_ratio),
            "out_of_range_alert": bool(range_alert_ratio is not None and out_of_range > range_alert_ratio),
            "orphan_rate": round(len(orphans) / len(text_chunks), 4) if text_chunks else 0.0,
            "heading_path_coverage": round(covered / len(chunks), 4) if chunks else 1.0,
        }

    @staticmethod
    def _table_integrity(doc: Any, chunks: list["Chunk"]) -> bool:
        """② 表格完整性：被整块保护（含行组块）覆盖的 table block 数 == 上游 table 总数。"""
        blocks = list(getattr(doc, "blocks", []) or [])
        want = {i for i, b in enumerate(blocks) if str(getattr(b, "type", "") or "") == "table"}
        got: set[int] = set()
        for chunk in chunks:
            if chunk.is_table:  # boundary_reasons 含 table_whole（含行组块）
                for span in chunk.block_spans:
                    idx = int(span.get("idx", -1))
                    if idx in want:
                        got.add(idx)
        return want == got
