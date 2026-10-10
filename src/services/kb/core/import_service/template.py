# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""导入模板生成器（FR-KB-021 / SR-KB-021 §6.1，迭代 0.3）。

「模板即文档」：按 kb_type 从 Schema 注册表（FR-KB-020，鸭子类型注入——本模块
只读字段对象的 key/label/required/options/help/example/widget 属性，不依赖注册表
的具体实现类）生成 Excel/CSV 导入模板，非技术人员拿到即会填（SRS §16.1）：

- xlsx：「数据」sheet（中文列头必填带 * 号 + 首行示例 + 选项列内置下拉校验）
  +「填写说明」sheet（列名/必填/格式说明/示例逐列一行）；
- csv：仅列头 + 示例行（utf-8-sig 带 BOM，Excel 双击直开不乱码）；
- 所有写出的字符串单元格统一过 sanitize_cell：以 = + - @ 开头的值前置单引号
  转义，防止模板/报告被填入公式（SR-KB-021 §6.3、§8 安全）。

依据文档：docs/07-实现方案/KB/SR-KB-021-批量导入.md §5、§6.1、§8、§9；
         docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）。
"""
from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

#: 「数据」sheet 名（与导入解析端的首个 sheet 约定一致）
DATA_SHEET_NAME: str = "数据"
#: 「填写说明」sheet 名
TIPS_SHEET_NAME: str = "填写说明"
#: 下拉校验生效区间：第 2 行（首行示例）到第 10001 行
DROPDOWN_FIRST_ROW: int = 2
DROPDOWN_LAST_ROW: int = 10001
#: 内置下拉枚举上限（Excel 公式长度约束，超出部分仍由后端校验兜底）
DROPDOWN_MAX_OPTIONS: int = 200
#: 公式注入危险前缀（= + - @，写入前转义）
FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")
#: 复杂控件列不做下拉（改由「填写说明」sheet 指导 JSON 填法，SR-KB-021 §6.1）
NON_DROPDOWN_WIDGETS: tuple[str, ...] = ("multi_select", "object", "object_array")
#: xlsx 媒体类型（模板与错误行报告下载共用）
XLSX_MEDIA_TYPE: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 条目基础列（六大知识库公共：标题/正文），置于注册表字段之前（SR-KB-021 §6.1）
_BASE_FIELD_DEFS: tuple[tuple[str, str, bool, str, str], ...] = (
    ("title", "标题", True, "条目标题，列表与检索展示用，请在同一行内填写（不要换行）", "便携蓝牙音箱 X1"),
    ("content", "正文", True, "条目正文，支持较长的多段文本", "内置 5200mAh 电池，续航 12 小时，支持 AUX/蓝牙双输入。"),
)


def sanitize_cell(value: Any) -> Any:
    """防公式注入（SR-KB-021 §6.3）：以 = + - @ 开头的字符串前置单引号转义。

    仅处理字符串；其余类型（数字/日期等）原样返回。CSV 同样适用——Excel 打开
    CSV 时也会把 ``=...`` 单元格当公式执行。
    """
    if isinstance(value, str) and value[:1] in FORMULA_PREFIXES:
        return "'" + value
    return value


def workbook_to_bytes(workbook: Workbook) -> bytes:
    """Workbook 序列化为 xlsx 字节流（内存 BytesIO，不落盘）。"""
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _attr(field: Any, name: str, default: Any = None) -> Any:
    """鸭子类型读取字段属性：缺失或 None 一律回退默认值（不依赖注册表具体类）。"""
    value = getattr(field, name, None)
    return default if value is None else value


@dataclass(frozen=True)
class ColumnSpec:
    """模板列规格：由注册表字段归一化而来（模板列头/下拉/说明的单一来源）。"""

    key: str                 # 字段英文键（映射回条目 metadata）
    label: str               # 中文列名（模板表头展示）
    required: bool = False   # 是否必填（表头加 * 号）
    options: tuple[str, ...] = ()  # 静态/动态选项（生成内置下拉）
    help_text: str = ""      # 格式/填写说明（「填写说明」sheet 与错误建议共用口径）
    example: str = ""        # 示例值（首行示例 + 说明 sheet）
    widget: str = ""         # 控件类型（multi_select/object 等复杂控件不做下拉）


def display_label(spec: ColumnSpec) -> str:
    """展示列名：必填列追加 * 号（与后端校验的必填口径同源）。"""
    return spec.label + ("*" if spec.required else "")


def column_specs(
    kb_type: str,
    schema_provider: Any,
    dynamic_options: Mapping[str, Sequence[str]] | None = None,
) -> list[ColumnSpec]:
    """按 kb_type 归一化模板列规格：基础列（标题/正文）+ 注册表字段。

    dynamic_options 为 api/taxonomy 等来源列的当前取值（Schema 端点同源解析），
    覆盖字段静态 options 进入下拉——下拉枚举与后端校验永远一致（SR-KB-021 §2 决策 1）。
    kb_type 未注册时由 schema_provider.fields 抛错（上游转人话 40001）。
    """
    specs: list[ColumnSpec] = [
        ColumnSpec(key=key, label=label, required=required, help_text=help_text, example=example)
        for key, label, required, help_text, example in _BASE_FIELD_DEFS
    ]
    for field in schema_provider.fields(kb_type):
        key = str(_attr(field, "key", ""))
        label = str(_attr(field, "label", key))
        options = tuple(str(opt) for opt in (_attr(field, "options", ()) or ()))
        dynamic = (dynamic_options or {}).get(key)
        if dynamic:
            options = tuple(str(opt) for opt in dynamic)
        example = str(_attr(field, "example", ""))
        widget = str(_attr(field, "widget", ""))
        if not example and widget in NON_DROPDOWN_WIDGETS:
            # 复杂控件列给 JSON 填法示例（下拉无法表达，见「填写说明」sheet）
            example = '{"key": "value"}'
        specs.append(
            ColumnSpec(
                key=key,
                label=label,
                required=bool(_attr(field, "required", False)),
                options=options,
                help_text=str(_attr(field, "help", "") or _attr(field, "pattern_msg", "")),
                example=example,
                widget=widget,
            )
        )
    return specs


def _column_width(spec: ColumnSpec) -> float:
    """列宽启发式：中文按 2 倍宽度估算，钳制在 12~40。"""
    text = display_label(spec)
    estimated = sum(2.0 if ord(ch) > 127 else 1.0 for ch in text) + 4.0
    return min(40.0, max(12.0, estimated))


def _dropdown_options(spec: ColumnSpec) -> tuple[str, ...]:
    """可进入内置下拉的选项：剔除含逗号/双引号的值（Excel 列表公式无法表达）。"""
    if spec.widget in NON_DROPDOWN_WIDGETS:
        return ()
    return tuple(opt for opt in spec.options[:DROPDOWN_MAX_OPTIONS] if "," not in opt and '"' not in opt)


def _build_dropdown(spec: ColumnSpec) -> DataValidation | None:
    """为选项列构造下拉校验（枚举与后端校验同源；无可用选项返回 None）。"""
    options = _dropdown_options(spec)
    if not options:
        return None
    validation = DataValidation(
        type="list",
        formula1='"' + ",".join(options) + '"',
        allow_blank=not spec.required,
        showErrorMessage=True,
    )
    validation.errorTitle = "填写不符合要求"
    validation.error = f"「{display_label(spec)}」请从下拉列表中选择"
    return validation


def build_workbook(
    kb_type: str,
    schema_provider: Any,
    dynamic_options: Mapping[str, Sequence[str]] | None = None,
) -> Workbook:
    """生成 xlsx 模板工作簿：数据 sheet（表头/示例/下拉）+ 填写说明 sheet。

    示例行与说明文案均过 sanitize_cell（示例值以 = 开头时转义为文本）。
    """
    specs = column_specs(kb_type, schema_provider, dynamic_options)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None  # Workbook() 恒有活动 sheet（类型收窄）
    sheet.title = DATA_SHEET_NAME
    sheet.append([display_label(spec) for spec in specs])
    for index, spec in enumerate(specs, start=1):
        column = get_column_letter(index)
        sheet.column_dimensions[column].width = _column_width(spec)
        validation = _build_dropdown(spec)
        if validation is not None:
            sheet.add_data_validation(validation)
            validation.add(f"{column}{DROPDOWN_FIRST_ROW}:{column}{DROPDOWN_LAST_ROW}")
    # 首行合法示例（占位示例行，用户从第 2 行起填写；示例值防公式注入）
    sheet.append([sanitize_cell(spec.example) for spec in specs])
    sheet.freeze_panes = "A2"

    tips = workbook.create_sheet(TIPS_SHEET_NAME)
    tips.append(["列名", "必填", "格式/说明", "示例"])
    for spec in specs:
        tips.append(
            [
                display_label(spec),
                "必填" if spec.required else "选填",
                spec.help_text or "文本",
                sanitize_cell(spec.example),
            ]
        )
    return workbook


def render_csv(
    kb_type: str,
    schema_provider: Any,
    dynamic_options: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """生成 csv 模板文本：仅列头 + 示例行（Excel 导出惯例 CRLF 行尾）。"""
    specs = column_specs(kb_type, schema_provider, dynamic_options)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([display_label(spec) for spec in specs])
    writer.writerow([sanitize_cell(spec.example) for spec in specs])
    return buffer.getvalue()


def render_csv_bytes(
    kb_type: str,
    schema_provider: Any,
    dynamic_options: Mapping[str, Sequence[str]] | None = None,
) -> bytes:
    """csv 模板字节流：utf-8-sig 带 BOM（Excel 双击直开不乱码）。"""
    return render_csv(kb_type, schema_provider, dynamic_options).encode("utf-8-sig")
