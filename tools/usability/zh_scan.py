# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""界面文案中文扫描器（NFR-USA-02 静态门禁·后端起步版，迭代 0.3）。

扫描 services/kb/api/ 与 platform/core/ 的 Python 源码，提取**面向用户**的字符串
字面量并输出「疑似英文残留清单」（判定规则沿用 SR-NFR-USA-02 §6.5 / SR-KB-023
§6.2，从 Vue 源扫描适配到后端 Python）：

- 提取面（用户可见文案的载体）：
  - 用户可见关键字参数：``message/summary/description/label/title/placeholder/
    detail/tooltip``（路由装饰器 summary、pydantic Field/Query/Form 的 description、
    错误信封 message 等——装饰器也是 Call，天然覆盖）；
  - 位置参数消息载体：``ApiError(code, status, message, ...)`` 与
    ``_envelope(trace_id, code, message, ...)`` 的第 3 个位置参数；
  - f-string 只取常量字面段拼接（``f"评测任务不存在: {job_id}"`` 取中文前缀段）。
- 判定英文：片段含连续 ≥2 个英文字母且不含任何汉字；先剔除白名单专有名词
  （SKU/MOQ/UUID/trace_id 等，单一事实源：platform/core/i18n/messages.ZH_WHITELIST）
  再判定；协议成功消息 ``ok``（04 §1.3 约定不渲染给用户）显式放行。
- 输出：``文件:行号: [载体] 文案`` 中文清单；``--strict`` 时命中即退出码 1
  （CI 门禁形态；缺省报告形态退出码 0，起步版先观察再收紧）。

用法：python3 tools/usability/zh_scan.py [--roots services/kb/api platform/core]
        [--strict] [--out <报告路径>]
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# 脚本直跑自举：把仓库根加入 sys.path（导入术语表白名单单一事实源）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:  # 单一事实源优先；独立拷贝运行时回落到本地基线白名单
    from platform.core.i18n.messages import ZH_WHITELIST as _WHITELIST
except Exception:  # noqa: BLE001  # 独立运行兜底（正常仓库内不会走到）
    _WHITELIST = frozenset({
        "SKU", "MOQ", "API", "URL", "ID", "TopK", "CSV", "Excel", "PDF", "OCR", "ASR", "JWT",
        "RBAC", "RAG", "LLM", "SaaS", "Logo", "Webhook", "UTC", "G", "MB", "GB", "TB",
        "UUID", "UID", "trace_id", "Milvus", "Qdrant", "DeepSeek", "BGE-M3", "OpenClaw",
    })

#: 缺省扫描根（管理 API 层 + 平台公共层；前端目录随 kb-admin-web 迭代加入）
DEFAULT_ROOTS: tuple[str, ...] = ("services/kb/api", "platform/core")

#: 用户可见文案的关键字参数名（取自 SR-NFR-USA-02 §6.5 可见属性的后端对应物）
USER_FACING_KWARGS: frozenset[str] = frozenset({
    "message", "msg", "summary", "description", "label", "title", "placeholder", "detail", "tooltip",
})

#: 位置参数消息载体：函数名 → message 所在参数下标（0 起）
POSITIONAL_MESSAGE_FUNCS: dict[str, int] = {"ApiError": 2, "_envelope": 2, "envelope": 2}

#: 协议约定放行的精确文案（04 §1.3：code=0 的 message 恒 "ok"，不渲染给用户）
ALLOWED_EXACT: frozenset[str] = frozenset({"ok"})

#: 连续 ≥2 个英文字母（判定英文的最小信号）
_RE_ENGLISH = re.compile(r"[A-Za-z]{2,}")
#: 任意汉字（含汉字即视为中文文案，不判英文）
_RE_HAN = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True)
class Finding:
    """一处疑似英文残留。"""

    file: str    # 相对仓库根的文件路径
    line: int    # 行号
    carrier: str # 载体（关键字参数名 / 位置参数函数名）
    text: str    # 命中文案

    def format(self) -> str:
        """报告行：``文件:行号: [载体] 文案``。"""
        return f"{self.file}:{self.line}: [{self.carrier}] {self.text}"


def _literal_text(node: ast.expr) -> str | None:
    """表达式 → 用户可见字符串字面量：常量原样；f-string 取常量段拼接；其余 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [part.value for part in node.values
                 if isinstance(part, ast.Constant) and isinstance(part.value, str)]
        return "".join(parts) if parts else None
    return None


def _call_name(node: ast.Call) -> str:
    """调用目标的短名：``ApiError(...)``/``self.raise_api(...)`` → "ApiError"/"raise_api"。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class _UserStringExtractor(ast.NodeVisitor):
    """单文件 AST 遍历：收集面向用户的字符串字面量。"""

    def __init__(self, source: str) -> None:
        self._source = source
        self.findings: list[tuple[str, int, str]] = []  # (载体, 行号, 文案)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        for kw in node.keywords:
            if kw.arg in USER_FACING_KWARGS:
                text = _literal_text(kw.value)
                if text is not None:
                    self.findings.append((kw.arg, kw.value.lineno, text))
        index = POSITIONAL_MESSAGE_FUNCS.get(name)
        if index is not None and len(node.args) > index:
            text = _literal_text(node.args[index])
            if text is not None:
                self.findings.append((name, node.args[index].lineno, text))
        self.generic_visit(node)


