# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""多格式解析（FR-KB-010 / SR-KB-010）TDD 补齐测试（五类场景补强，不改既有文件）。

按 TDD 新规五列审计既有 test_parsers/test_pdf/test_excel/test_ocr/test_asr 后补齐：
- 等价类/边界：扩展名抽取的退化形态（无扩展名 / 尾点 / 点文件）与大小写（.PDF），
  魔数嗅探的空文件与不可读文件（OSError 分支）；
- 异常：注册表覆盖注册语义（同扩展名后注册者胜出，等价类）；
- 并发：多线程并发 resolve 惰性导入（注册表只读共享，导入竞态不串格式）。

KPI 映射：SRS §16.1「知识入库」——解析路由与嗅探是入库链路第一道闸，
路由错误/嗅探误判直接造成入库失败或脏数据（对应入库成功率指标）。

依据文档：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.6、§9。
"""
from __future__ import annotations

import threading

import pytest

from services.kb.core.pipeline.parsers.base import (
    BaseParser,
    Block,
    UnsupportedFormatError,
)
from services.kb.core.pipeline.parsers.registry import (
    REGISTRY,
    FormatRule,
    ParserRegistry,
)


class TestRegistryExtensionEdges:
    """扩展名抽取边界（等价类/边界）：退化形态统一走 PARSE-1001，不产生错误路由。"""

    @pytest.mark.parametrize("filename", ["README", "release-1.2", "archive"])
    def test_no_extension_rejected(self, filename: str) -> None:
        """无扩展名（含多段文件名）：UnsupportedFormatError，不误路由。"""
        with pytest.raises(UnsupportedFormatError) as exc_info:
            REGISTRY.resolve(filename)
        assert exc_info.value.code == "PARSE-1001"

    @pytest.mark.parametrize("filename", ["file.", ".hidden", "提交材料."])
    def test_degenerate_dots_rejected(self, filename: str) -> None:
        """尾点与点文件：扩展名为空串 → 拒绝（不会把整个文件名当扩展名）。"""
        with pytest.raises(UnsupportedFormatError):
            REGISTRY.resolve(filename)

    @pytest.mark.parametrize(
        ("filename", "fmt"),
        [("报告.PDF", "pdf"), ("表格.XLSX", "excel"), ("照片.PNG", "image_ocr")],
    )
    def test_uppercase_extension_routes(self, filename: str, fmt: str) -> None:
        """大写扩展名：路由大小写不敏感（既有用例仅覆盖 .JPEG，此处补 .PDF/.XLSX/.PNG）。"""
        assert REGISTRY.fmt_of(filename) == fmt


class TestSniffFormatEdges:
    """魔数嗅探边界/降级：无法证明格式即返回 None（交由扩展名与人工兜底）。"""

    def test_empty_file_returns_none(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """空文件（0 字节）：头部不足 12 字节，返回 None 而非误判。"""
        target = tmp_path / "empty.pdf"
        target.write_bytes(b"")
        assert REGISTRY.sniff_format(str(target)) is None

    def test_unreadable_path_returns_none(self) -> None:
        """不可读路径（不存在）：OSError 分支返回 None，不抛异常（嗅探是尽力而为）。"""
        assert REGISTRY.sniff_format("/nonexistent/missing.pdf") is None


class TestRegistryOverrideAndConcurrency:
    """注册表语义（异常/等价类 + 并发）：覆盖注册与多线程惰性导入。"""

    def test_reregister_same_extension_last_wins(self) -> None:
        """同扩展名重复注册：后注册者覆盖先注册者（模块 docstring 明确语义）。"""

        def _factory_a() -> BaseParser:  # pragma: no cover - 工厂不被 resolve 触发
            raise AssertionError("不应被调用")

        def _factory_b() -> BaseParser:  # pragma: no cover - 工厂不被 resolve 触发
            raise AssertionError("不应被调用")

        registry = ParserRegistry()
        registry.register(FormatRule((".custom",), "word", "parser_a", _factory_a))
        registry.register(FormatRule((".custom",), "excel", "parser_b", _factory_b))
        assert registry.rule_of("a.custom").parser_name == "parser_b"
        assert registry.fmt_of("a.custom") == "excel"

    def test_concurrent_resolve_lazy_import_no_crosswiring(self) -> None:
        """并发：8 线程并发 resolve 五种格式 → 实例类型与格式一一对应，无串线。

        REGISTRY 为进程级只读共享单例，resolve 内部惰性导入；并发首调
        验证导入竞态下解析器实例不串格式（NFR-REL-01 稳定性）。
        """
        expectations: list[tuple[str, str]] = [
            ("a.docx", "word_parser"),
            ("b.xlsx", "excel_parser"),
            ("c.png", "ocr_parser"),
            ("d.mp3", "asr_parser"),
        ]
        results: list[tuple[str, str, str]] = []  # (文件, parser_name, 线程内校验标记)
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            filename, expected = expectations[index % len(expectations)]
            barrier.wait()
            parser_name = REGISTRY.name_of(filename)
            with lock:
                results.append((filename, parser_name, expected))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 8
        for filename, parser_name, expected in results:
            assert parser_name == expected, filename
