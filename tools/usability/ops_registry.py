# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""运维操作清单装载器（operations.yaml 的解析与校验，coverage_check / task_sheet 共用）。

为避免引入 pyproject 之外的依赖（COM-IP-05 依赖白名单纪律），这里内置一个
**受限 YAML 子集解析器**，支持 operations.yaml 实际使用的语法：

- 顶层 ``key:`` 开启清单节（值为条目列表）或 ``key: 标量``（元信息）；
- 列表条目 ``- key: value`` 起、同缩进 +2 的 ``key: value`` 续行组成一个映射；
- 标量：``true/false`` 布尔、纯整数字符串转 int、引号剥壳、其余按原文字符串；
- ``#`` 开头的整行注释与空行忽略（不支持行尾注释，值内 ``#`` 原样保留）。

解析结果校验后产出 ``Operation``（dataclass），供核对工具与任务单生成器消费。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: 操作码合法字符（大写字母/数字/下划线；与 SR-KB-023 ops_registry 口径一致）
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: api 字段合法形态：``METHOD /path``
_API_RE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) \/.+$")
#: 危险级合法取值
_DANGER_LEVELS = ("false", "true", "high")
#: 界面操作步数上限（SR-KB-023 §6.3 易用性规范：任何操作 ≤3 步）
STEPS_LIMIT: int = 3

#: dangerous 字段的中文展示名（报告/任务单用）
DANGER_LABELS: dict[str, str] = {
    "false": "普通",
    "true": "危险（二次确认）",
    "high": "高危（输入名称确认）",
}


@dataclass(frozen=True)
class Operation:
    """一条运维操作声明（无 SQL/命令行覆盖核对的登记单元）。

    - ``code``：操作码（如 KB_ITEM_CREATE，任务单与核对报告的关联键）；
    - ``name``：操作名（界面用语，遵循 platform/core/i18n 术语表）；
    - ``api``：支撑端点（``METHOD /path``，路径模板参数用 ``{uid}`` 形态）；
    - ``steps``：界面操作步数（≤3，超限由 coverage_check 报违规）；
    - ``dangerous``：危险级 false/true/high（high=删除/回滚等需输入名称确认）。
    """

    code: str
    name: str
    api: str
    steps: int
    dangerous: str

    @property
    def method(self) -> str:
        """HTTP 方法（api 字段空格前段）。"""
        return self.api.split(" ", 1)[0]

    @property
    def path(self) -> str:
        """路径模板（api 字段空格后段）。"""
        return self.api.split(" ", 1)[1]

    @property
    def danger_label(self) -> str:
        """危险级中文展示名。"""
        return DANGER_LABELS.get(self.dangerous, self.dangerous)


def _parse_scalar(raw: str) -> object:
    """标量字面量转 Python 值：引号剥壳 → 布尔 → 整数 → 原文。"""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def parse_simple_yaml(text: str) -> dict[str, object]:
    """解析受限 YAML 子集（见模块 docstring 语法约定）为 ``dict``。

    语法越界（顶层映射值非标量、列表项前出现映射续行等）抛 ``ValueError``，
    消息中文并带行号，便于定位登记笔误。
    """
    root: dict[str, object] = {}
    section_list: list[dict[str, object]] | None = None
    current_item: dict[str, object] | None = None
    section_name = ""

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            current_item = None
            if stripped.endswith(":"):
                # 顶层节名：值为其下的列表
                section_name = stripped[:-1].strip()
                section_list = []
                root[section_name] = section_list
            elif ": " in stripped:
                key, _, value = stripped.partition(": ")
                root[key.strip()] = _parse_scalar(value)
            else:
                raise ValueError(f"operations.yaml 第 {lineno} 行语法不支持：{stripped!r}")
        else:
            if section_list is None:
                raise ValueError(f"operations.yaml 第 {lineno} 行缩进出现在任何节之前：{stripped!r}")
            if stripped.startswith("- "):
                current_item = {}
                section_list.append(current_item)
                rest = stripped[2:]
                if ": " in rest or rest.endswith(":"):
                    key, _, value = rest.partition(":")
                    current_item[key.strip()] = _parse_scalar(value)
                else:
                    raise ValueError(f"operations.yaml 第 {lineno} 行列表项缺少键值：{stripped!r}")
            else:
                if current_item is None:
                    raise ValueError(f"operations.yaml 第 {lineno} 行续行出现在列表项之前：{stripped!r}")
                if ": " not in stripped and not stripped.endswith(":"):
                    raise ValueError(f"operations.yaml 第 {lineno} 行缺少键值分隔：{stripped!r}")
                key, _, value = stripped.partition(":")
                current_item[key.strip()] = _parse_scalar(value)
    return root


