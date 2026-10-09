# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# license_gate.py —— 开源许可证门禁（COM-IP-05）：未审查登记的第三方包禁止引入。
"""校验 pyproject.toml 主 dependencies 的包名是否全部在允许清单内。

允许清单两层（SR-COM-IP-05 依赖审查口径）：
  1) 内置默认白名单：项目起步即核定许可证的常用包（fastapi/pydantic/openpyxl/
     python-docx/pytest/httpx/ruff/uvicorn）；
  2) LICENSES.md 登记追加：依赖审查（许可证兼容性 + 用途）通过后的包登记进
     仓根 LICENSES.md，本门禁自动并入白名单。
范围说明（起步版）：只校验 [project].dependencies（运行时主依赖）；
optional-dependencies 的重型按需依赖（pdf/ocr/asr）与 dev 组待各自完成依赖
审查并在 LICENSES.md 登记后，由后续迭代纳入强校验。

用法：python tools/ci_gates/license_gate.py [<目录>]   # 目录缺省为当前目录（CI 于仓根执行）
退出码：0=全部在白名单；1=存在未登记依赖或配置错误（中文提示走依赖审查流程）。
"""

import re
import sys
import tomllib
from pathlib import Path

# 内置默认白名单：迭代 0.1 起步核定（许可证均为 MIT/Apache-2.0/BSD 等宽松协议）
BUILTIN_ALLOWLIST = {
    "fastapi",
    "pydantic",
    "openpyxl",
    "python-docx",
    "pytest",
    "httpx",
    "ruff",
    "uvicorn",
}

# PEP 503 规范化后比较：python_docx / python-docx / python.docx 视为同一包
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def normalize(name: str) -> str:
    """PEP 503 包名规范化：小写并把 - _ . 的连续段折叠为单个 -。"""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def dep_name(dep: str) -> str:
    """从 PEP 508 依赖声明（如 fastapi>=0.110、python-docx~=1.1）提取纯包名。

    环境标记（; python_version ...）与 extras（[all]）不影响首段包名提取；
    提取失败返回空串，由调用方按「无法解析」上报。
    """
    m = _NAME_RE.match(dep.strip())
    return m.group(0) if m else ""


def load_licenses_md(path: Path) -> set[str]:
    """解析 LICENSES.md 登记的追加包名：表格行首列或列表项首词。

    支持两种登记形态（任选其一）：
      | 包名 | 许可证 | 用途 | 审查记录 |
      - 包名：许可证与用途说明
    表头行/分隔行（首列为中文或 ---）不匹配包名正则，自动忽略；文件不存在视为空集。
    """
    if not path.exists():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        cell = ""
        if s.startswith("|"):
            parts = [p.strip() for p in s.strip("|").split("|")]
            cell = parts[0] if parts else ""
        elif s.startswith(("-", "*")):
            rest = s.lstrip("-*").strip()
            cell = rest.split()[0] if rest else ""
        cell = cell.strip("`").strip()
        if cell and _NAME_RE.fullmatch(cell):
            names.add(normalize(cell))
    return names


def main(argv: list[str]) -> int:
    """门禁入口：读 <目录>/pyproject.toml 主依赖 × （内置白名单 + LICENSES.md 追加）。"""
    if argv:
        # 显式指定目录（元测试形态）：目录即输入，缺 pyproject.toml 直接报错不回退
        root = Path(argv[0])
        pyproject = root / "pyproject.toml"
    else:
        # 无参（CI 形态）：当前目录；不在仓根执行时回退到脚本所在仓根
        root = Path(".")
        pyproject = root / "pyproject.toml"
        if not pyproject.exists():
            root = Path(__file__).resolve().parents[2]
            pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        print(f"[错误] 未找到 pyproject.toml（查找目录：{root}）——COM-IP-05 门禁以主依赖清单为输入。")
        return 1

    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"[错误] pyproject.toml 解析失败：{exc}")
        return 1

    deps = data.get("project", {}).get("dependencies", []) or []
    allow = BUILTIN_ALLOWLIST | load_licenses_md(root / "LICENSES.md")

    unknown: list[str] = []
    for dep in deps:
        name = dep_name(str(dep))
        if not name:
            unknown.append(f"{dep}（无法解析包名）")
        elif normalize(name) not in allow:
            unknown.append(name)

    if unknown:
        print("[不通过] 发现未登记开源依赖（COM-IP-05）：")
        for name in unknown:
            print(f"  - {name}")
        print("处置：先走依赖审查流程——核对许可证兼容性与用途，审查通过后在仓根 LICENSES.md")
        print("      登记该包（| 包名 | 许可证 | 用途 | 审查记录 |），再重新提交；未登记不得合入。")
        return 1

    print(
        f"[通过] 开源许可证门禁（COM-IP-05）：{len(deps)} 个主依赖全部在允许清单"
        f"（内置 {len(BUILTIN_ALLOWLIST)} 项 + LICENSES.md 追加 {len(allow - BUILTIN_ALLOWLIST)} 项）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
