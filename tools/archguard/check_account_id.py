# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# check_account_id.py —— 架构守护起步版（NFR-SCA-01，SR-NFR-SCA-01 §6.2 检查①/② 子集）：
# 多账号维度不变式的静态扫描，保证「P2 单账号 → P4 28 账号集群无需推倒重来」从设计
# 口径变成 CI 可阻断的工程约束；完整 ArchGuard（OpenAPI/import 图/环境对账）随迭代落位。
"""扫描 services/ 与 migrations/ 的 SQL/ORM 文本，执行两类不变式检查：

INV-1a（数据层，blocker）：新建业务表 DDL 必须含 account_id（或 P5 多租户 tenant_id）
  列——缺列即告警退出 1。豁免：知识域共享白名单表（account_id=0 表全局）与
  arch_/app_config/alembic_ 前缀治理表（与 SR-NFR-SCA-01 §6.2 SHARED_TABLES 同源）。
  识别对象：*.sql 的 CREATE TABLE 语句；*.py 中的 CREATE TABLE 字符串与 alembic
  op.create_table(...) ORM 调用（括号配平截取建表体后判定）。

INV-2b（服务层，blocker）：硬编码单账号常量扫描——形如 account_id = 1 / tenant_id = 2 /
  ACCOUNT_ID = 3 的魔数赋值（含 SQL 字符串内的 WHERE account_id = 1）即告警退出 1；
  账号维度必须由配置/上下文注入（SR-SC-001 instances.yaml → agent_instance）。
  豁免：纯注释行；目标行或其上一行携带「# arch-guard: exempt <理由+CR号>」显式标注。

用法：python tools/archguard/check_account_id.py   # 无参：扫描 <仓根>/services 与 <仓根>/migrations
      python tools/archguard/check_account_id.py <目录> [<目录> ...]   # 元测试用：显式指定目录
退出码：0=零违例；1=存在违例（目录未创建视为跳过并提示，不算失败——services/migrations
      按迭代逐步落位，见仓库 README §结构）。
"""

import re
import sys
from pathlib import Path

# 仓根定位：本脚本位于 <仓根>/tools/archguard/，向上三级
REPO_ROOT = Path(__file__).resolve().parents[2]

# 知识域共享表白名单（SR-NFR-SCA-01 §6.2/§7 data.shared_tables 同源，随 03-数据库设计 §3.1 同步维护）
SHARED_TABLES = {
    "knowledge_item",
    "knowledge_chunk",
    "skill_library",
    "negative_case",
    "process_library",
    "ingest_pipeline_log",
}
# 治理/配置类表前缀白名单：架构治理与配置中心表无账号维度语义
TABLE_PREFIX_WHITELIST = ("arch_", "app_config", "alembic_")

# SQL 建表语句识别（容忍 IF NOT EXISTS 与引号/反引号包表名）
CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)[\"'`\]]?", re.IGNORECASE
)
# alembic ORM 建表调用识别：op.create_table("table_name", ...)
OP_CREATE_TABLE_RE = re.compile(r"op\.create_table\(\s*[\"'`](\w+)[\"'`]")
# 账号维度列判定：建表体内出现 account_id 或 tenant_id 即满足（P5 起叠加 tenant_id，NFR-SCA-02）
ACCOUNT_COLUMN_RE = re.compile(r"\b(?:account_id|tenant_id)\b", re.IGNORECASE)
# 硬编码单账号魔数赋值：account_id = 1 等（负向后顾排除 ==/>=/<=/!= 比较与标识符粘连）
ACCOUNT_CONST_RE = re.compile(r"(?<![=!<>+%*/\w])(?:account_id|tenant_id)\s*=\s*\d+\b", re.IGNORECASE)
# 显式豁免标注（与 SR-NFR-SCA-01 §5 豁免约定一致，须附理由与 CR 号）
EXEMPT_MARK = "arch-guard: exempt"


def table_whitelisted(table: str) -> bool:
    """白名单判定：知识域共享表（account_id=0 表全局）或治理表前缀。"""
    lowered = table.lower()
    return lowered in SHARED_TABLES or lowered.startswith(TABLE_PREFIX_WHITELIST)


