# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""无 SQL/命令行覆盖核对（FR-KB-023 门禁一，迭代 0.3 起步版）。

把「全部管理操作无需 SQL/命令行」从口号变成机器可判定：
1. 以 **AST 提取** services/kb/api/ 下全部路由定义（不实例化 FastAPI，零依赖零副作用），
   生成「管理操作 → API 映射清单」——覆盖 ``router = APIRouter(prefix=...)`` +
   ``@router.get/post/...`` 装饰器、模块级路径常量（如 ``_ROUTE_TRIGGER``）、以及
   工厂函数内 ``@app.get(path)`` 形态（路径取函数参数默认值，如 /internal/metrics）；
2. 与 operations.yaml 声明的运维操作清单比对：
   - 声明了但无 API 支撑 → ``[缺API]`` 缺口（该操作只能 SQL/命令行兜底 = 红线）；
   - 实际路由未登记清单 → ``[漏洞]``（防新功能绕过登记）；
   - steps > 3 → ``[超3步]``（违反 SR-KB-023 §6.3 操作路径 ≤3 步规范）；
3. 输出中文核对报告（stdout，可 ``--out`` 落盘）；任一缺口/漏洞/超步 → 退出码 1。

用法：python3 tools/usability/coverage_check.py [--api-root services/kb/api]
        [--ops tools/usability/operations.yaml] [--out <报告路径>]
