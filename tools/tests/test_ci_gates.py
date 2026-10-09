# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# test_ci_gates.py —— 工程门禁元测试（SR-NFR-MAI-01 §9）：在临时目录构造违规/合规样例，
# 验证 check_headers / license_gate / scan_secrets / check_account_id 四个门禁的判定与退出码。
# 注意：本文件自身处于仓库密钥扫描范围内，全部违规样例（假密钥/假口令）必须以字符串拼接
#       构造，保证源码中不出现任何可被门禁命中的连续字面量。
"""门禁元测试：每个门禁覆盖「合规通过 / 违规拦截 / 豁免或修复」三类路径。

用例设计口径：门禁自身的可信性靠元测试保证（金样本回归）——门禁误放行与误阻断
同等是缺陷；样例一律写入 pytest tmp_path 临时目录，不污染仓库工作区。
"""

from pathlib import Path

from tools.archguard import check_account_id
from tools.ci_gates import check_headers, license_gate, scan_secrets

# 违规样例常量（拼接构造，源码中无连续命中字面量；均为公开文档示例值，非真实凭证）：
# _AWS_KEY: AWS 官方文档示例 AccessKeyId；_PRIVATE_KEY: PEM 私钥文件头；
# _PWD_LINE: password 明文赋值行；_ALI_KEY: 阿里云 AccessKeyId 前缀示例。
_AWS_KEY = "AK" + "IAIOSFODNN7EXAMPLE"
_PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"
_PWD_LINE = 'pass' + 'word = "Hunter2SecretX"'
_ALI_KEY = "LT" + "AIiosFODNN7EXAMPLE01"


# ─────────────────────────────── check_headers（版权头门禁） ───────────────────────────────


def test_check_headers_合规文件通过(tmp_path: Path) -> None:
    """文件头 5 行注释含「知识产权归甲方所有」→ 退出码 0。"""
    (tmp_path / "ok.py").write_text(
        "# 知识产权归甲方所有（COM-IP-01）——交付物。\n# 示例模块。\nx = 1\n", encoding="utf-8"
    )
    assert check_headers.main([str(tmp_path)]) == 0


def test_check_headers_缺头文件违规(tmp_path: Path) -> None:
    """有实质内容但头 5 行注释无版权标记 → 退出码 1。"""
    (tmp_path / "bad.py").write_text("# 只有一行普通注释的模块\ny = 2\n", encoding="utf-8")
    assert check_headers.main([str(tmp_path)]) == 1


def test_check_headers_docstring头部亦合规(tmp_path: Path) -> None:
    """版权标记写在模块 docstring 前 5 行内同样视为头部注释 → 通过。"""
    (tmp_path / "doc.py").write_text(
        '"""知识库服务——知识产权归甲方所有。\n\n模块职责：示例。\n"""\nz = 3\n', encoding="utf-8"
    )
    assert check_headers.main([str(tmp_path)]) == 0


def test_check_headers_空文件跳过(tmp_path: Path) -> None:
    """0 字节纯包标记 __init__.py 不判违规（无内容可标注版权）。"""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    assert check_headers.main([str(tmp_path)]) == 0


def test_check_headers_fix自动补头(tmp_path: Path) -> None:
    """--fix 对缺头文件自动补写版权头，修复后门禁即刻通过。"""
    target = tmp_path / "fixme.py"
    target.write_text("# 普通注释\nw = 4\n", encoding="utf-8")
    assert check_headers.main(["--fix", str(tmp_path)]) == 0
    assert check_headers.COPYRIGHT_MARK in target.read_text(encoding="utf-8")
    assert check_headers.main([str(tmp_path)]) == 0  # 复检：修复持久生效


# ─────────────────────────────── license_gate（许可证门禁） ───────────────────────────────