def extract_balanced(text: str, open_idx: int) -> str:
    """从 open_idx（'(' 位置）起做括号配平，返回括号内文本；未闭合返回空串。

    用于从 CREATE TABLE ... ( ... ) 与 op.create_table( ... ) 中截取建表体，
    比「截到第一个 )」更稳：列定义内嵌 CHECK/ENUM 子括号不会提前截断。
    """
    depth = 0
    for i in range(open_idx, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    return ""


def scan_ddl_text(text: str, origin: str) -> list[str]:
    """INV-1a：扫描文本中的 CREATE TABLE（SQL 文件与 .py 内嵌 SQL 字符串通吃）。

    新建业务表建表体缺 account_id/tenant_id 列 → 告警；白名单表跳过。
    起步版 DDL 无行级豁免——共享表走白名单登记，其余表缺列即违例（修复优先于豁免）。
    """
    warnings: list[str] = []
    for m in CREATE_TABLE_RE.finditer(text):
        table = m.group(1)
        if table_whitelisted(table):
            continue
        open_idx = text.find("(", m.end())
        if open_idx == -1:
            continue
        body = extract_balanced(text, open_idx)
        if not body:
            continue
        if not ACCOUNT_COLUMN_RE.search(body):
            warnings.append(
                f"[INV-1a][告警] {origin}: 业务表 {table} 缺 account_id/tenant_id 列"
                f"（NFR-SCA-01：P2 起新增业务表必须带账号维度；知识域共享表请登记白名单）"
            )
    return warnings


def scan_orm_ddl_text(text: str, origin: str) -> list[str]:
    """INV-1a（ORM 形态）：扫描 alembic 迁移 .py 中的 op.create_table(...) 调用。

    建表体取括号配平区间，sa.Column("account_id", ...) / sa.Column("tenant_id", ...)
    存在即满足；白名单与告警文案与 SQL 形态一致。
    """
    warnings: list[str] = []
    for m in OP_CREATE_TABLE_RE.finditer(text):
        table = m.group(1)
        if table_whitelisted(table):
            continue
        # op.create_table 的开括号位于表名之前（匹配区间内），据此做括号配平取建表体
        open_idx = text.find("(", m.start(), m.end())
        if open_idx == -1:
            continue
        body = extract_balanced(text, open_idx)
        if not body:
            continue
        if not ACCOUNT_COLUMN_RE.search(body):
            warnings.append(
                f"[INV-1a][告警] {origin}: alembic 建表 {table} 缺 account_id/tenant_id 列"
                f"（NFR-SCA-01：迁移新增业务表必须带账号维度）"
            )
    return warnings


def scan_hardcoded_lines(lines: list[str], origin: str) -> list[str]:
    """INV-2b：逐行扫描硬编码单账号魔数赋值（account_id = 1 等，含 SQL 字符串内字面量）。

    豁免三类：纯 # 注释行；本行携带 arch-guard: exempt 标注；上一行携带标注
    （标注格式：# arch-guard: exempt <理由+CR号>，缺失 CR 号属流程问题由评审把关）。
    """
    warnings: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if EXEMPT_MARK in line or (i > 0 and EXEMPT_MARK in lines[i - 1]):
            continue
        if ACCOUNT_CONST_RE.search(line):
            preview = stripped[:60] + ("…" if len(stripped) > 60 else "")
            warnings.append(
                f"[INV-2b][告警] {origin}:{i + 1}: 硬编码账号常量「{preview}」"
                f"（NFR-SCA-01：账号维度应由配置/上下文注入，禁止魔数；"
                f"如确需豁免，行前注释 # arch-guard: exempt <理由+CR号>）"
            )
    return warnings


def scan_directory(root: Path) -> list[str]:
    """扫描单个目录：*.sql 做 DDL+魔数检查，*.py 额外做 op.create_table 检查。"""
    warnings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sql"}:
            continue
        if any(part in {"__pycache__", ".venv", "node_modules"} for part in path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        warnings.extend(scan_ddl_text(text, str(path)))
        if path.suffix == ".py":
            warnings.extend(scan_orm_ddl_text(text, str(path)))
        warnings.extend(scan_hardcoded_lines(text.splitlines(), str(path)))
    return warnings


def main(argv: list[str]) -> int:
    """门禁入口：无参按仓根默认目录扫描（CI 形态）；有参按显式目录扫描（元测试形态）。"""
    dirs = [Path(a) for a in argv] if argv else [REPO_ROOT / "services", REPO_ROOT / "migrations"]
    warnings: list[str] = []
    for d in dirs:
        if not d.exists():
            print(f"[提示] 目录未创建，跳过：{d}（services/migrations 随迭代落位，非门禁失败）")
            continue
        warnings.extend(scan_directory(d))

    if warnings:
        print(f"[不通过] NFR-SCA-01 架构守护发现 {len(warnings)} 处违例（阻断合入）：")
        print("\n".join(f"  {w}" for w in warnings))
        return 1
    print("[通过] NFR-SCA-01 架构守护：services/migrations 扫描零违例（业务表账号维度完整，无硬编码账号常量）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
