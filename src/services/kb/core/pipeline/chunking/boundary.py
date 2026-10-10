# 知识产权归甲方所有（COM-IP-01）
"""边界检测器族（FR-KB-012 / SR-KB-012 §6.2、§6.3）：四级边界优先级的可插拔实现。

优先级（05-核心模块详细设计 §2.2，逐级生效）：
    ① HeadingBoundary   标题层级边界（最高优先级）
    ② TableBoundary     表格整体边界（整表成块，不跨块拆分）
    ③ SimilarityBoundary 段落语义边界（相邻句向量余弦骤降点，可插拔，默认不启用）
    ④ LengthFallback    长度上限兜底（最后一个不超过上限的完整句边界）

③ SimilarityBoundary 依赖本地 BGE-M3 句向量（TEI 内网 HTTP，NFR-SEC-06 数据
不出域）。向量服务接入前不注入（或 embed_fn=None），分块自动退化为
「标题 + 表格 + 长度」三级边界；接入后注入 embed_fn 即启用完整四级
（替换点：FR-KB-013 向量化服务同源客户端）。

本模块通过 ChunkConfigLike / BufferedSentenceLike 结构化协议与主循环解耦，
避免与 chunker.py 循环导入（协议由 chunker.ChunkConfig / BufferedSentence
鸭子类型满足）。
"""
from __future__ import annotations

import abc
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

# ---- 边界依据常量（boundary_reasons 审计值，SR-KB-012 §4）-------------------
REASON_HEADING = "heading"                  # ① 标题层级边界
REASON_TABLE = "table_whole"                # ② 表格整体边界
REASON_TABLE_SPLIT = "table_row_group_split"  # ② 受控例外：超限表格按行组切分（留痕告警）
REASON_SEMANTIC = "semantic"                # ③ 句向量骤降语义边界
REASON_HARD_LIMIT = "hard_limit"            # ④ 长度上限兜底
REASON_MERGE = "merge"                      # 后处理：过短块合并

# 向量类型别名：与 BGE-M3 返回的稠密向量一致（本地 TEI，维度 1024）
Vector = Sequence[float]


class ChunkConfigLike(Protocol):
    """分块配置鸭子类型（由 chunker.ChunkConfig 结构化满足，避免循环导入）。"""

    sim_drop: float          # 相邻句余弦骤降阈值（相对前序窗口均值的落差）
    sim_window: int          # 骤降参照窗口（句数）
    table_hard_cap: int      # 表格整块保护上限（token）
    max_tokens: int          # 单块目标上限（token）
    sent_min_chars: int      # 过短句并入邻句阈值（字符）


class BufferedSentenceLike(Protocol):
    """缓冲句鸭子类型（由 chunker.BufferedSentence 结构化满足）。"""

    block_index: int         # 所属上游 block 序号
    text: str                # 句子文本
    tokens: int              # 句子 token 数
    drop_before: bool        # 其前存在句向量骤降点（候选切点）


@dataclass(frozen=True)
class BoundaryHit:
    """一次边界命中：reason 为切点依据（boundary_reasons 审计值）。

    detail 约定：
        句级检测器携带 {"cut": int}——切点位置 p，表示发射缓冲句 [0:p)、
        其余句（含触发判定的待并入句）留在缓冲继续聚合；
        块级检测器仅携带 reason，出块计划由主循环另行取用。
    """

    reason: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BoundaryContext:
    """边界判定上下文（只读，由主循环每步构造）。

    块级判定（①②）关注 block；句级判定（③④）关注 sentences 与
    buffer_tokens——句级判定时 sentences 为「当前缓冲句 + 待并入句」，
    buffer_tokens 已包含待并入句的 token。
    """

    block: Any                        # 当前上游 block（UnstructuredDoc.blocks 元素，鸭子类型）
    block_index: int                  # 当前 block 序号
    sentences: tuple                   # tuple[BufferedSentenceLike, ...]
    buffer_tokens: int                 # 缓冲 token 数（句级判定时含待并入句）
    config: ChunkConfigLike            # 分块配置


class BoundaryDetector(abc.ABC):
    """边界检测器抽象：主循环按优先级依次询问，首个命中者生效。

    检测器只做「判定」，不修改主循环状态；出块动作统一由
    chunker.SemanticChunker 执行（职责分离，便于独立单测）。
    """

    #: 本级边界依据（REASON_* 常量），子类必须覆写
    reason: str = ""

    @abc.abstractmethod
    def detect(self, context: BoundaryContext) -> BoundaryHit | None:
        """在给定上下文中探测边界；未命中返回 None。"""
        raise NotImplementedError