def _display_path(py_file: Path, repo_root: Path) -> str:
    """展示路径：仓库内取相对路径，仓库外（自定义 --roots）取绝对路径。"""
    try:
        return str(py_file.relative_to(repo_root))
    except ValueError:
        return str(py_file)


def extract_user_strings(py_file: Path, repo_root: Path) -> list[tuple[str, int, str]]:
    """解析单文件 → (载体, 行号, 文案) 列表（解析失败抛 SyntaxError，由调用方处置）。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    extractor = _UserStringExtractor(_display_path(py_file, repo_root))
    extractor.visit(tree)
    return extractor.findings


def is_english_residue(text: str, whitelist: Iterable[str] | None = None) -> bool:
    """英文残留判定：含汉字 → 非英文；剔除白名单词后仍含连续 ≥2 英文字母 → 残留。

    白名单词按长度降序剔除（长词优先，避免 "BGE-M3" 被 "M3" 类短词截断干扰）；
    匹配大小写不敏感、词边界约束（``trace_id`` 不会被 ``ID`` 误伤）。
    """
    if _RE_HAN.search(text):
        return False
    if text.strip().lower() in ALLOWED_EXACT:
        return False
    cleaned = text
    for word in sorted(whitelist if whitelist is not None else _WHITELIST, key=len, reverse=True):
        cleaned = re.sub(rf"(?i)\b{re.escape(word)}\b", " ", cleaned)
    return bool(_RE_ENGLISH.search(cleaned))


def scan_directory(
    roots: Sequence[str | Path], repo_root: Path, *, whitelist: Iterable[str] | None = None
) -> list[Finding]:
    """扫描多个根目录下的 .py 文件 → 疑似英文残留清单（文件/行号排序）。"""
    findings: list[Finding] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_absolute():
            root_path = repo_root / root_path  # 相对根一律按仓库根解析（与缺省口径一致）
        if not root_path.is_dir():
            continue
        for py_file in sorted(root_path.rglob("*.py")):
            try:
                user_strings = extract_user_strings(py_file, repo_root)
            except SyntaxError:
                continue  # 非法语法文件交给 CI 其他门禁；中文扫描不因个别文件中断
            for carrier, lineno, text in user_strings:
                if is_english_residue(text, whitelist):
                    findings.append(
                        Finding(file=_display_path(py_file, repo_root), line=lineno,
                                carrier=carrier, text=text.strip())
                    )
    findings.sort(key=lambda f: (f.file, f.line))
    return findings


def render_report(findings: Sequence[Finding], roots: Sequence[str], scanned: int) -> str:
    """中文扫描报告。"""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("界面文案中文扫描报告（NFR-USA-02 起步版·后端源码，迭代 0.3）")
    lines.append(f"扫描范围：{', '.join(str(r) for r in roots)}（{scanned} 个 .py 文件）")
    lines.append(f"白名单：platform/core/i18n/messages.ZH_WHITELIST（{len(_WHITELIST)} 词）"
                 f"+ 协议放行 {sorted(ALLOWED_EXACT)}")
    lines.append("=" * 78)
    if findings:
        lines.append(f"疑似英文残留 {len(findings)} 处（文案应改为中文或经 i18n 映射中心下发）：")
        lines.extend(f"  {finding.format()}" for finding in findings)
        lines.append("处置：面向用户的 message/summary/description/label 改写为中文；")
        lines.append("      专有名词确需保留英文的，在 ZH_WHITELIST 登记并说明理由（评审后加入）。")
    else:
        lines.append("疑似英文残留 0 处：扫描范围内面向用户的字符串字面量均为中文（含白名单专有名词）。")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：扫描 → 中文报告；退出码 0=通过/报告形态，``--strict`` 且命中 → 1。"""
    parser = argparse.ArgumentParser(description="界面文案中文扫描（NFR-USA-02 起步版）")
    parser.add_argument("--roots", nargs="*", default=list(DEFAULT_ROOTS),
                        help="扫描根目录（缺省 services/kb/api platform/core）")
    parser.add_argument("--strict", action="store_true",
                        help="门禁形态：命中英文残留即退出码 1（CI 收紧用）")
    parser.add_argument("--out", default=None, help="报告落盘路径（缺省仅打印）")
    args = parser.parse_args(list(argv if argv is not None else sys.argv[1:]))

    findings = scan_directory(args.roots, _REPO_ROOT)
    scanned = sum(
        1 for root in args.roots for _ in Path(root).rglob("*.py") if Path(root).is_dir()
    )
    report = render_report(findings, roots=args.roots, scanned=scanned)
    print(report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report + "\n", encoding="utf-8")
        print(f"报告已写入：{out_path}")
    if findings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