def _coerce_operation(entry: object, index: int) -> Operation:
    """单个 YAML 映射 → Operation（字段完整性与取值校验，违规抛 ValueError）。"""
    if not isinstance(entry, dict):
        raise ValueError(f"operations.yaml 第 {index + 1} 个操作不是映射结构")
    missing = [key for key in ("code", "name", "api") if not str(entry.get(key, "") or "").strip()]
    if missing:
        raise ValueError(f"operations.yaml 第 {index + 1} 个操作缺字段：{', '.join(missing)}")
    code = str(entry["code"]).strip()
    api = str(entry["api"]).strip()
    if not _CODE_RE.match(code):
        raise ValueError(f"操作码 {code!r} 不合法（大写字母/数字/下划线，字母开头）")
    if not _API_RE.match(api):
        raise ValueError(f"操作 {code} 的 api 字段不合法（应为 'METHOD /path'）：{api!r}")
    steps = entry.get("steps", 2)
    if not isinstance(steps, int) or steps < 1:
        raise ValueError(f"操作 {code} 的 steps 不合法（正整数）：{steps!r}")
    raw_danger = entry.get("dangerous", "false")
    if isinstance(raw_danger, bool):
        dangerous = "true" if raw_danger else "false"
    else:
        dangerous = str(raw_danger).strip().lower()
    if dangerous not in _DANGER_LEVELS:
        raise ValueError(f"操作 {code} 的 dangerous 不合法（{'/'.join(_DANGER_LEVELS)}）：{raw_danger!r}")
    return Operation(code=code, name=str(entry["name"]).strip(), api=api, steps=steps, dangerous=dangerous)


def load_operations(path: str | Path) -> list[Operation]:
    """读取并校验 operations.yaml → Operation 列表（按登记顺序）。

    文件缺失 / YAML 语法越界 / 字段不合法均抛异常（中文消息）；调用方
    （coverage_check / task_sheet）负责把异常转为退出码或报告项。
    """
    yaml_path = Path(path)
    if not yaml_path.is_file():
        raise FileNotFoundError(f"操作清单不存在：{yaml_path}（FR-KB-023 覆盖核对输入无效）")
    parsed = parse_simple_yaml(yaml_path.read_text(encoding="utf-8"))
    raw_operations = parsed.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError(f"{yaml_path} 缺少 operations 列表节")
    operations = [_coerce_operation(entry, idx) for idx, entry in enumerate(raw_operations)]
    codes = [op.code for op in operations]
    duplicated = sorted({code for code in codes if codes.count(code) > 1})
    if duplicated:
        raise ValueError(f"操作码重复登记：{', '.join(duplicated)}")
    return operations


def normalize_path(path: str) -> str:
    """路径模板归一化：``{参数名}`` 一律压平为 ``{}``，供两侧宽松比对。

    声明侧 ``{id}`` 与路由侧 ``{uid}`` 视为同一端点（模板参数名不是契约），
    仅在完全无参数形态可匹配时启用（见 coverage_check 的两轮匹配）。
    """
    return re.sub(r"\{[^{}]+\}", "{}", path)