def _write_pyproject(root: Path, deps: list[str]) -> None:
    """在临时目录写入只含主 dependencies 的最小 pyproject.toml 样例。"""
    body = "[project]\nname = 'sample'\ndependencies = [\n"
    body += "".join(f"    '{d}',\n" for d in deps)
    body += "]\n"
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


def test_license_gate_白名单依赖通过(tmp_path: Path) -> None:
    """主依赖全部在内置白名单（含带版本约束的 PEP 508 形态）→ 退出码 0。"""
    _write_pyproject(tmp_path, ["fastapi>=0.110", "python-docx~=1.1"])
    assert license_gate.main([str(tmp_path)]) == 0


def test_license_gate_未登记依赖拦截(tmp_path: Path) -> None:
    """引入白名单之外的未知包 → 退出码 1（提示走 COM-IP-05 依赖审查）。"""
    _write_pyproject(tmp_path, ["fastapi>=0.110", "mystery-lib>=1.0"])
    assert license_gate.main([str(tmp_path)]) == 1


def test_license_gate_licenses_md登记追加放行(tmp_path: Path) -> None:
    """未知包经 LICENSES.md 登记（表格首列）后放行 → 退出码 0。"""
    _write_pyproject(tmp_path, ["fastapi>=0.110", "mystery-lib>=1.0"])
    (tmp_path / "LICENSES.md").write_text(
        "| 包名 | 许可证 | 用途 | 审查记录 |\n|---|---|---|---|\n"
        "| mystery-lib | MIT | 示例依赖 | CR-2026-001 |\n",
        encoding="utf-8",
    )
    assert license_gate.main([str(tmp_path)]) == 0


def test_license_gate_缺pyproject报错(tmp_path: Path) -> None:
    """目录下无 pyproject.toml（且无仓根可回退）→ 退出码 1。"""
    assert license_gate.main([str(tmp_path / "nowhere")]) == 1


# ─────────────────────────────── scan_secrets（密钥扫描门禁） ───────────────────────────────


def test_scan_secrets_干净目录通过(tmp_path: Path) -> None:
    """普通源码与配置不命中任何高危模式 → 退出码 0。"""
    (tmp_path / "app.py").write_text("PORT = 8000\nname = 'kb-service'\n", encoding="utf-8")
    assert scan_secrets.main([str(tmp_path)]) == 0


def test_scan_secrets_云密钥命中拦截(tmp_path: Path) -> None:
    """文本中出现 AWS 示例 AccessKeyId → 退出码 1。"""
    (tmp_path / "leak.txt").write_text(f"aws_key = '{_AWS_KEY}'\n", encoding="utf-8")
    assert scan_secrets.main([str(tmp_path)]) == 1


def test_scan_secrets_阿里云密钥命中拦截(tmp_path: Path) -> None:
    """文本中出现阿里云 AccessKeyId 前缀凭据 → 退出码 1。"""
    (tmp_path / "ali.txt").write_text(f"access_key_id: {_ALI_KEY}\n", encoding="utf-8")
    assert scan_secrets.main([str(tmp_path)]) == 1


def test_scan_secrets_私钥头命中拦截(tmp_path: Path) -> None:
    """文本中出现 PEM 私钥文件头 → 退出码 1。"""
    (tmp_path / "id_rsa").write_text(_PRIVATE_KEY + "\nMIIB...\n", encoding="utf-8")
    assert scan_secrets.main([str(tmp_path)]) == 1


def test_scan_secrets_口令明文赋值命中拦截(tmp_path: Path) -> None:
    """password = "字面值" 明文赋值形态 → 退出码 1。"""
    (tmp_path / "conf.ini").write_text(_PWD_LINE + "\n", encoding="utf-8")
    assert scan_secrets.main([str(tmp_path)]) == 1


def test_scan_secrets_env_example白名单豁免(tmp_path: Path) -> None:
    """.env.example（占位符模板）即使含口令形态也不判违规 → 退出码 0。"""
    (tmp_path / ".env.example").write_text(_PWD_LINE.replace("Hunter2SecretX", "<占位符>") + "\n", encoding="utf-8")
    assert scan_secrets.main([str(tmp_path)]) == 0