class HeadingBoundary(BoundaryDetector):
    """① 标题层级边界（最高优先级，05 设计 §2.2）：heading block 即切点。

    命中后主循环冲刷当前缓冲（切分依据 heading），并维护标题层级栈
    （弹出同层/低层再压入，见 metadata.HeadingStack）；标题文本归属其后
    首个块（只出现一次，SR-KB-012 §6.3）。
    """

    reason = REASON_HEADING

    def detect(self, context: BoundaryContext) -> BoundaryHit | None:
        if _block_type(context.block) == "heading":
            return BoundaryHit(self.reason)
        return None


@dataclass(frozen=True)
class TablePiece:
    """表格出块计划单元：一个整表（未超限）或一个行组（超限受控切分）。"""

    content: str            # 块内容（整表 / 行组 Markdown，行组含重复表头行）
    spans: list[dict]       # 覆盖区间 {"idx": block 序号, "start": 数据行下界, "end": 数据行上界}
    reasons: list[str]      # 边界依据（table_whole [+ table_row_group_split]）
    summary: str            # 表格摘要（供 FR-KB-020 预览与人工复核）
    split: bool             # 是否发生了行组切分（受控例外）


class TableBoundary(BoundaryDetector):
    """② 表格整体边界：table block 独立成块，绝不与文本合并（参数检索完整性）。

    受控例外（SR-KB-012 §2 偏差标注，05 设计 §2.2 未定义超长表处理）：
    整表 token 超过保护上限 table_hard_cap 时按行组切分，每块首行重复表头行
    （单块仍可独立检索参数），reasons 附加 table_row_group_split 供主循环
    写 degrade 留痕并告警人工复核——绝不静默发生。
    """

    reason = REASON_TABLE

    def __init__(self, token_counter: Callable[[str], int]) -> None:
        self._count_tokens = token_counter

    def detect(self, context: BoundaryContext) -> BoundaryHit | None:
        if _block_type(context.block) == "table":
            return BoundaryHit(self.reason)
        return None

    def plan(self, block_index: int, block: Any, config: ChunkConfigLike) -> list[TablePiece]:
        """产出表格出块计划：默认整表一块；超保护上限才受控按行组切分。"""
        rows = [[str(cell) for cell in row] for row in (getattr(block, "table", None) or [])]
        n_rows = len(rows)
        n_cols = len(rows[0]) if rows else 0
        summary = f"表格 {n_rows}行×{n_cols}列"
        if not rows:
            # 空表：仍需出块（账目对账覆盖该 block），内容为空
            return [TablePiece(
                content="",
                spans=[{"idx": block_index, "start": 0, "end": 0}],
                reasons=[REASON_TABLE],
                summary=summary,
                split=False,
            )]
        whole_md = rows_to_markdown(rows)
        if self._count_tokens(whole_md) <= config.table_hard_cap:
            # 整表保护：不拆分（数据行区间 [0, n_rows-1)）
            return [TablePiece(
                content=whole_md,
                spans=[{"idx": block_index, "start": 0, "end": n_rows - 1}],
                reasons=[REASON_TABLE],
                summary=summary,
                split=False,
            )]
        # 受控例外：行组切分 + 每块重复表头（行区间在 split_row_groups 内贪心聚合）
        split_summary = summary + "（超限行组切分，每块重复表头，需人工复核）"
        header, data = rows[0], rows[1:]
        pieces: list[TablePiece] = []
        for lo, hi, group in split_row_groups(header, data, cap=config.table_hard_cap,
                                              token_counter=self._count_tokens):
            pieces.append(TablePiece(
                content=rows_to_markdown([header, *group]),
                spans=[{"idx": block_index, "start": lo, "end": hi}],
                reasons=[REASON_TABLE, REASON_TABLE_SPLIT],
                summary=split_summary,
                split=True,
            ))
        return pieces


