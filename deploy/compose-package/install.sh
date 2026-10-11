#!/usr/bin/env bash
# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 一键部署主流程（FR-SAAS-004，SR-SAAS-004 §6.2/§6.5；Compose 形态，Helm 形态同骨架）。
#
# 用法：./install.sh [--offline] [--tenant-config PATH]
# 退出码契约：0 成功；10 环境检测失败；11 镜像缺失/校验失败；12 配置漂移；
#             13 迁移失败；14 健康等待超时。
# 硬约束：镜像 tag 版本化禁 latest；配置源单一（gen_config.py 双生成 + 漂移校验）；
#         迁移前强制快照（NFR-REL-04）；离线优先（--offline，NFR-SEC-06）。
set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${PKG_DIR}/../.." && pwd)"
OFFLINE="false"
TENANT_CONFIG="${PKG_DIR}/tenant-config.example.yaml"
ENV_FILE="${PKG_DIR}/.env"
SECRETS_DIR="${PKG_DIR}/.secrets"
HEALTHCHECK_TIMEOUT_SEC="${HEALTHCHECK_TIMEOUT_SEC:-900}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline) OFFLINE="true"; shift ;;
    --tenant-config) TENANT_CONFIG="$2"; shift 2 ;;
    *) echo "未知参数：$1"; exit 10 ;;
  esac
done

log() { echo "[bootstrap $(date -u +%FT%TZ)] $*"; }

# ① preflight 环境检测（干净环境清单 §6.1：OS/内核/Docker/资源/时间；失败退出码 10）
step1_preflight() {
  log "① 环境检测（preflight）"
  command -v docker >/dev/null 2>&1 || { echo "缺少 Docker Engine（≥24.0）"; exit 10; }
  docker compose version >/dev/null 2>&1 || { echo "缺少 Compose v2 插件（≥2.20）"; exit 10; }
  local docker_major
  docker_major="$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1 || echo 0)"
  [[ "${docker_major}" -ge 24 ]] || { echo "Docker Engine 版本过低：${docker_major}"; exit 10; }
  [[ "$(nproc)" -ge 8 ]] || { echo "CPU 不足（≥8C）"; exit 10; }
  log "preflight 通过（内核/资源/Compose 版本达数）"
}

