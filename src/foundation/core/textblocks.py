# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识片段 Prompt 契约层（CR Q1 修复：跨服务逐字复制的 parse_chunk_blocks 收敛为单一实现）。

「【知识片段】…【引用格式】」是 agent（FR-AG-003）与 site（FR-SITE-003）共用的
检索块注入 Prompt 格式——格式一旦调整两服务必须同步，故收敛至 foundation 单点
（此前 brain/generator.py 与 site/conversation/service.py 各持一份 22 行逐字拷贝）。
"""
from __future__ import annotations

import re

#: 编号块行头：如「[R1] ST-3200 参数表」（ref 含 R 前缀，与 [R#] 引用标记同形）
BLOCK_HEADER_RE = re.compile(r"^\[(R\d+)\]\s*(.+)$")


def parse_chunk_blocks(prompt: str) -> list[dict[str, str]]:
    """解析 【知识片段】…【引用格式】 之间的编号块 → [{ref, title, text}]。

    - ref 为带 R 前缀的编号（"R1"、"R2"…，与 [R#] 引用标记同形）；
    - 无该段（Prompt 未注入检索块）返回空列表；
    - 末块同样闭合（text 为正文行以空格连接的去空白形态）。
    """
    if "【知识片段】" not in prompt:
        return []
    section = prompt.split("【知识片段】", 1)[1].split("【引用格式】", 1)[0]
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    body: list[str] = []
    for line in section.splitlines():
        matched = BLOCK_HEADER_RE.match(line.strip())
        if matched:
            if current is not None:
                current["text"] = " ".join(body).strip()
                blocks.append(current)
            current = {"ref": matched.group(1), "title": matched.group(2).strip()}
            body = []
        elif current is not None and line.strip():
            body.append(line.strip())
    if current is not None:
        current["text"] = " ".join(body).strip()
        blocks.append(current)
    return blocks
