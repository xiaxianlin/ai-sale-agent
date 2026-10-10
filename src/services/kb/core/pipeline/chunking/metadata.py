# 知识产权归甲方所有（COM-IP-01）
"""上下文元数据构造（FR-KB-012 / SR-KB-012 §6.4）：heading_path 标题栈 + context_meta 定稿回填。

context_meta 内容契约（SR-KB-012 §4，对应 03-数据库设计 §3.1 knowledge_chunk）：

    headings / parent_heading  父标题结构化形态：供 FR-KB-015 重排加权、FR-KB-020 面包屑
    prev/next_chunk_uid        相邻块指针：检索端可拼接邻块扩展上下文（小 loss 切分大 gain 检索）
    block_spans / block_types  覆盖的上游 block 区间与类型：账目对账依据 + §12.2 原文回溯
    char_span                  块内容在「全块按序拼接文本」中的字符区间（原文近似定位）
    boundary_reasons           本块各切点的边界依据审计（四级优先级的实际生效证据）
    metrics                    块级指标（token 数等）

相邻块指针在全部分块定稿（含过短块合并）后统一回填：插入/合并即重算，
保证 _merge_oversmall 合并后指针不失真（幂等，SR-KB-012 §6.4）。
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅为类型注解服务，运行时零依赖（避免与 chunker 循环导入）
    from .chunker import Chunk


class HeadingStack:
    """标题层级栈（SR-KB-012 §6.3/§6.4）：enter 时先弹出同层/低层标题再压入。

    任意时刻 path() 即「当前生效的各级祖先标题」，以 '/' 连接各层级标题原文；
    无标题文档（纯 Excel 单表、纯文本）路径为空串，context_meta.headings=[]。
    """

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []  # (层级, 标题原文)

    def enter(self, level: int, text: str) -> None:
        """进入标题：弹出栈内层级 >= level 的标题（同层/低层不再生效）后压入。"""
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, text))

    def headings(self) -> list[str]:
        """当前生效的各级标题原文（有序，自顶向下）。"""
        return [text for _, text in self._stack]

    def path(self) -> str:
        """标题层级路径（heading_path 字段值），如 ``产品目录/支架系列/ST-3200/参数``。"""
        return "/".join(self.headings())

    def __len__(self) -> int:
        return len(self._stack)


def make_chunk_uid(item_key: str, chunk_index: int) -> str:
    """块 uid：``c-<sha256(item_key#chunk_index) 前 8 位>``。

    确定性生成：同一清洗产物重放（FR-KB-017 增量重跑）得到完全一致的 uid，
    保证幂等；item_key 取 source_file（编排层可换 item_id，口径由调用方决定）。
    """
    digest = hashlib.sha256(f"{item_key}#{chunk_index}".encode("utf-8")).hexdigest()[:8]
    return f"c-{digest}"


def finalize_chunks(chunks: list["Chunk"], doc: Any, item_key: str) -> None:
    """分块定稿回填（原地修改，必须在合并/行组切分全部完成后调用一次）。

    步骤（SR-KB-012 §6.4）：
        ① 按输出顺序统一赋 chunk_index（item 内从 0 递增）并生成确定性 uid；
        ② 回填 context_meta 标准键（保留 orphan / table_summary 等过程附加键）；
        ③ 统一回填相邻块指针 prev/next_chunk_uid（定稿后一次成型，合并不失真）；
        ④ 累计 char_span（块内容在全块拼接文本中的字符区间）。
    """
    blocks = list(getattr(doc, "blocks", []) or [])
    # ① 索引与 uid 定稿
    for index, chunk in enumerate(chunks):
        chunk.chunk_index = index
        chunk.uid = make_chunk_uid(item_key, index)
    # ②③④ 元数据回填
    offset = 0
    for index, chunk in enumerate(chunks):
        headings = chunk.heading_path.split("/") if chunk.heading_path else []
        meta = dict(chunk.context_meta)  # 保留过程阶段写入的附加键（orphan/table_summary 等）
        meta.update({
            "headings": headings,
            "parent_heading": headings[-2] if len(headings) >= 2 else None,
            "doc_title": str(getattr(doc, "title", "") or ""),
            "source_file": str(getattr(doc, "source_file", "") or ""),
            "prev_chunk_uid": chunks[index - 1].uid if index > 0 else None,
            "next_chunk_uid": chunks[index + 1].uid if index + 1 < len(chunks) else None,
            "block_types": _block_types(blocks, chunk.block_spans),
            "char_span": [offset, offset + len(chunk.content)],
            "boundary_reasons": list(chunk.boundary_reasons),
            "block_spans": [dict(span) for span in chunk.block_spans],
            "metrics": {"tokens": chunk.tokens},
        })
        chunk.context_meta = meta
        offset += len(chunk.content)


def _block_types(blocks: list[Any], spans: list[dict]) -> list[str]:
    """按 span 顺序取上游 block.type 序列（检索过滤与账目审计用）。"""
    ordered = sorted(spans, key=lambda span: int(span.get("idx", 0)))
    types: list[str] = []
    for span in ordered:
        idx = int(span.get("idx", -1))
        if 0 <= idx < len(blocks):
            types.append(str(getattr(blocks[idx], "type", "") or ""))
    return types