def test_scan_secrets_二进制文件跳过(tmp_path: Path) -> None:
    """含 NUL 字节的二进制文件（如权重/图片）跳过不扫描 → 退出码 0。"""
    (tmp_path / "model.bin").write_bytes(b"\x00\x01\x02" + _AWS_KEY.encode() + b"\x00")
    assert scan_secrets.main([str(tmp_path)]) == 0


# ─────────────────────────────── check_account_id（架构守护） ───────────────────────────────


def test_archguard_业务表缺账号列告警(tmp_path: Path) -> None:
    """INV-1a：新建业务表 DDL 缺 account_id/tenant_id 列 → 退出码 1。"""
    (tmp_path / "v1.sql").write_text(
        "CREATE TABLE customer_profile (\n  id BIGINT PRIMARY KEY,\n  name TEXT\n);\n", encoding="utf-8"
    )
    assert check_account_id.main([str(tmp_path)]) == 1


def test_archguard_含账号维度列通过(tmp_path: Path) -> None:
    """INV-1a：业务表带 account_id 列（含嵌套 CHECK 子括号）→ 退出码 0。"""
    (tmp_path / "v2.sql").write_text(
        "CREATE TABLE inquiry_thread (\n"
        "  id BIGINT PRIMARY KEY,\n"
        "  account_id BIGINT NOT NULL DEFAULT 0,\n"
        "  state TEXT CHECK (state IN ('OPEN', 'CLOSED'))\n"
        ");\n",
        encoding="utf-8",
    )
    assert check_account_id.main([str(tmp_path)]) == 0


def test_archguard_共享表白名单豁免(tmp_path: Path) -> None:
    """INV-1a：知识域共享表（如 knowledge_chunk）无账号列不判违例 → 退出码 0。"""
    (tmp_path / "v3.sql").write_text(
        "CREATE TABLE knowledge_chunk (\n  id BIGINT PRIMARY KEY,\n  content TEXT\n);\n", encoding="utf-8"
    )
    assert check_account_id.main([str(tmp_path)]) == 0


def test_archguard_alembic建表缺列告警(tmp_path: Path) -> None:
    """INV-1a（ORM 形态）：op.create_table 迁移缺 account_id 列 → 退出码 1。"""
    (tmp_path / "v4.py").write_text(
        "def upgrade():\n"
        "    op.create_table('quote_record',\n"
        "        sa.Column('id', sa.BigInteger(), primary_key=True),\n"
        "        sa.Column('price', sa.Numeric()),\n"
        "    )\n",
        encoding="utf-8",
    )
    assert check_account_id.main([str(tmp_path)]) == 1


def test_archguard_硬编码账号魔数告警(tmp_path: Path) -> None:
    """INV-2b：account_id = 1 魔数赋值（含 SQL 字符串字面量形态）→ 退出码 1。"""
    (tmp_path / "repo.py").write_text(
        "SQL = 'SELECT * FROM inquiry_thread WHERE " + "account" + "_id = 1'\n", encoding="utf-8"
    )
    assert check_account_id.main([str(tmp_path)]) == 1


def test_archguard_比较与豁免标注不误报(tmp_path: Path) -> None:
    """INV-2b：== 比较、上下文注入赋值与 arch-guard: exempt 标注（紧邻上一行）均不判违例。"""
    (tmp_path / "clean.py").write_text(
        "if row.account_id == 1:\n"
        "    pass\n"
        "account_id = ctx.account_id  # 从请求上下文注入，非魔数\n"
        "# arch-guard: exempt 后台聚合单账号演示 CR-2026-002\n"
        "tenant_id = 9\n",
        encoding="utf-8",
    )
    assert check_account_id.main([str(tmp_path)]) == 0