class SimilarityBoundary(BoundaryDetector):
    """③ 段落语义边界（可插拔，SR-KB-012 §6.2）：相邻句向量余弦骤降点 = 话题切换处。

    embed_fn：批量句向量函数 ``list[str] -> list[Vector]``（本地 BGE-M3/TEI
    注入，batch=32 由客户端实现，数据不出内网）。默认 None → 未启用：
    flag_drops 全 False、detect 恒 None，分块自动降为三级边界；
    向量服务接入后注入即启用完整四级（无需改动主循环）。

    骤降判定：以「前序 sim_window 句相似度均值」为基线，当前相邻句相似度
    落差 ≥ sim_drop 即判为骤降点（窗口为空时以自身为基线，落差恒 0 不误报）。
    """

    reason = REASON_SEMANTIC

    def __init__(self, embed_fn: Callable[[list[str]], list[Vector]] | None = None) -> None:
        self.embed_fn = embed_fn

    @property
    def enabled(self) -> bool:
        """是否已接入句向量服务（embed_fn 注入即启用）。"""
        return self.embed_fn is not None

    def flag_drops(self, sentences: list[str], config: ChunkConfigLike) -> list[bool]:
        """逐句骤降标记：flags[i]=True 表示第 i 句之后是候选切点。

        返回长度恒为句数 - 1（相邻句对数）；未启用或句数不足时全 False。
        结果供主循环逐句缓冲时以 drop_before 形态携带；主循环单遍消费，
        无需缓存（同一 block 只判定一次，SR-KB-012 §6.2 的段落级缓存优化
        在接入真实向量服务后按需补充）。
        """
        if self.embed_fn is None or len(sentences) < 2:
            return [False] * max(len(sentences) - 1, 0)
        vectors = self.embed_fn(list(sentences))
        if len(vectors) != len(sentences):
            raise ValueError("SimilarityBoundary：embed_fn 返回向量数与句子数不一致")
        sims = [cosine(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
        flags = [False] * len(sims)
        for i, cur in enumerate(sims):
            prior = sims[max(0, i - config.sim_window): i]  # 前序窗口（不含自身）
            baseline = (sum(prior) / len(prior)) if prior else cur
            if baseline - cur >= config.sim_drop:  # 落差超阈值 → 骤降点
                flags[i] = True
        return flags

    def detect(self, context: BoundaryContext) -> BoundaryHit | None:
        """超上限时的语义回溯：取缓冲内（含待并入句）最近的骤降切点。"""
        if self.embed_fn is None:
            return None
        sentences = context.sentences
        # p 从大到小回溯：p 即切点位置，发射 [0:p)；sentences[p].drop_before
        # 为 True 表示切点恰在第 p 句之前（含最后一位的待并入句）
        for p in range(len(sentences) - 1, 0, -1):
            if sentences[p].drop_before:
                return BoundaryHit(self.reason, {"cut": p})
        return None


class LengthFallback(BoundaryDetector):
    """④ 长度上限兜底（SR-KB-012 §6.3 ④）：无语义切点时的最后手段。

    在不超过 max_tokens 的前提下取最长完整句前缀切分（「不超过上限的
    最后一个完整句边界」）；单句本身即超限时以首句为切点强制推进——
    句内不切，宁可超限出块也不阻塞流水线（超限块交 token_dist 指标暴露）。
    """

    reason = REASON_HARD_LIMIT

    def detect(self, context: BoundaryContext) -> BoundaryHit | None:
        best = 0
        acc = 0
        for sentence in context.sentences:
            if acc + sentence.tokens <= context.config.max_tokens:
                acc += sentence.tokens
                best += 1
            else:
                break
        if best < 1:
            best = 1  # 单句超长：强制以首句为界，保证主循环推进
        return BoundaryHit(self.reason, {"cut": best})


# ---- 工具函数 ----------------------------------------------------------------


def _block_type(block: Any) -> str:
    """安全读取 block.type（鸭子类型，容忍缺省）。"""
    return str(getattr(block, "type", "") or "")


def cosine(u: Vector, v: Vector) -> float:
    """余弦相似度（零向量防护：任一向量为零时返回 0.0，避免 NaN 污染骤降判定）。"""
    dot = sum(a * b for a, b in zip(u, v))
    norm_u = math.sqrt(sum(a * a for a in u))
    norm_v = math.sqrt(sum(b * b for b in v))
    if norm_u == 0.0 or norm_v == 0.0:
        return 0.0
    return dot / (norm_u * norm_v)


def rows_to_markdown(rows: Sequence[Sequence[str]]) -> str:
    """表格行 → Markdown 表示（首行为表头，含分隔行）；空表返回空串。"""
    if not rows:
        return ""
    lines = ["| " + " | ".join(str(cell) for cell in rows[0]) + " |"]
    lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def split_row_groups(
    header: Sequence[str],
    data: Sequence[Sequence[str]],
    cap: int,
    token_counter: Callable[[str], int],
) -> list[tuple[int, int, list[Sequence[str]]]]:
    """受控行组切分（TableBoundary 超限路径，SR-KB-012 §2 偏差标注）。

    贪心聚合数据行，使「表头 + 行组」的 token 不超过 cap（计量含重复表头行，
    与出块内容口径一致）；单行即超限时该行独立成组（不再细分，靠 degrade
    留痕 + 人工复核兜底）。
    返回 [(行下界, 行上界, 行组), ...]，行区间为数据行下标（表头不计）。
    """
    groups: list[list[Sequence[str]]] = []
    group: list[Sequence[str]] = []
    for row in data:
        candidate = rows_to_markdown([header, *group, row])
        if group and token_counter(candidate) > cap:
            groups.append(group)
            group = [row]
        else:
            group.append(row)
    if group:
        groups.append(group)
    result: list[tuple[int, int, list[Sequence[str]]]] = []
    lo = 0
    for g in groups:
        result.append((lo, lo + len(g), g))
        lo += len(g)
    return result
