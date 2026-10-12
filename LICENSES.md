# LICENSES.md —— 开源依赖审查登记册（COM-IP-05，license_gate.py 自动并入白名单）
# 登记口径：| 包名 | 许可证 | 用途 | 审查记录 |；未登记的包不得进入 [project].dependencies。
# 内置白名单（fastapi/pydantic/openpyxl/python-docx/pytest/httpx/ruff/uvicorn）无需在此重复登记。

| 包名 | 许可证 | 用途 | 审查记录 |
|---|---|---|---|
| psycopg | LGPL-3.0-or-later | PostgreSQL 驱动（psycopg 3 + binary 轮子，真实存储层 stores/pg） | CR 报告 S4 修复（2026-10-11）：LGPL 仅动态链接使用、不修改库源码，与 COM-IP-01 交付物无代码混染 |
| cryptography | Apache-2.0 OR BSD-3-Clause | 凭证保险库信封加密 AES-256-GCM（vault 主路径，NFR-SEC-04） | CR 报告 S2 修复（2026-10-11）：双许可均为宽松协议，用途与范围无冲突 |
