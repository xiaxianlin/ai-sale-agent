#!/usr/bin/env bash
# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# Compose 形态回滚（FR-SAAS-004，SR-SAAS-004 §6.6）。
#
# 用法：./rollback.sh --to <version> [--skip-db]
# 退出码契约：0 成功（含重复回滚 no-op）；20 目标版本不存在；21 未备份拒绝执行。
# 边界（SR §6.6）：downgrade 仅保证 N-1 相邻版本自动可回；跨 ≥2 版本须人工评估
# 双人确认；downgrade 前强制使用迁移前快照（--skip-db 仅回滚应用层，schema 未变场景）。
# CR A4 修复：docker compose 调用补 -f 指向包内 compose.yaml；DB 回退口径改为
# 恢复迁移前快照（migrations/ 为裸 SQL，无迁移框架回滚链）。
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${PKG_DIR}/compose.yaml"   # CR A4 修复：包内真实编排文件（-f 必指）
ENV_FILE="${PKG_DIR}/.env"
BACKUP_DIR="${PKG_DIR}/backups"
TARGET_VERSION=""
SKIP_DB="false"
DEPLOY_LOG="${PKG_DIR}/deploy_records.log"   # 部署/回滚留痕（生产写 deploy_record 表）

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to) TARGET_VERSION="$2"; shift 2 ;;
    --skip-db) SKIP_DB="true"; shift ;;
    *) echo "未知参数：$1"; exit 20 ;;
  esac
done

log() { echo "[rollback $(date -u +%FT%TZ)] $*"; }

# compose 调用统一入口（CR A4 修复：-f 指向包内 compose.yaml + --env-file 注入 .env）
dc() { docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"; }

[[ -n "${TARGET_VERSION}" ]] || { echo "缺少 --to <version>"; exit 20; }
[[ -f "${ENV_FILE}" ]] || { echo "缺 ${ENV_FILE}（未部署或已移除）"; exit 20; }
[[ -f "${COMPOSE_FILE}" ]] || { echo "缺编排文件：${COMPOSE_FILE}"; exit 20; }
[[ "${TARGET_VERSION}" != "latest" ]] || { echo "禁止回滚到 latest（版本化不可变）"; exit 20; }

CURRENT_TAG="$(grep -E '^IMAGE_TAG=' "${ENV_FILE}" | cut -d= -f2- || true)"

# 幂等：已在目标版本 → no-op 成功（重复 rollback --to 同版本第二次直接成功）
if [[ "${CURRENT_TAG}" == "${TARGET_VERSION}" ]]; then
  log "当前已是目标版本 ${TARGET_VERSION}，no-op（幂等）"
  exit 0
fi

# 目标版本必须留痕可查（deploy_record / 部署日志），否则拒绝（退出码 20）
grep -q "${TARGET_VERSION}" "${DEPLOY_LOG}" 2>/dev/null \
  || { echo "目标版本不存在或无部署留痕：${TARGET_VERSION}"; exit 20; }

# 未备份拒绝执行（退出码 21；备份保留 ≥30 天，NFR-REL-04）——除 --skip-db 外强制
if [[ "${SKIP_DB}" != "true" ]]; then
  ls "${BACKUP_DIR}"/pre-migrate-*.sql >/dev/null 2>&1 \
    || { echo "未发现迁移前备份（${BACKUP_DIR}/pre-migrate-*.sql），拒绝回滚"; exit 21; }
  LATEST_BACKUP="$(ls -1t "${BACKUP_DIR}"/pre-migrate-*.sql | head -1)"
  log "使用快照：${LATEST_BACKUP}"
fi

# ① 恢复镜像 tag 与配置指纹（.env 由 gen-config 重新生成后校验，指纹对齐目标版本）
log "回滚应用层：IMAGE_TAG ${CURRENT_TAG} → ${TARGET_VERSION}"
sed -i.bak "s/^IMAGE_TAG=.*/IMAGE_TAG=${TARGET_VERSION}/" "${ENV_FILE}"
dc up -d --force-recreate app site

# ② DB 回退（--skip-db 跳过：纯代码缺陷、schema 未变场景）
#    CR A4 修复：裸 SQL 口径无迁移框架回滚——回退 = 清库后恢复迁移前快照
if [[ "${SKIP_DB}" != "true" ]]; then
  log "执行 DB 快照恢复（仅保证 N-1 相邻版本自动可回）"
  local_pg_user() { grep -E '^POSTGRES_USER=' "${ENV_FILE}" | cut -d= -f2- || true; }
  local_pg_db() { grep -E '^POSTGRES_DB=' "${ENV_FILE}" | cut -d= -f2- || true; }
  PG_USER="$(local_pg_user)"; PG_USER="${PG_USER:-aisale}"
  PG_DB="$(local_pg_db)"; PG_DB="${PG_DB:-aisale}"
  dc exec -T postgres psql -U "${PG_USER}" -d postgres \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
         WHERE datname = '${PG_DB}' AND pid <> pg_backend_pid();" \
    -c "DROP DATABASE IF EXISTS ${PG_DB};" \
    -c "CREATE DATABASE ${PG_DB};" \
    || { echo "清库失败（准备恢复快照前），请人工评估"; exit 21; }
  dc exec -T postgres psql -v ON_ERROR_STOP=1 -U "${PG_USER}" -d "${PG_DB}" \
    < "${LATEST_BACKUP}" \
    || { echo "快照恢复失败，请人工评估（跨 ≥2 版本需双人确认）"; exit 21; }
fi

# ③ 留痕 rolled_back（生产写 deploy_record.result='rolled_back'）
echo "$(date -u +%FT%TZ) rolled_back to ${TARGET_VERSION} (from ${CURRENT_TAG}, skip_db=${SKIP_DB})" \
  >> "${DEPLOY_LOG}"
log "回滚完成：${TARGET_VERSION}（已留痕 rolled_back，请重跑 install.sh verify 自检）"
exit 0