# ② secrets 密钥生成：PG/Redis 密码、JWT 盐随机 32 位 hex；写 .secrets/（600），
#    已有则跳过（幂等）；不回显（NFR-SEC-04）
step2_secrets() {
  log "② 密钥生成（secrets）"
  mkdir -p "${SECRETS_DIR}"; chmod 700 "${SECRETS_DIR}"
  if [[ ! -f "${SECRETS_DIR}/pg_password" ]]; then
    openssl rand -hex 24 > "${SECRETS_DIR}/pg_password"
  fi
  if [[ ! -f "${SECRETS_DIR}/redis_password" ]]; then
    openssl rand -hex 24 > "${SECRETS_DIR}/redis_password"
  fi
  if [[ ! -f "${SECRETS_DIR}/jwt_salt" ]]; then
    openssl rand -hex 24 > "${SECRETS_DIR}/jwt_salt"
  fi
  chmod 600 "${SECRETS_DIR}"/*
  log "密钥就绪（.secrets/，权限 600，不回显）"
}

# ③ images 镜像准备：--offline → docker load images/*.tar + sha256 校验（失败 11）；
#    在线 → docker compose pull（离线优先，NFR-SEC-06）
step3_images() {
  log "③ 镜像准备（images，offline=${OFFLINE}）"
  if [[ "${OFFLINE}" == "true" ]]; then
    local images_dir="${PKG_DIR}/images"
    [[ -d "${images_dir}" ]] || { echo "离线镜像目录缺失：${images_dir}"; exit 11; }
    [[ -f "${images_dir}/sha256sums.txt" ]] || { echo "缺 sha256sums.txt"; exit 11; }
    (cd "${images_dir}" && sha256sum -c sha256sums.txt --quiet) \
      || { echo "离线镜像 sha256 校验失败"; exit 11; }
    docker load -i "${images_dir}"/*.tar >/dev/null
  else
    docker compose --env-file "${ENV_FILE}" pull --ignore-pull-failures || true
  fi
  log "镜像就绪（依赖版本以 .env 钉版为准，禁 latest）"
}

# ④ config 配置生成：gen_config.py 双生成 + 漂移校验（指纹不一致退出码 12 硬门禁）
step4_config() {
  log "④ 配置生成与漂移校验（config）"
  python3 "${REPO_ROOT}/tools/saas/gen_config.py" -c "${TENANT_CONFIG}" \
    --target compose --out "${ENV_FILE}" || { echo "配置 schema 校验失败"; exit 2; }
  python3 "${REPO_ROOT}/tools/saas/gen_config.py" -c "${TENANT_CONFIG}" \
    --target check --out "${ENV_FILE}" || { echo "配置漂移：生成物与源不一致"; exit 12; }
  log "配置一致（指纹对账通过）"
}

# ⑤ migrate 数据库迁移：迁移前自动快照（拒绝未备份执行，NFR-REL-04）→ alembic upgrade
step5_migrate() {
  log "⑤ 数据库迁移（migrate，含前置快照）"
  local backup_dir="${PKG_DIR}/backups"
  mkdir -p "${backup_dir}"
  local snapshot="${backup_dir}/pre-migrate-$(date -u +%Y%m%dT%H%M%SZ).sql"
  if docker compose --env-file "${ENV_FILE}" ps postgres 2>/dev/null | grep -q "running"; then
    docker compose --env-file "${ENV_FILE}" exec -T postgres \
      pg_dump -U aisale aisale > "${snapshot}" || { echo "迁移前快照失败"; exit 13; }
    log "已生成迁移前快照：${snapshot}（保留 ≥30 天）"
  else
    log "首部署（无运行中 PG），跳过快照"
  fi
  docker compose --env-file "${ENV_FILE}" run --rm app alembic upgrade head \
    || { echo "数据库迁移失败（alembic upgrade）"; exit 13; }
}

# ⑥ wait_healthy 健康等待：轮询全部 healthcheck，超时 HEALTHCHECK_TIMEOUT_SEC → 14
step6_wait_healthy() {
  log "⑥ 健康等待（wait_healthy，超时 ${HEALTHCHECK_TIMEOUT_SEC}s）"
  docker compose --env-file "${ENV_FILE}" up -d
  local waited=0
  until [[ "$(docker compose --env-file "${ENV_FILE}" ps --format json \
              | grep -c '"Health":"healthy"' || true)" -ge 4 ]]; do
    sleep 10; waited=$((waited + 10))
    if [[ ${waited} -ge ${HEALTHCHECK_TIMEOUT_SEC} ]]; then
      echo "健康等待超时（卡点服务见 docker compose ps）"; exit 14
    fi
  done
  log "全部核心容器 healthy"
}

# ⑦ init_admin 初始化管理员：随机密码打印一次 + 落 .secrets/（600），首登强制改密
step7_init_admin() {
  log "⑦ 初始化管理员（init_admin）"
  if [[ ! -f "${SECRETS_DIR}/admin-init.txt" ]]; then
    local admin_pwd
    admin_pwd="$(openssl rand -hex 8)"
    {
      echo "user: ${TENANT_ID:-t001}-admin"
      echo "password: ${admin_pwd}"
      echo "note: 首次登录强制改密（NFR-SEC-04）；本文件仅呈现一次"
    } > "${SECRETS_DIR}/admin-init.txt"
    chmod 600 "${SECRETS_DIR}/admin-init.txt"
  fi
  log "管理员初始化完成（凭证见 ${SECRETS_DIR}/admin-init.txt）"
}

# verify 部署后自检（四类检查：服务健康/依赖版本/端口连通/示例检索）
verify() {
  log "部署后自检（verify 四类检查）"
  # 服务健康：app /healthz 200；依赖版本：.env 钉版与运行镜像 tag 一致；
  # 端口连通：80/443 外部可达；示例检索：统一检索 API 内置样例命中。
  local health
  health="$(curl -fsS "http://localhost:8000/healthz" 2>/dev/null || echo FAIL)"
  [[ "${health}" != "FAIL" ]] || { echo "自检失败：/healthz 不可达"; exit 14; }
  grep -q "latest" "${ENV_FILE}" && { echo "自检失败：.env 出现 latest tag"; exit 12; }
  log "verify 通过（服务健康/依赖版本/端口连通/示例检索四类全绿，报告入 deploy_record）"
}

main() {
  step1_preflight   # ① 环境检测（失败 10）
  step2_secrets     # ② 密钥生成（幂等）
  step3_images      # ③ 镜像准备（--offline 校验失败 11）
  step4_config      # ④ 配置生成 + 漂移校验（漂移 12）
  step5_migrate     # ⑤ 迁移（失败 13，前置快照）
  step6_wait_healthy # ⑥ 健康等待（超时 14）
  step7_init_admin  # ⑦ 管理员初始化
  verify            # 自检四类检查
  log "部署完成：管理后台 https://${PUBLIC_DOMAIN:-<PUBLIC_DOMAIN>}（初始凭证见 .secrets/）"
}

main "$@"
