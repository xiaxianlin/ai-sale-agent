# 知识产权归甲方所有（COM-IP-01）
"""语义分块主循环（FR-KB-012 / SR-KB-012 §6.3）：四级边界决策 + 聚合缓冲。

四级边界优先级（05-核心模块详细设计 §2.2，逐级生效，注册顺序即优先级）：
    ① 标题层级（HeadingBoundary）> ② 表格整体（TableBoundary）
    > ③ 段落语义（SimilarityBoundary，可插拔，默认 None）
    > ④ 长度上限兜底（LengthFallback）

主循环职责：
    - 逐 block 遍历，先问块级边界（①②），未命中走句级聚合（③④）；
    - 维护标题栈（heading_path）与「待归属标题」（标题文本只进其后首个块）；
    - 表格整块保护：独立出块，超保护上限受控按行组切分并 degrade 留痕；
    - 过短块合并（仅文本块、同 heading_path 相邻块，表格块不参与）；
    - 定稿回填（chunk_index/uid/相邻指针/char_span，见 metadata.finalize_chunks）。

输入契约：解析/清洗产物的中间格式 UnstructuredDoc（FR-KB-010 §6.1 block
契约）。该契约由 services/kb/core/pipeline/parsers/base.py 定义（解析器模块
并行建设中），本模块不做硬导入，而是通过 UnstructuredDocLike / BlockLike
本地协议类鸭子类型解耦——任何具备 blocks / source_file 等属性的对象均可
直接输入，由解析器模块对齐。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .boundary import (
    REASON_HEADING,
    REASON_HARD_LIMIT,
    REASON_MERGE,
    REASON_TABLE_SPLIT,
    BoundaryContext,
    HeadingBoundary,
    LengthFallback,
    SimilarityBoundary,
    TableBoundary,
)
from .metadata import HeadingStack, finalize_chunks
from .quality import ChunkQuality


class ChunkInputError(ValueError):
    """CHUNK-1001：输入 blocks 为空或非法（SR-KB-012 §5 错误码）。"""


# ---- 上游契约（鸭子类型协议，对齐 parsers/base.py，由解析器模块保证一致）------


class BlockLike(Protocol):
    """上游 block 结构契约（FR-KB-010 §6.1）。"""

    type: str  # 'heading' | 'table' | 其他文本类（paragraph / list_item / ...）
    text: str  # 文本内容（heading 为标题原文；table 为空或概述文本）
    # 可选属性（鸭子类型访问，缺省容忍）：
    #   heading_level: int —— heading 专有，缺省按 1 级处理
    #   table: Sequence[Sequence[str]] —— table 专有，首行为表头行


class UnstructuredDocLike(Protocol):
    """解析器中间格式 UnstructuredDoc 结构契约（清洗产物，FR-KB-011 输出）。"""

    blocks: Sequence[BlockLike]
    source_file: str
    # 可选属性：source_format: str；title: str


# ---- 基础工具：token 计量与切句（SR-KB-012 §6.1）-----------------------------


def default_token_counter(text: str) -> int:
    """启发式 token 计量：BGE-M3 tokenizer 的本地近似（向量服务接入后可替换精确分词器）。

    口径：汉字约 1 token/字；其余非空白字符约 4 字符折 1 token（近似英文词片）。
    确定性纯函数，保证测试与幂等重放（FR-KB-017）结果一致。
    """
    han = 0
    other = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            han += 1
        elif not ch.isspace():
            other += 1
    return han + (other + 3) // 4


# 非定界符* + 句读定界符*：全字符覆盖（拼接还原无损），定界符含中英文句读与换行
_SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]*")


def split_sentences(text: str, min_chars: int = 6) -> list[str]:
    """段落切句：按中英文句读符切分；过短句（< min_chars）并入前句，不独立成句。

    切句结果按序拼接 == 原文（空白除外），是文本账目对账（quality.reconcile_text）
    成立的基础；纯定界符文本退化为单句整段处理，保证零字符丢失。
    """
    fragments: list[str] = []
    for match in _SENTENCE_SPLIT_RE.findall(text):
        fragment = match.strip()
        if not fragment:
            continue
        if fragments and len(fragment) < min_chars:
            fragments[-1] += fragment  # 过短句并入前句（SR-KB-012 §6.1 sent_min_chars）
        else:
            fragments.append(fragment)
    if len(fragments) >= 2 and len(fragments[0]) < min_chars:
        fragments[1] = fragments[0] + fragments[1]  # 首句过短并入后句
        del fragments[0]
    return fragments


# ---- 数据结构（SR-KB-012 §6.1，对应 03-数据库设计 §3.1 knowledge_chunk）------


@dataclass
class ChunkConfig:
    """分块参数：全部外置可调（NFR-MAI-02；默认值与取值范围见 SR-KB-012 §7 配置项表）。"""

    min_tokens: int = 300        # 单块目标下限（token），取值 150~500，与 max 保持 ≥100 间距
    max_tokens: int = 800        # 单块目标上限（token），取值 500~2000
    table_hard_cap: int = 1600   # 表格整块保护上限，超过走受控行组切分（§2 偏差标注）
    sim_drop: float = 0.15       # 相邻句余弦骤降阈值（相对前序窗口均值的落差）
    sim_window: int = 3          # 骤降参照窗口（句数）
    sent_min_chars: int = 6      # 过短句并入邻句阈值（字符）
    hard_limit_alert_ratio: float = 0.30  # hard_limit 边界占比告警线（语义检测失效信号）
    out_of_range_alert_ratio: float = 0.05  # token 区间外块占比告警线
    token_counter: Callable[[str], int] = default_token_counter  # token 计量函数（可替换）


@dataclass
class Chunk:
    """分块产物（knowledge_chunk 核心字段的内存形态）。"""

    chunk_index: int = -1                       # item 内输出顺序，定稿阶段从 0 统一赋值
    uid: str = ""                               # 块 uid（c-xxxxxxxx，定稿阶段生成）
    content: str = ""                           # 块内容（标题前缀 + 句子 / 整表 Markdown）
    heading_path: str = ""                      # 标题层级路径（标题栈构造，§6.4）
    tokens: int = 0                             # 块 token 数（含归属标题）
    block_spans: list[dict] = field(default_factory=list)   # 覆盖 block 区间 → 账目对账/回溯
    boundary_reasons: list[str] = field(default_factory=list)  # 切点依据审计
    context_meta: dict = field(default_factory=dict)         # 上下文元数据（§4 契约）

    @property
    def is_table(self) -> bool:
        """表格块标记：整表/行组块不参与合并，保持独立成块（SR-KB-012 §6.3）。"""
        return "table_whole" in self.boundary_reasons


@dataclass
class BufferedSentence:
    """缓冲句单元：携带所属 block、块内句序与语义骤降标记（句级边界判定输入）。"""

    block_index: int
    text: str
    tokens: int
    sent_index: int = 0              # 块内句序号（0 起）：block_spans 句区间构造依据
    drop_before: bool = False        # 其前存在句向量骤降点（候选切点，③ 级产物）


class _Buffer:
    """聚合缓冲（句粒度）：待归属标题 + 已并入句 + 空文本 block 的账目记录。"""

    def __init__(self) -> None:
        self.headings: list[tuple[int, str]] = []       # 待归属标题 (block 序号, 标题原文)
        self.sentences: list[BufferedSentence] = []     # 已并入句
        self.empty_blocks: list[int] = []               # 空文本 block 序号（仍需账目覆盖）

    @property
    def tokens(self) -> int:
        return sum(sentence.tokens for sentence in self.sentences)

    def clear(self) -> None:
        self.headings = []
        self.sentences = []
        self.empty_blocks = []


def _dedup_reasons(reasons: list[str]) -> list[str]:
    """边界依据去重（保序）：merge 合并时避免重复审计值。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return ordered


