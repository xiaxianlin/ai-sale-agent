# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# scan_secrets.py —— 密钥扫描门禁：NFR-SEC-04（凭证边界）/ SR-COM-IP-03 §6.7（凭证不入交付包）
# 的起步版实现；CI 每次提交对仓库文本文件做高危凭据模式零命中检查。
"""扫描目录下文本文件的高危凭据模式：命中即退出码 1（阻断）。

规则集（起步版，可按泄漏演练结论增补）：
  - 云厂商 AccessKeyId：AWS（AKIA 前缀 16 位大写数字字母）、阿里云（LTAI 前缀）
  - 平台个人令牌：GitHub ghp_ 令牌、通用 sk- 密钥（OpenAI 等）
  - 私钥文件头：-----BEGIN ... PRIVATE KEY-----
  - 凭据明文赋值：password/passwd/pwd/secret/token/api[_-]key = "字面值"（≥8 字符）
白名单：文件名为 .env.example（占位符模板，本身即脱敏形态的规范载体）。
跳过：.git 等版本库与缓存目录、二进制文件（含 NUL 字节）、超 2MB 大文件。
输出：只回显命中串前 4 字符（打码），确保门禁日志自身不泄露密钥。

用法：python tools/ci_gates/scan_secrets.py [<目录>]   # 目录缺省为当前目录（CI 于仓根执行）
退出码：0=零命中；1=存在命中（或参数错误）。
"""

import re
import sys
from pathlib import Path

# 高危凭据模式集：(规则名, 正则)。正则源码本身不匹配自身模式（已逐一核对避免自伤）
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS AccessKeyId", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("阿里云 AccessKeyId", re.compile(r"LTAI[0-9A-Za-z]{12,20}")),
    ("GitHub 个人令牌", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("通用 sk- 密钥", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("私钥文件头", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    (
        "凭据明文赋值",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
        ),
    ),
]

# 白名单文件名：占位符模板（交付包 deploy 目录的规范形态，见 SR-COM-IP-03 §6.1-02）
WHITELIST_NAMES = {".env.example"}
# 跳过的目录名：版本库内部/缓存/构建产物，扫描无意义且噪声大
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", "dist", "out", ".video_agent", ".mypy_cache",
}
# 单文件大小上限：超过视为数据/制品而非源码文本，跳过（性能保护）
MAX_FILE_BYTES = 2 * 1024 * 1024


def mask(matched: str) -> str:
    """打码预览：只回显命中串前 4 字符——门禁日志严禁回显完整密钥（安全设计 §4）。"""
    return matched[:4] + "****"


def iter_text_files(root: Path):
    """遍历待扫描文本文件：过滤白名单文件、跳过目录、二进制与超大文件。"""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if path.name in WHITELIST_NAMES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            with path.open("rb") as fh:
                head = fh.read(1024)
        except OSError:
            continue
        if b"\x00" in head:  # 含 NUL 字节判二进制（图片/权重/压缩包等）
            continue
        yield path


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """扫描单个文本文件：返回 (行号, 规则名, 打码命中串) 列表。"""
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hits
    for no, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in PATTERNS:
            m = pattern.search(line)
            if m:
                hits.append((no, rule, mask(m.group(0))))
    return hits


def main(argv: list[str]) -> int:
    """门禁入口：扫描目录并汇总命中；零命中退出 0，任何命中退出 1。"""
    root = Path(argv[0]) if argv else Path(".")
    if not root.is_dir():
        print(f"[错误] 目录不存在：{root}（密钥扫描门禁输入无效）")
        return 1

    total, hits = 0, []
    for path in iter_text_files(root):
        total += 1
        for no, rule, preview in scan_file(path):
            hits.append(f"  {path}:{no} [{rule}] {preview}")

    if hits:
        print(f"[不通过] 密钥扫描命中 {len(hits)} 处高危模式（NFR-SEC-04 / SR-COM-IP-03 §6.7）：")
        print("\n".join(hits))
        print("处置：真实凭证一律改由环境变量/密钥管理注入并立即轮换已泄漏凭证；")
        print("      模板类文件改名为 .env.example 并以占位符填充。")
        return 1
    print(f"[通过] 密钥扫描门禁：扫描 {total} 个文本文件零命中（.env.example 白名单已豁免）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
