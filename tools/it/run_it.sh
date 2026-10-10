#!/usr/bin/env bash
# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# run_it.sh —— 容器化集成联调运行器（迭代 0.3 收口：PostgreSQL + Milvus 真实对拍）。
#
# 流程：容器栈健康检查 → 数据库迁移（幂等）→ 导出联调环境变量 → 运行集成测试。
# 用法：bash tools/it/run_it.sh
#   环境变量（可选覆盖）：
#     KB_PG_DSN     PostgreSQL 连接串（缺省本地容器 postgres:16，见 deploy/compose/compose.yaml）
#     KB_MILVUS_URI Milvus 地址（缺省 http://localhost:19530）
# 前置：docker compose -f deploy/compose/compose.yaml up -d
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/compose.yaml"
export KB_PG_DSN="${KB_PG_DSN:-postgresql://kb:kb_local_dev@localhost:15432/kb}"
export KB_MILVUS_URI="${KB_MILVUS_URI:-http://localhost:19530}"

echo "==> [1/4] 容器栈健康检查（docker compose ps）"
python3 - "${COMPOSE_FILE}" <<'PY'
"""解析 docker compose ps --format json：postgres 须 healthy，etcd/milvus 须 running。"""
import json
import subprocess
import sys

compose_file = sys.argv[1]
result = subprocess.run(
    ["docker", "compose", "-f", compose_file, "ps", "--format", "json"],
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    print(f"✗ docker compose ps 执行失败：{result.stderr.strip()}")
    print("  请确认 Docker 已启动且 compose 文件存在")
    sys.exit(1)
raw = result.stdout.strip()
items: list[dict] = []
if raw:
    try:  # 兼容 JSON 数组（新版本）与逐行 JSON（旧版本）两种输出形态
        data = json.loads(raw)
        items = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
if not items:
    print("✗ 容器栈未启动：请先执行 docker compose -f deploy/compose/compose.yaml up -d")
    sys.exit(1)
by_service = {it.get("Service") or it.get("Name", ""): it for it in items}
problems: list[str] = []
for service in ("postgres", "etcd", "milvus"):
    info = by_service.get(service)
    if info is None:
        problems.append(f"{service} 未启动")
        continue
    if (info.get("State") or "").lower() != "running":
        problems.append(f"{service} 状态为 {info.get('State')}（需 running）")
    elif service == "postgres" and (info.get("Health") or "").lower() != "healthy":
        problems.append(f"postgres 健康状态为 {info.get('Health')}（需 healthy，稍候重试）")
if problems:
    print("✗ 容器健康检查未通过：" + "；".join(problems))
    sys.exit(1)
print("✓ postgres（healthy）/ etcd / milvus 均已就绪")
PY

echo "==> [2/4] 应用数据库迁移（migrations/*.sql，按文件名序幂等执行）"
python3 - "${REPO_ROOT}" <<'PY'
"""执行 apply_migrations：幂等重放 001_kb_core.sql（IF NOT EXISTS）。"""
import os
import sys

repo_root = sys.argv[1]
sys.path.insert(0, os.path.join(repo_root, "src"))
from services.kb.core.stores.pg.connection import apply_migrations  # noqa: E402

applied = apply_migrations(os.environ["KB_PG_DSN"], os.path.join(repo_root, "migrations"))
print(f"✓ 已执行迁移脚本: {', '.join(applied)}")
PY

echo "==> [3/4] 联调环境变量"
echo "    KB_PG_DSN=${KB_PG_DSN}"
echo "    KB_MILVUS_URI=${KB_MILVUS_URI}"

echo "==> [4/4] 运行集成测试（pytest src/services/kb/tests/integration）"
cd "${REPO_ROOT}"
python3 -m pytest src/services/kb/tests/integration -v

echo "✓ 集成联调全部通过（PG 对拍 + Milvus 对拍）"
