# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# check_headers.py —— 版权头门禁：COM-IP-01（版权头模板）/ COM-IP-04（中文注释规范），
# 与 SR-NFR-MAI-01 §6（CI 阻断级门禁）联动；每次提交强制执行。
"""扫描目录下全部 .py 文件：文件头 5 行注释内必须含「知识产权归甲方所有」。

判定规则（SR-COM-IP-04 §6.1 文件头样板）：文件头 5 行中属于注释语义的文本——
以 # 开头的行注释，以及模块 docstring（三引号块）内的行——拼接后须包含版权标记。
空文件（0 字节或纯空白，如纯包标记的 __init__.py）跳过：无内容可标注版权。

用法：python tools/ci_gates/check_headers.py [--fix] <目录> [<目录> ...]
  --fix  对缺头文件自动补写标准版权头（shebang 之后插入），补写后即刻合规；
         用于存量一次性整改，不改变门禁判定口径。
退出码：0=全部合规；1=存在缺头文件（或参数/目录错误）。
"""

import sys
from pathlib import Path

# 版权标记：与 COM-IP-01 版权头模板（tools/templates，随迭代落位）保持同一措辞
COPYRIGHT_MARK = "知识产权归甲方所有"
# 只检查文件头前 5 行：头部规范区，超出即视为未按模板落头
HEADER_LINE_LIMIT = 5
# --fix 自动补写的标准版权头（两行，插入后必在 5 行内命中标记）
FIX_HEADER = (
    f"# {COPYRIGHT_MARK}（COM-IP-01）——AI 外贸销售智能体系统交付物。\n"
    "# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。\n"
)


def is_blank_file(path: Path) -> bool:
    """空文件判定：0 字节，或小体积纯空白文件（纯包标记 __init__.py）。

    这类文件没有任何代码与注释内容，不承载可主张版权的表达，跳过不判违规；
    一旦写入实质内容（哪怕一行注释）即恢复强制的版权头要求。
    """
    size = path.stat().st_size
    if size == 0:
        return True
    if size <= 1024:  # 小文件直接全文判空白；大文件不可能纯空白，不必读入
        return not path.read_text(encoding="utf-8", errors="replace").strip()
    return False


def extract_head_comments(path: Path) -> str:
    """提取文件头前 5 行中的注释语义文本：# 行注释与模块 docstring 块内行。

    docstring 识别采用行级三引号翻转：单行内出现成对三引号视为自闭合；
    奇数次出现视为块边界翻转。文件不可读时返回空串（由调用方记为违规）。
    """
    texts: list[str] = []
    in_doc = False
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(HEADER_LINE_LIMIT), fh):
                stripped = line.strip()
                if not in_doc and stripped.startswith("#"):
                    texts.append(stripped.lstrip("#"))
                    continue
                for quote in ('"""', "'''"):
                    if quote in stripped:
                        # 单行自闭合 docstring：整行计入且不改变块状态；否则翻转块状态
                        if stripped.count(quote) >= 2:
                            texts.append(stripped)
                        else:
                            in_doc = not in_doc
                            texts.append(stripped)
                        break
                else:
                    if in_doc:  # 处于 docstring 块内：整行计入头部注释文本
                        texts.append(stripped)
    except OSError:
        return ""
    return "\n".join(texts)


def fix_header(path: Path) -> None:
    """为缺头文件自动补写标准版权头：shebang 之后、其余内容之前插入。

    以 # 注释形式插入而非 docstring，避免挤占模块 docstring（其首行
    按 SR-COM-IP-04 §6.1 承载模块职责说明，不应被版权头占用）。
    """
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    fixed = "".join(lines[:insert_at]) + FIX_HEADER + "".join(lines[insert_at:])
    path.write_text(fixed, encoding="utf-8")


def main(argv: list[str]) -> int:
    """门禁入口：逐目录扫描 .py 文件头；--fix 修复后复检；退出码 0/1 供 CI 判定。"""
    fix = "--fix" in argv
    dirs = [a for a in argv if not a.startswith("-")]
    if not dirs:
        print("用法: python tools/ci_gates/check_headers.py [--fix] <目录> [<目录> ...]")
        return 1

    missing: list[str] = []
    scanned = 0
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            print(f"[错误] 目录不存在：{d}（COM-IP-01 版权头门禁输入无效）")
            return 1
        for py in sorted(root.rglob("*.py")):
            if is_blank_file(py):
                continue
            scanned += 1
            if COPYRIGHT_MARK in extract_head_comments(py):
                continue
            if fix:
                fix_header(py)
                if COPYRIGHT_MARK not in extract_head_comments(py):  # 双保险：修复必然合规
                    missing.append(str(py))
                else:
                    print(f"[修复] 已补写版权头：{py}")
            else:
                missing.append(str(py))

    if missing:
        print(f"[不通过] 以下 {len(missing)} 个文件头 5 行注释内缺少「{COPYRIGHT_MARK}」：")
        for p in missing:
            print(f"  - {p}")
        print("处置：补写标准版权头，或执行 python tools/ci_gates/check_headers.py --fix <目录> 自动补头。")
        return 1
    print(f"[通过] 版权头门禁（COM-IP-01/04）：扫描 {scanned} 个 .py 文件全部合规（空文件已跳过）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