"""
from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# 脚本直跑自举：把仓库根加入 sys.path 以复用 tools.usability 包内共享模块
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.usability.ops_registry import (  # noqa: E402
    STEPS_LIMIT,
    Operation,
    load_operations,
    normalize_path,
)

#: HTTP 方法集合（装饰器函数名 → 大写方法）
HTTP_METHODS: frozenset[str] = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})

#: 不属管理操作面的路径前缀（内部监控通道，豁免登记，报告中单独列出）
EXEMPT_PREFIXES: tuple[str, ...] = ("/internal/",)


@dataclass(frozen=True)
class Endpoint:
    """AST 提取出的一条路由定义。"""

    method: str   # HTTP 方法（大写）
    path: str     # 完整路径模板（prefix + 装饰器路径）
    source: str   # 定义所在文件（相对仓库根）
    func: str     # 处理函数名

    @property
    def api(self) -> str:
        """``METHOD /path`` 形态（与 operations.yaml 的 api 字段同构）。"""
        return f"{self.method} {self.path}"


@dataclass
class CoverageResult:
    """核对结论：映射行 + 三类问题清单。"""

    rows: list[dict[str, str]] = field(default_factory=list)      # 每操作一行（含核对结果）
    gaps: list[str] = field(default_factory=list)                 # [缺API] 声明无 API 支撑
    wild_routes: list[str] = field(default_factory=list)          # [漏洞] 路由未登记清单
    oversteps: list[str] = field(default_factory=list)            # [超3步] 步数违规
    exempt_routes: list[str] = field(default_factory=list)        # 豁免的内部端点（不计漏洞）
    endpoints: list[Endpoint] = field(default_factory=list)       # AST 提取的全部端点

    @property
    def problems(self) -> list[str]:
        """全部问题清单（缺口 + 漏洞 + 超步，决定退出码）。"""
        return [*self.gaps, *self.wild_routes, *self.oversteps]

    @property
    def passed(self) -> bool:
        """通过判定：无缺口、无未登记路由、无超步操作。"""
        return not self.problems


# --------------------------------------------------------------------------- AST 路由提取


class _RouteExtractor(ast.NodeVisitor):
    """从单个模块 AST 提取路由定义。

    名称解析范围（作用域栈）：模块级常量 + 外层/本层函数参数的常量默认值；
    路由前缀：模块级 ``xxx = APIRouter(prefix=...)`` 赋值登记的变量名 → 前缀。
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._module_names: dict[str, str] = {}
        self._router_prefixes: dict[str, str] = {}
        self.endpoints: list[Endpoint] = []
        self._scopes: list[dict[str, str]] = [self._module_names]

    # -- 入口 -------------------------------------------------------------

    def extract(self, tree: ast.Module) -> list[Endpoint]:
        """先收集模块级名称/路由前缀，再遍历函数装饰器。"""
        for stmt in tree.body:
            self._collect_module_assign(stmt)
        for stmt in tree.body:
            self.visit(stmt)
        return self.endpoints

    # -- 模块级赋值收集 ----------------------------------------------------

    def _collect_module_assign(self, stmt: ast.stmt) -> None:
        """模块级常量（含带注解赋值）与 APIRouter 前缀登记。"""
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            return
        if not isinstance(target, ast.Name):
            return
        prefix = self._router_prefix_of(value)
        if prefix is not None:
            self._router_prefixes[target.id] = prefix
            return
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            self._module_names[target.id] = value.value

    def _router_prefix_of(self, value: ast.expr) -> str | None:
        """表达式为 ``APIRouter(...)`` 调用时返回其 prefix（缺省空串），否则 None。"""
        if not isinstance(value, ast.Call):
            return None
        func = value.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else ""
        )
        if name != "APIRouter":
            return None
        for kw in value.keywords:
            if kw.arg == "prefix":
                resolved = self._resolve_str(kw.value, self._module_names)
                return resolved if resolved is not None else ""
        return ""

    # -- 名称解析 ----------------------------------------------------------

    @staticmethod
    def _resolve_str(node: ast.expr, scope: dict[str, str]) -> str | None:
        """节点解析为字符串常量：直接常量，或作用域内可解析的名称。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return scope.get(node.id)
        return None

    # -- 函数遍历（装饰器在定义处的外层作用域求值） -------------------------

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self._handle_decorator(decorator, node.name)
        inner: dict[str, str] = dict(self._scopes[-1])
        args = list(node.args.posonlyargs) + list(node.args.args)
        defaults = list(node.args.defaults)
        if defaults:
            for arg, default in zip(args[len(args) - len(defaults):], defaults):
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    inner[arg.arg] = default.value
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None and isinstance(default, ast.Constant) and isinstance(default.value, str):
                inner[arg.arg] = default.value
        self._scopes.append(inner)
        for stmt in node.body:
            self.visit(stmt)
        self._scopes.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # -- 装饰器 → 端点 -----------------------------------------------------

    def _handle_decorator(self, decorator: ast.expr, func_name: str) -> None:
        if not isinstance(decorator, ast.Call):
            return
        func = decorator.func
        if not isinstance(func, ast.Attribute) or func.attr not in HTTP_METHODS:
            return
        if not decorator.args:
            return  # 无路径参数的裸装饰器不是路由注册
        scope = self._scopes[-1]
        path = self._resolve_str(decorator.args[0], scope)
        if path is None:
            return  # 路径动态拼接（如 include_router 场景）：交由运行时清单兜底，不误报
        receiver = func.value
        prefix = ""
        if isinstance(receiver, ast.Name):
            prefix = self._router_prefixes.get(receiver.id, "")
        self.endpoints.append(
            Endpoint(
                method=func.attr.upper(),
                path=f"{prefix}{path}",
                source=self._source,
                func=func_name,
            )
        )


def _display_path(py_file: Path, repo_root: Path) -> str:
    """展示路径：仓库内取相对路径，仓库外（自定义 --api-root）取绝对路径。"""
    try:
        return str(py_file.relative_to(repo_root))
    except ValueError:
        return str(py_file)


def extract_endpoints(api_root: str | Path, repo_root: str | Path) -> list[Endpoint]:
    """扫描 api_root 下全部 .py 文件的 AST，提取路由端点（按 path/method 排序）。"""
    root_path = Path(api_root)
    endpoints: list[Endpoint] = []
    for py_file in sorted(root_path.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        source = _display_path(py_file, Path(repo_root))
        endpoints.extend(_RouteExtractor(source).extract(tree))
    endpoints.sort(key=lambda ep: (ep.path, ep.method))
    return endpoints


# --------------------------------------------------------------------------- 比对与报告


def check_coverage(operations: Sequence[Operation], endpoints: Sequence[Endpoint]) -> CoverageResult:
    """声明清单 ⇄ 实际端点比对：两轮匹配（先精确后模板参数归一化）。"""
    result = CoverageResult(endpoints=list(endpoints))
    exact_index = {ep.api: ep for ep in endpoints}
    norm_index: dict[tuple[str, str], Endpoint] = {}
    for ep in endpoints:
        norm_index.setdefault((ep.method, normalize_path(ep.path)), ep)

    matched_endpoints: set[Endpoint] = set()
    for op in operations:
        endpoint = exact_index.get(op.api)
        matched_via = "精确匹配"
        if endpoint is None:
            endpoint = norm_index.get((op.method, normalize_path(op.path)))
            matched_via = "模板参数归一匹配"
        if endpoint is None:
            result.gaps.append(f"[缺API] {op.code}（{op.name}）→ 声明 {op.api}，但路由中未找到支撑端点")
            result.rows.append(
                {"code": op.code, "name": op.name, "api": op.api, "source": "—", "steps": str(op.steps),
                 "danger": op.danger_label, "status": "缺API"}
            )
        else:
            matched_endpoints.add(endpoint)
            result.rows.append(
                {"code": op.code, "name": op.name, "api": op.api, "source": endpoint.source,
                 "steps": str(op.steps), "danger": op.danger_label, "status": f"已支撑（{matched_via}）"}
            )
        if op.steps > STEPS_LIMIT:
            result.oversteps.append(
                f"[超3步] {op.code}（{op.name}）steps={op.steps}，超过 ≤{STEPS_LIMIT} 步规范"
                "（SR-KB-023 §6.3，需收敛操作路径）"
            )

    for ep in endpoints:
        if ep in matched_endpoints:
            continue
        if ep.path.startswith(EXEMPT_PREFIXES):
            result.exempt_routes.append(f"{ep.api}（{ep.source}）—— 内部监控通道，豁免登记")
            continue
        result.wild_routes.append(
            f"[漏洞] 管理路由未登记 operations.yaml（界面无入口即只能命令行兜底）：{ep.api}（{ep.source}）"
        )
    return result


def render_report(
    operations: Sequence[Operation], result: CoverageResult, *, api_root: str, ops_path: str
) -> str:
    """核对报告（简体中文，表格驱动）。"""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("无 SQL/命令行覆盖核对报告（FR-KB-023 / NFR-USA-01，迭代 0.3）")
    lines.append(f"扫描路由目录：{api_root}    操作清单：{ops_path}")
    lines.append("=" * 78)
    lines.append("")
    lines.append(f"一、管理操作 → API 映射清单（共 {len(operations)} 项声明 / 提取 {len(result.endpoints)} 条路由）")
    lines.append(f"{'操作码':<24}{'操作名称':<20}{'API':<64}{'步数':<4}{'危险级':<14}核对结果")
    for row in result.rows:
        lines.append(
            f"{row['code']:<24}{row['name']:<20}{row['api']:<64}"
            f"{row['steps']:<4}{row['danger']:<14}{row['status']}"
        )
    lines.append("")
    lines.append("二、核对结论")
    lines.append(
        f"  - 声明操作 {len(operations)} 项：{len(operations) - len(result.gaps)} 项具备 API 支撑，"
        f"{len(result.gaps)} 项缺口"
    )
    lines.append(
        f"  - 实际路由 {len(result.endpoints)} 条：未登记管理路由 {len(result.wild_routes)} 条，"
        f"豁免内部端点 {len(result.exempt_routes)} 条"
    )
    overstep_count = len(result.oversteps)
    lines.append(f"  - 步数核对：{overstep_count} 项超过 {STEPS_LIMIT} 步上限" if overstep_count
                 else f"  - 步数核对：全部操作 ≤{STEPS_LIMIT} 步")
    if result.exempt_routes:
        for item in result.exempt_routes:
            lines.append(f"  - 豁免登记：{item}")
    lines.append(f"  - 总判定：{'通过（无缺口 / 无漏洞 / 无超步）' if result.passed else '不通过'}")
    lines.append("")
    if result.problems:
        lines.append("三、问题清单（需整改后复跑）")
        lines.extend(f"  {idx}. {problem}" for idx, problem in enumerate(result.problems, start=1))
        lines.append("")
        lines.append("处置：[缺API] 补齐 API 或修正 operations.yaml 声明；[漏洞] 同步登记操作清单；")
        lines.append("      [超3步] 收敛界面操作路径（SR-KB-023 §6.3）。")
    else:
        lines.append("三、问题清单：无")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI 入口


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="无 SQL/命令行覆盖核对（FR-KB-023）")
    parser.add_argument("--api-root", default=str(_REPO_ROOT / "services" / "kb" / "api"),
                        help="路由定义目录（默认 services/kb/api）")
    parser.add_argument("--ops", default=str(_REPO_ROOT / "tools" / "usability" / "operations.yaml"),
                        help="运维操作声明清单（默认 tools/usability/operations.yaml）")
    parser.add_argument("--out", default=None, help="报告落盘路径（缺省仅打印）")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：提取端点 → 装载清单 → 比对 → 中文报告；退出码 0=通过 / 1=存在缺口。"""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    api_root = Path(args.api_root)
    if not api_root.is_dir():
        print(f"[错误] 路由目录不存在：{api_root}（覆盖核对输入无效）")
        return 1
    try:
        operations = load_operations(args.ops)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] 操作清单装载失败：{exc}")
        return 1
    endpoints = extract_endpoints(api_root, _REPO_ROOT)
    result = check_coverage(operations, endpoints)
    report = render_report(operations, result, api_root=str(api_root), ops_path=args.ops)
    print(report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report + "\n", encoding="utf-8")
        print(f"报告已写入：{out_path}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
