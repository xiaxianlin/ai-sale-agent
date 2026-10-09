# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""解析统一中间格式与解析器抽象基类（FR-KB-010 多格式文档解析）。

五种来源格式（PDF / Word / Excel / 图片 OCR / 语音 ASR）统一输出 UnstructuredDoc
（blocks 序列：标题 / 段落 / 表格），解析与下游清洗（FR-KB-011）、语义分块
（FR-KB-012）解耦，下游只面向中间格式编程。

表格原子块契约：type='table' 的 block 携带完整二维表，解析层保证表格从不跨块
拆分；FR-KB-012 分块按契约不得切分 table 块（见实现方案 §2 决策表、§6.1）。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.1
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

# ── 块类型常量（与实现方案 §6.1 Block.type 枚举一致）────────────────────────
BLOCK_TYPE_HEADING = "heading"      # 标题块
BLOCK_TYPE_PARAGRAPH = "paragraph"  # 段落块
BLOCK_TYPE_TABLE = "table"          # 表格块（原子块，禁止跨块拆分）

# ── 错误码（实现方案 §5：PARSE-1001 不支持的格式；PARSE-1002 文件损坏/加密）──
CODE_UNSUPPORTED = "PARSE-1001"
CODE_CORRUPT = "PARSE-1002"


class ParseError(Exception):
    """解析过程基础异常：统一中文错误信息并携带接口错误码（接口设计 §1.3）。

    上游编排层（parse_service，见实现方案 §6.6）捕获本异常后按
    重试 → 降级 → 人工兜底队列 的顺序处理。
    """

    def __init__(self, message: str, code: str = "PARSE-1000") -> None:
        super().__init__(message)
        self.message = message  # 中文错误信息（对外展示）
        self.code = code        # 统一错误码


class UnsupportedFormatError(ParseError):
    """不支持的格式 / 缺少可选解析依赖（错误码 PARSE-1001）。

    两种触发场景：
    1. 文件扩展名/魔数未在 registry 注册（不重试，直接进人工兜底队列）；
    2. 可选依赖（pymupdf / paddleocr / openai-whisper 等）未安装——
       import 守护抛出本异常并附中文安装提示。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code=CODE_UNSUPPORTED)


class CorruptOrEncryptedError(ParseError):
    """文件损坏或加密无法解析（错误码 PARSE-1002）。

    加密 PDF 等无法自动降级的场景最终进人工兜底队列（实现方案 §6.2①）。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code=CODE_CORRUPT)


@dataclass
class Block:
    """中间格式最小单元。

    table 块为原子块：携带完整二维表（list[list[str]]），禁止跨块拆分；
    跨页/跨段拼接必须在发射前完成（见 BaseParser.emit_table 契约）。
    """

    type: str                                    # 'heading' | 'paragraph' | 'table'
    heading_level: int = 0                       # 标题层级 1~6；0=正文
    text: str = ""                               # 文本内容（table 块可为空）
    table: list[list[str]] | None = None         # 完整二维表格，仅 type='table' 时非空
    meta: dict[str, Any] = field(default_factory=dict)  # 页码/时间戳/OCR 置信度等溯源元数据


@dataclass
class UnstructuredDoc:
    """五种格式统一解析结果，供 FR-KB-011 清洗 / FR-KB-012 分块消费。

    序列化归档至 OSS（p0/parse-output/{md5}.json），支撑 §16.1 抽样核对
    与下游重放（实现方案 §4）。
    """

    source_file: str                             # 源文件路径 / OSS 对象键
    source_format: str                           # 与 ingest_pipeline_log.source_format 枚举一致
    blocks: list[Block] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)  # chars/tables/degrade 等统计

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 归档的字典（OSS 解析产物格式，实现方案 §4）。"""
        return {
            "source_file": self.source_file,
            "source_format": self.source_format,
            "blocks": [
                {
                    "type": b.type,
                    "heading_level": b.heading_level,
                    "text": b.text,
                    "table": b.table,
                    "meta": b.meta,
                }
                for b in self.blocks
            ],
            "stats": self.stats,
        }


def cfg_get(cfg: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    """读取外置配置项（app_config 热更新口径，NFR-MAI-02）；未配置时返回默认值。"""
    if cfg is None:
        return default
    return cfg[key] if key in cfg else default


class BaseParser(ABC):
    """解析器抽象基类：parse() 为模板方法，子类只实现 _do_parse。

    子类约定：
    - 类属性 parser_name（如 'pdf_parser'）与 fmt（source_format 枚举值）；
    - _do_parse 内通过 _set_stats() 追加解析器特有统计
      （如 degrade=['no_text_layer->ocr']、needs_manual_review=True）；
    - 表格块只能经 emit_table() 发射，保证原子块契约。
    """

    parser_name: str = "base"
    fmt: str = ""

    #: 解析器特有统计（每次 parse() 开始时重置；由 _set_stats 写入）
    _extra_stats: dict[str, Any]

    def parse(self, path: str) -> UnstructuredDoc:
        """模板方法：调 _do_parse → 汇总 chars/tables 统计 → 异常统一包装为 ParseError。"""
        self._extra_stats = {}
        try:
            blocks = self._do_parse(path)
        except ParseError:
            # 本模块抛出的业务异常（加密/不支持的格式/降级失败）原样上抛，不重复包装
            raise
        except Exception as exc:  # noqa: BLE001 —— 第三方库异常统一转 ParseError，禁止裸吞
            raise ParseError(f"解析失败（{self.parser_name}）：{exc}") from exc
        stats: dict[str, Any] = {
            "chars": sum(len(b.text) for b in blocks),
            "tables": sum(1 for b in blocks if b.type == BLOCK_TYPE_TABLE),
        }
        stats.update(self._extra_stats)
        return UnstructuredDoc(
            source_file=path,
            source_format=self.fmt,
            blocks=blocks,
            stats=stats,
        )

    @abstractmethod
    def _do_parse(self, path: str) -> list[Block]:
        """子类实现：读取源文件并按阅读顺序产出 blocks（表格必须整表发射）。"""

    def _set_stats(self, **kv: Any) -> None:
        """追加解析器特有统计项（如 degrade / needs_manual_review / warnings）。"""
        stats = getattr(self, "_extra_stats", None)
        if stats is None:
            stats = {}
            self._extra_stats = stats
        stats.update(kv)

    @staticmethod
    def emit_table(rows: list[list[str]], meta: dict[str, Any] | None = None) -> Block:
        """表格块唯一出口：必须整表发射；跨页/跨段拼接须在调用前完成（FR-KB-012 契约）。

        - rows 做浅拷贝，防止调用方后续修改污染已发射块；
        - rows 为 None 直接抛错——空表不应出块，禁止产生 table=None 的脏块。
        """
        if rows is None:
            raise ParseError("表格块禁止携带空行集（FR-KB-012 表格不跨块契约）")
        return Block(
            type=BLOCK_TYPE_TABLE,
            table=[list(row) for row in rows],
            meta=dict(meta) if meta else {},
        )