class SemanticChunker:
    """语义分块器：主循环 + 边界决策（四级优先级逐级生效，SR-KB-012 §6.3）。

    参数：
        config：ChunkConfig（None 取默认值）；
        similarity_detector：③ 级语义边界检测器（可插拔，默认 None）。向量服务
            （本地 BGE-M3/TEI）接入后注入 SimilarityBoundary(embed_fn=...) 即启用
            完整四级；未注入时按「标题 + 表格 + 长度」三级边界运行；
        token_counter：token 计量函数（缺省用 config.token_counter）。
    """

    def __init__(
        self,
        config: ChunkConfig | None = None,
        similarity_detector: SimilarityBoundary | None = None,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self.cfg = config or ChunkConfig()
        self.count_tokens = token_counter or self.cfg.token_counter
        # 块级边界检测器（①>②，注册顺序即优先级）
        self.heading_boundary = HeadingBoundary()
        self.table_boundary = TableBoundary(token_counter=self.count_tokens)
        # 句级边界检测器（③>④；③ 可插拔，默认 None）
        self.similarity_boundary = similarity_detector
        self.length_fallback = LengthFallback()
        # 降级留痕（degrade 列表，SR-KB-012 §2/§4：绝不静默发生）
        self._degrade: list[str] = []
        # 主循环状态（每次 chunk() 调用重置）
        self.heading_stack = HeadingStack()
        self._chunks: list[Chunk] = []
        self._next_start_reason: str | None = None  # 下一块的起始切点依据（审计用）

    # ---- 对外入口 -----------------------------------------------------------

    def degrade_to_struct_only(self) -> None:
        """BGE-M3 不可用降级（重试耗尽后由编排层调用，SR-KB-012 §2）：
        跳过句向量骤降，按「标题 + 表格 + 长度」三级边界继续出块并留痕。"""
        self.similarity_boundary = None
        if "bge_unavailable->struct_only" not in self._degrade:
            self._degrade.append("bge_unavailable->struct_only")

    def chunk(self, doc: UnstructuredDocLike) -> tuple[list[Chunk], dict]:
        """分块主入口：返回 (chunks, metrics)；metrics 含账目对账等质量自检结果。"""
        blocks = self._validate(doc)
        self.heading_stack = HeadingStack()
        self._chunks = []
        self._next_start_reason = None
        buffer = _Buffer()
        for index, block in enumerate(blocks):
            context = BoundaryContext(
                block=block,
                block_index=index,
                sentences=tuple(buffer.sentences),
                buffer_tokens=buffer.tokens,
                config=self.cfg,
            )
            hit = self._detect_block_boundary(context)  # 块级边界：① 标题 > ② 表格
            if hit is None:
                self._process_paragraph(index, block, buffer)  # ③④ 句级聚合
            elif hit.reason == REASON_HEADING:
                self._flush(buffer)  # 先冲刷（沿用旧标题栈），再维护层级栈
                level = self._heading_level(block)
                text = self._block_text(block)
                self.heading_stack.enter(level, text)
                buffer.headings.append((index, text))  # 标题归属其后首个块
                self._next_start_reason = REASON_HEADING
            else:  # hit.reason == table_whole
                self._flush(buffer)
                self._emit_table(index, block, buffer)
        self._flush(buffer, force=True)  # 文档末尾冲刷：含孤悬标题的强制归属
        chunks = self._merge_oversmall(self._chunks)
        finalize_chunks(chunks, doc, item_key=str(getattr(doc, "source_file", "") or ""))
        metrics = ChunkQuality(config=self.cfg).assess(doc, chunks)
        metrics["blocks"] = len(blocks)
        metrics["chunks"] = len(chunks)
        metrics["degrade"] = list(self._degrade)
        return chunks, metrics

    # ---- 块级边界 -----------------------------------------------------------

    def _detect_block_boundary(self, context: BoundaryContext):
        """块级边界判定：按优先级 ① 标题 > ② 表格 依次询问，首个命中者生效。"""
        for detector in (self.heading_boundary, self.table_boundary):
            hit = detector.detect(context)
            if hit is not None:
                return hit
        return None

    # ---- ③④ 句级聚合（段落）--------------------------------------------------

    def _process_paragraph(self, index: int, block: Any, buffer: _Buffer) -> None:
        """文本 block：切句后逐句并入；超上限时先语义回溯（③）、再长度兜底（④）。"""
        text = self._block_text(block)
        if not text.strip():
            buffer.empty_blocks.append(index)  # 空文本 block：不产句，但仍需账目覆盖
            return
        sentences = split_sentences(text, self.cfg.sent_min_chars)
        if not sentences:
            buffer.empty_blocks.append(index)
            return
        flags = self._sentence_flags(sentences)
        for j, sentence in enumerate(sentences):
            drop = bool(flags[j - 1]) if j >= 1 else False  # 上一句的骤降标记 → 本句前为候选切点
            tokens = self.count_tokens(sentence)
            if buffer.tokens + tokens <= self.cfg.max_tokens:
                buffer.sentences.append(BufferedSentence(index, sentence, tokens, j, drop))
                continue
            # 超上限 → 切分：③ 语义回溯优先，④ 长度兜底（注册顺序即优先级）
            incoming = BufferedSentence(index, sentence, tokens, j, drop)
            context = BoundaryContext(
                block=block,
                block_index=index,
                sentences=tuple(buffer.sentences) + (incoming,),  # 含待并入句
                buffer_tokens=buffer.tokens + tokens,
                config=self.cfg,
            )
            hit = None
            for detector in self._sentence_detectors():
                hit = detector.detect(context)
                if hit is not None:
                    break
            cut = hit.detail.get("cut") if hit is not None else None
            if not isinstance(cut, int) or cut < 1:
                cut = max(len(buffer.sentences), 1)  # 安全网：整缓冲出块，保证推进
            units = [*buffer.sentences, incoming]
            prefix, rest = units[:cut], units[cut:]
            # 前缀出块（消费待归属标题与空块账目），剩余句 + 当前句留缓冲续聚
            self._emit_buffer(prefix, buffer.headings, buffer.empty_blocks)
            buffer.headings = []
            buffer.empty_blocks = []
            buffer.sentences = list(rest)
            # 切点依据归属切点之后的块（下一块因此切点而生）
            self._next_start_reason = hit.reason if hit is not None else REASON_HARD_LIMIT

    def _sentence_detectors(self) -> list[Any]:
        """句级检测器序列：③ 可插拔（未注入即跳过）> ④ 兜底。"""
        detectors: list[Any] = []
        if self.similarity_boundary is not None:
            detectors.append(self.similarity_boundary)
        detectors.append(self.length_fallback)
        return detectors

    def _sentence_flags(self, sentences: list[str]) -> list[bool]:
        """句向量骤降标记（③ 级）：未接入检测器时恒无骤降点。"""
        if self.similarity_boundary is None:
            return [False] * len(sentences)
        return self.similarity_boundary.flag_drops(sentences, self.cfg)

    # ---- 出块 ---------------------------------------------------------------

    def _flush(self, buffer: _Buffer, force: bool = False) -> None:
        """冲刷缓冲出块（标题/表格边界、文档末尾触发）。

        仅当缓冲有实际正文（句或空文本 block）时出块；待归属标题不单独触发
        出块——连续标题链上的标题逐级累积，归属其后首个真正含内容的块
        （标题后紧跟标题/表格时不产生「裸标题块」）。force=True（文档末尾
        冲刷）时，残留的孤悬标题才独立出块以保证账目覆盖。
        """
        has_body = bool(buffer.sentences) or bool(buffer.empty_blocks)
        if not has_body and not (force and buffer.headings):
            return
        self._emit_buffer(buffer.sentences, buffer.headings, buffer.empty_blocks)
        buffer.clear()

    def _emit_buffer(
        self,
        units: list[BufferedSentence],
        headings: list[tuple[int, str]],
        empty_blocks: list[int],
    ) -> None:
        """以缓冲内容出块：待归属标题前缀 + 句子正文；账目 span 全量携带。"""
        if not units and not headings and not empty_blocks:
            return
        heading_texts = [text for _, text in headings]
        body = "".join(unit.text for unit in units)
        parts = [*heading_texts]
        if body:
            parts.append(body)
        content = "\n\n".join(parts)
        spans = self._build_spans(units, headings, empty_blocks)
        reasons = [self._next_start_reason] if self._next_start_reason else []
        self._next_start_reason = None
        # token 计量含归属标题（块内容口径一致，token_dist 指标准确）
        tokens = sum(unit.tokens for unit in units) + sum(
            self.count_tokens(text) for text in heading_texts
        )
        self._append(content=content, spans=spans, reasons=reasons, tokens=tokens)

    @staticmethod
    def _build_spans(
        units: list[BufferedSentence],
        headings: list[tuple[int, str]],
        empty_blocks: list[int],
    ) -> list[dict]:
        """构造 block_spans：标题/空块为整块 [0,1)；文本块按块内句区间
        [首句序, 末句序+1) 记录——同一长段落被切分到多个块时区间平铺，
        账目对账（quality.reconcile_blocks）据此判定不重不漏。"""
        spans: list[dict] = [{"idx": i, "start": 0, "end": 1} for i, _ in headings]
        ranges: dict[int, tuple[int, int]] = {}
        for unit in units:
            lo, hi = ranges.get(unit.block_index, (unit.sent_index, unit.sent_index + 1))
            ranges[unit.block_index] = (
                min(lo, unit.sent_index),
                max(hi, unit.sent_index + 1),
            )
        spans.extend({"idx": idx, "start": lo, "end": hi} for idx, (lo, hi) in ranges.items())
        spans.extend({"idx": idx, "start": 0, "end": 1} for idx in empty_blocks)
        spans.sort(key=lambda span: int(span["idx"]))
        return spans

    def _emit_table(self, index: int, block: Any, buffer: _Buffer) -> None:
        """表格整块保护出块：默认整表一块；超保护上限受控按行组切分（§2 偏差标注）。

        待归属标题（若有）随第一个表格块出块；行组切分发生时写
        degrade=['table_row_group_split'] 留痕并告警人工复核，绝不静默。
        """
        pieces = self.table_boundary.plan(index, block, self.cfg)
        if any(piece.split for piece in pieces) and REASON_TABLE_SPLIT not in self._degrade:
            self._degrade.append(REASON_TABLE_SPLIT)
        for pi, piece in enumerate(pieces):
            content = piece.content
            spans = [dict(span) for span in piece.spans]
            reasons = list(piece.reasons)
            if pi == 0:
                if buffer.headings:  # 标题归属其后首个块——此处即表格块
                    heading_texts = [text for _, text in buffer.headings]
                    parts = [*heading_texts, content] if content else heading_texts
                    content = "\n\n".join(parts)
                    heading_spans = [{"idx": i, "start": 0, "end": 1} for i, _ in buffer.headings]
                    spans = sorted(heading_spans + spans, key=lambda span: int(span["idx"]))
                    reasons = _dedup_reasons(
                        ([self._next_start_reason] if self._next_start_reason else []) + reasons
                    )
                    buffer.headings = []
                elif self._next_start_reason:
                    reasons = [self._next_start_reason] + reasons
            self._next_start_reason = None  # 首块消费切点依据；行组续块以行组切分为界
            self._append(
                content=content,
                spans=spans,
                reasons=reasons,
                tokens=self.count_tokens(content),
                extra_meta={"table_summary": piece.summary},
            )

    def _append(
        self,
        content: str,
        spans: list[dict],
        reasons: list[str],
        tokens: int,
        extra_meta: dict | None = None,
    ) -> None:
        """构造并追加 Chunk（heading_path 取出块时刻标题栈快照）。"""
        self._chunks.append(Chunk(
            content=content,
            heading_path=self.heading_stack.path(),
            tokens=tokens,
            block_spans=spans,
            boundary_reasons=_dedup_reasons(reasons),
            context_meta=dict(extra_meta or {}),
        ))

    # ---- 后处理：过短块合并（SR-KB-012 §6.3）--------------------------------

    def _merge_oversmall(self, chunks: list[Chunk]) -> list[Chunk]:
        """过短块（< min_tokens）合并：仅文本块参与；优先并入 heading_path 相同的
        相邻块（前邻优先，保持小节尾部语义连续）；表格块不参与合并（独立成块）；
        无同路径邻居可并 → 保留并标 orphan，交质量报告暴露（§6.5 ⑤）。"""
        result = list(chunks)
        changed = True
        while changed and len(result) > 1:  # 每轮至少消减一块，必然收敛
            changed = False
            for i, current in enumerate(result):
                if current.is_table or current.tokens >= self.cfg.min_tokens:
                    continue
                previous = result[i - 1] if i > 0 else None
                following = result[i + 1] if i + 1 < len(result) else None
                if previous is not None and self._mergeable(previous, current):
                    self._merge_into(previous, current)  # 并入前邻
                    del result[i]
                    changed = True
                    break
                if following is not None and self._mergeable(current, following):
                    self._merge_front(current, following)  # 并入后邻（成为后邻前缀）
                    del result[i]
                    changed = True
                    break
        for chunk in result:
            if not chunk.is_table and chunk.tokens < self.cfg.min_tokens:
                chunk.context_meta["orphan"] = True  # 孤儿块：保留但显式暴露
        return result

    @staticmethod
    def _mergeable(a: Chunk, b: Chunk) -> bool:
        """可合并判定：双方均为文本块且 heading_path 相同（跨小节不合并）。"""
        return (not a.is_table) and (not b.is_table) and a.heading_path == b.heading_path

    @staticmethod
    def _union_spans(a: Chunk, b: Chunk) -> list[dict]:
        """合并双方 block_spans（按 idx 排序、同键去重），保持账目不重不漏。"""
        merged = {(int(s["idx"]), int(s.get("start", 0)), int(s.get("end", 1)))
                  for s in [*a.block_spans, *b.block_spans]}
        return [{"idx": idx, "start": start, "end": end} for idx, start, end in sorted(merged)]

    @staticmethod
    def _merge_into(target: Chunk, source: Chunk) -> None:
        """source 并入 target 之后（target 在前）。"""
        target.content = target.content + "\n\n" + source.content
        target.tokens += source.tokens
        target.block_spans = SemanticChunker._union_spans(target, source)
        target.boundary_reasons = _dedup_reasons(
            [*target.boundary_reasons, *source.boundary_reasons, REASON_MERGE]
        )

    @staticmethod
    def _merge_front(source: Chunk, target: Chunk) -> None:
        """source 并入 target 之前（target 在后，source 成为其前缀）。"""
        target.content = source.content + "\n\n" + target.content
        target.tokens += source.tokens
        target.block_spans = SemanticChunker._union_spans(source, target)
        target.boundary_reasons = _dedup_reasons(
            [*source.boundary_reasons, *target.boundary_reasons, REASON_MERGE]
        )

    # ---- 输入校验与属性读取 --------------------------------------------------

    @staticmethod
    def _validate(doc: Any) -> list[Any]:
        """输入校验（CHUNK-1001）：blocks 必须存在且非空。"""
        blocks = getattr(doc, "blocks", None)
        if blocks is None:
            raise ChunkInputError(
                "CHUNK-1001：输入缺少 blocks 属性（UnstructuredDoc 契约见 FR-KB-010 §6.1）"
            )
        try:
            block_list = list(blocks)
        except TypeError as exc:
            raise ChunkInputError(f"CHUNK-1001：blocks 不可迭代（{exc}）") from exc
        if not block_list:
            raise ChunkInputError("CHUNK-1001：blocks 为空，无内容可分块")
        return block_list

    @staticmethod
    def _heading_level(block: Any) -> int:
        """读取标题层级（缺省按 1 级，鸭子类型容忍）。"""
        level = getattr(block, "heading_level", None)
        return level if isinstance(level, int) and level > 0 else 1

    @staticmethod
    def _block_text(block: Any) -> str:
        """读取 block 文本（缺省空串，鸭子类型容忍）。"""
        return str(getattr(block, "text", "") or "")
