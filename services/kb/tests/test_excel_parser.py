# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""Excel 解析器单元测试（FR-KB-010）。

覆盖（对应实现方案 §9 test_excel_multisheet）：
- 多 sheet：每 sheet 1 个 heading 块 + N 个完整表格块（worksheets 顺序保序）；
- 连续 ≥2 空行 = 逻辑表边界：每张逻辑表整块发射、绝不按行数切割（FR-KB-012 契约）；
- 合并单元格值在左上角、余格空串；首尾全空行/列剔除；
- 单元格超限告警（parse.excel.max_cells）；
- import 守护：openpyxl 缺失时抛带中文提示的 UnsupportedFormatError。

说明：openpyxl 属核心依赖，但环境未安装时实测用例自动跳过（importorskip），
纯逻辑用例（切分/裁剪）不依赖 openpyxl、始终执行。

实现方案：docs/07-实现方案/KB/SR-KB-010-多格式文档解析.md §6.3、§7、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import sys
from typing import Any

import pytest

from services.kb.core.pipeline.parsers.base import (
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_TABLE,
    Block,
    UnsupportedFormatError,
)
from services.kb.core.pipeline.parsers.excel_parser import ExcelParser


class TestExcelPureLogic:
    """纯逻辑用例：不依赖 openpyxl（其缺失环境同样可回归表格不拆分契约）。"""

    def test_split_by_blank_rows(self) -> None:
        """连续 ≥2 空行切分逻辑表；单空行保留在表内。"""
        rows = [
            ["型号", "电压"],
            ["A1", "220V"],
            ["", ""],
            ["", ""],          # 连续第 2 空行 → 逻辑表边界
            ["名称", "价格"],
            ["", ""],          # 单空行：属第二张逻辑表内部结构，不切分
            ["B2", "$9"],
        ]
        groups = ExcelParser._split_by_blank_rows(rows)
        assert len(groups) == 2
        assert groups[0] == [["型号", "电压"], ["A1", "220V"]]
        assert groups[1] == [["名称", "价格"], ["", ""], ["B2", "$9"]]

    def test_split_long_table_never_cut_by_rows(self) -> None:
        """表格不跨块契约：无空行边界的 30 行表必须保持 1 张完整逻辑表。"""
        rows = [[f"R{i}", f"V{i}"] for i in range(30)]
        groups = ExcelParser._split_by_blank_rows(rows)
        assert len(groups) == 1
        assert len(groups[0]) == 30

    def test_trim_blank_rows_and_cols(self) -> None:
        """去首尾全空行/列（首尾边界裁剪；表内空行保留）。"""
        rows = [
            ["", "", ""],       # 首部全空行 → 剔除
            ["", "型号", "电压"],
            ["", "A1", "220V"],
            ["", "", ""],       # 尾部全空行 → 剔除
        ]
        # 首列全空（剔除首列后第二列"电压"保留为尾列）
        assert ExcelParser._trim_blank(rows) == [["型号", "电压"], ["A1", "220V"]]

    def test_trim_pads_short_rows(self) -> None:
        """行宽不齐时右侧补空串规范化为矩形（防信息错位）。"""
        assert ExcelParser._trim_blank([["型号", "电压"], ["A1"]]) == [["型号", "电压"], ["A1", ""]]

    def test_trim_blank_empty(self) -> None:
        """全空输入返回空（整 sheet 空 → 不产块）。"""
        assert ExcelParser._trim_blank([]) == []
        assert ExcelParser._trim_blank([["", ""], ["", ""]]) == []

    def test_import_guard(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """import 守护：openpyxl 缺失时抛带中文提示的 UnsupportedFormatError。"""
        path = tmp_path / "a.xlsx"
        path.write_bytes(b"PK\x03\x04")
        monkeypatch.setitem(sys.modules, "openpyxl", None)  # 模拟未安装
        with pytest.raises(UnsupportedFormatError) as exc_info:
            ExcelParser().parse(str(path))
        assert "openpyxl" in str(exc_info.value)
        assert exc_info.value.code == "PARSE-1001"


# ─────────────────────── Excel 实测（openpyxl，缺失自动跳过）──────────────────────


@pytest.fixture()
def openpyxl_mod() -> Any:
    """openpyxl 模块（未安装时实测用例自动跳过；纯逻辑用例不受影响）。"""
    return pytest.importorskip("openpyxl", reason="openpyxl 未安装，Excel 实测跳过")


@pytest.fixture()
def sample_xlsx(tmp_path: Any, openpyxl_mod: Any) -> str:
    """生成 3 sheet 测试工作簿：
    - 参数表：含 2 张逻辑表（2 空行分隔）+ 合并单元格 + 首尾空行空列；
    - 价格表：普通小表；
    - 空表：整 sheet 无内容。
    """
    wb = openpyxl_mod.Workbook()
    ws1 = wb.active
    ws1.title = "参数表"
    # 首部空行 + 空列（A 列留空），测首尾裁剪
    ws1["B2"] = "参数"
    ws1["C2"] = "数值"
    ws1["B3"] = "电压"
    ws1["C3"] = "220V"
    ws1.merge_cells("B2:C2")  # 合并单元格：值在左上角 B2，C2 余格为空
    # 连续 2 空行（第 4、5 行）→ 逻辑表边界
    ws1["B6"] = "名称"
    ws1["C6"] = "价格"
    ws1["B7"] = "继电器"
    ws1["C7"] = "$1.5"
    # 尾部空行
    ws2 = wb.create_sheet("价格表")
    ws2["A1"] = "币种"
    ws2["B1"] = "汇率"
    ws2["A2"] = "USD"
    ws2["B2"] = "7.2"
    wb.create_sheet("空表")
    path = str(tmp_path / "sample.xlsx")
    wb.save(path)
    return path


class TestExcelParserRealFile:
    """openpyxl 实测：多 sheet 结构化与表格整块产出。"""

    def test_multisheet_heading_and_tables(self, sample_xlsx: str) -> None:
        """每 sheet 1 个 heading 块 + N 个完整 table 块；空 sheet 不产块；顺序保序。"""
        doc = ExcelParser().parse(sample_xlsx)
        assert doc.source_format == "excel"
        # 参数表（heading + 2 张逻辑表）→ 价格表（heading + 1 表）；空表跳过
        assert [b.type for b in doc.blocks] == [
            BLOCK_TYPE_HEADING,
            BLOCK_TYPE_TABLE,
            BLOCK_TYPE_TABLE,
            BLOCK_TYPE_HEADING,
            BLOCK_TYPE_TABLE,
        ]
        assert doc.blocks[0].text == "工作表：参数表"
        assert doc.blocks[0].heading_level == 1
        assert doc.blocks[3].text == "工作表：价格表"
        # 表块 meta 携带 sheet 溯源
        assert doc.blocks[1].meta["sheet"] == "参数表"

    def test_logical_table_atomic_not_split(self, sample_xlsx: str) -> None:
        """表格不跨块契约：两张逻辑表各自整块产出（按空行边界，绝不按行数切割）。"""
        doc = ExcelParser().parse(sample_xlsx)
        tables = [b.table for b in doc.blocks if b.type == BLOCK_TYPE_TABLE]
        # 逻辑表 1：合并单元格 → 值在左上角、余格空串；首尾空行空列已剔除
        assert tables[0] == [["参数", ""], ["电压", "220V"]]
        # 逻辑表 2：跨过 2 空行边界的完整表
        assert tables[1] == [["名称", "价格"], ["继电器", "$1.5"]]

    def test_numbers_as_string_cells(self, sample_xlsx: str) -> None:
        """数值单元格转为字符串，无关键信息丢失（§16.1 数据完整性）。"""
        doc = ExcelParser().parse(sample_xlsx)
        price_table = [b for b in doc.blocks if b.type == BLOCK_TYPE_TABLE and b.meta.get("sheet") == "价格表"][0]
        assert price_table.table == [["币种", "汇率"], ["USD", "7.2"]]

    def test_cell_overflow_warning(self, tmp_path: Any, openpyxl_mod: Any) -> None:
        """单元格数超 parse.excel.max_cells → stats.warnings 告警（人工确认入口）。"""
        wb = openpyxl_mod.Workbook()
        ws = wb.active
        ws.title = "大表"
        for r in range(1, 11):
            for c in range(1, 11):
                ws.cell(row=r, column=c, value=f"v{r}{c}")
        path = str(tmp_path / "big.xlsx")
        wb.save(path)
        parser = ExcelParser(cfg={"parse.excel.max_cells": 50})  # 100 > 50 → 告警
        doc = parser.parse(path)
        assert doc.stats["warnings"], "超限应产生告警"
        assert "大表" in doc.stats["warnings"][0]
        # 告警不阻断解析：表格仍完整产出
        assert doc.stats["tables"] == 1
        assert len(doc.blocks[1].table or []) == 10

    def test_corrupt_file(self, tmp_path: Any, openpyxl_mod: Any) -> None:
        """损坏文件 → PARSE-1002（需 openpyxl 可用，否则走 import 守护路径）。"""
        path = tmp_path / "bad.xlsx"
        path.write_bytes(b"\x00\x01not-excel")
        from services.kb.core.pipeline.parsers.base import ParseError

        with pytest.raises(ParseError) as exc_info:
            ExcelParser().parse(str(path))
        assert exc_info.value.code == "PARSE-1002"
