# 知识产权归甲方所有（COM-IP-01）
"""chunking 单元测试公共造数库。

以本地等价 dataclass 模拟解析器中间格式 UnstructuredDoc / Block
（对齐 services/kb/core/pipeline/parsers/base.py 的 FR-KB-010 §6.1 契约；
解析器模块并行建设，此处不导入其代码，走鸭子类型解耦）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from services.kb.core.pipeline.chunking import default_token_counter

# 互不重复的汉字池：用于生成可辨识、token 数可控的中文句子
_HAN_POOL = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥乾坤坎离"


@dataclass
class FakeBlock:
    """等价 Block：type/text 必备，heading_level 与 table 按需（鸭子类型可选属性）。"""

    type: str = "paragraph"
    text: str = ""
    heading_level: Optional[int] = None
    table: Optional[list[list[str]]] = None


@dataclass
class FakeDoc:
    """等价 UnstructuredDoc：blocks + source_file（+ 可选 title/source_format）。"""

    blocks: list[FakeBlock] = field(default_factory=list)
    source_file: str = "p0/clean-output/测试文档.json"
    source_format: str = "markdown"
    title: str = "测试文档"


def heading(text: str, level: int) -> FakeBlock:
    """构造标题 block。"""
    return FakeBlock(type="heading", text=text, heading_level=level)


def para(text: str) -> FakeBlock:
    """构造段落 block。"""
    return FakeBlock(type="paragraph", text=text)


def table_block(rows: list[list[str]]) -> FakeBlock:
    """构造表格 block（首行为表头）。"""
    return FakeBlock(type="table", text="", table=rows)


def cn_sentence(idx: int, han: int = 12) -> str:
    """生成互不重复的中文句子：han 个汉字 + 句号（token 数用 default_token_counter 精确计算）。"""
    body = "".join(_HAN_POOL[(idx * 7 + k) % len(_HAN_POOL)] for k in range(han))
    return body + "。"


def topic_sentence(topic: str, idx: int, han: int = 12) -> str:
    """以单字主题标记开头的中文句子（语义边界测试：mock 嵌入按首字判主题）。

    topic 甲/乙 分别映射两个正交主题向量，同主题句间相似度 1.0、跨主题 0.0。
    """
    body = topic + "".join(_HAN_POOL[(idx * 5 + k + 3) % len(_HAN_POOL)] for k in range(han - 1))
    return body + "。"


def tokens_of(text: str) -> int:
    """测试统一口径的 token 计量（与分块器默认计量一致）。"""
    return default_token_counter(text)
