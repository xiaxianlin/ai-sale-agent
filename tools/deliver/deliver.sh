#!/usr/bin/env bash
# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# deliver.sh —— 阶段源码交付装配流水线骨架（COM-IP-03，SR-COM-IP-03 §6.2 起步版）。
# 用法：bash tools/deliver/deliver.sh <P0|P1|P2|P3|P4|P5> <验收tag> [交付序号，默认001]
# 产物：dist/<阶段>-<序号>/（01-source…07-handover 七目录 + MANIFEST.json 逐项 sha256 清单）
# 骨架边界：git bundle 装配 / 迁移目录拷贝 / 密钥复扫 / MANIFEST 生成 / 整体指纹为真实实现；
#           数据导出、模型权重、文档冻结、台账登记为占位步骤，待 OSS/milvus-backup、
#           TEI 权重目录与 admin-service 交付台账 API 落地后替换（SR-COM-IP-03 §5/§6.4/§6.6）。
set -euo pipefail

PHASE="${1:-}"
TAG="${2:-}"
SEQ="${3:-001}"

# ── 参数校验：阶段枚举与验收 tag 必填（交付以验收 tag 为唯一输入，保证重复执行产物一致）──
case "$PHASE" in
  P0|P1|P2|P3|P4|P5) ;;
  *) echo "用法: bash tools/deliver/deliver.sh <P0|P1|P2|P3|P4|P5> <验收tag> [交付序号]"; exit 1 ;;
esac
if [ -z "$TAG" ]; then
  echo "[失败] 缺少验收 tag 参数（COM-IP-03：交付物必须由验收 tag 导出，禁止 working-tree 快照）"
  exit 1
fi

DIST="dist/${PHASE}-${SEQ}"
mkdir -p "${DIST}"/01-source "${DIST}"/02-deploy "${DIST}"/03-migration \
         "${DIST}"/04-docs "${DIST}"/05-data "${DIST}"/06-weights "${DIST}"/07-handover

# sha256 计算兼容 Linux(sha256sum) / macOS(shasum)，供 bundle 校验与 MANIFEST 逐项指纹共用
hash256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# ── 步骤 1 源码装配：git bundle 全量历史（--all 携带全部分支与 tag）+ 验收 tag 指向留证 ──
# 说明：SR-COM-IP-03 §6.1-01 要求「全部源代码（含 Git 提交历史）」——bundle 即完整仓，
#       甲方 clone 后提交数/tag 与源仓一致；多仓形态（kb-admin-web/admin-web/mobile-app）
#       接入后在本步骤改为逐仓循环装配，当前起步版为单仓。
BUNDLE="${DIST}/01-source/repo.bundle"
git bundle create "$BUNDLE" --all
git rev-parse "${TAG}^{commit}" > "${DIST}/01-source/tag.txt"
echo "[步骤1] 源码 bundle 完成：${BUNDLE}（验收 tag ${TAG} -> $(cat "${DIST}/01-source/tag.txt")）"

# ── 步骤 2 迁移脚本链：拷贝迁移目录 + 空库重放校验（占位）──
# 占位说明：alembic 接入后启用 migration_replay_check.sh 三重校验——①空库 upgrade head
#       全程成功（断链即失败）；②alembic check 无模型/迁移漂移；③单链头无分叉孤儿版本
#       （SR-COM-IP-03 §6.3）。当前仓库 migrations/ 未落位，先出占位报告随包留痕。
if [ -d migrations ]; then
  cp -r migrations "${DIST}/03-migration/"
fi
{
  echo "迁移链重放校验报告（占位）——alembic 接入后由 migration_replay_check.sh 生成真实结果"
  echo "校验项：空库 upgrade head / alembic check 漂移 / 单链头 / 上阶段链祖先比对（§6.3/§6.8）"
} > "${DIST}/03-migration/replay_report.txt"
echo "[步骤2] 迁移目录已拷贝；空库重放校验为占位步骤（见 03-migration/replay_report.txt）"

# ── 步骤 3 配置/部署脚本：deploy 目录脱敏副本 + 涉密零命中复扫（真实实现）──
# 边界（SR-COM-IP-03 §6.7）：交付包只允许 .env.example 占位符形态，任何真实凭证
#       不得入包；此处复用 CI 密钥扫描门禁对副本复扫，命中即装配中止（不脱敏放行）。
if [ -d deploy ]; then
  cp -r deploy "${DIST}/02-deploy/"
fi
python3 tools/ci_gates/scan_secrets.py "${DIST}/02-deploy"
echo "[步骤3] 部署配置副本完成并已通过密钥复扫（真实凭证一律由 07-handover 密钥清单线下交接）"

# ── 步骤 4 文档快照（占位）：验收 tag 处 docs 冻结导出 ──
# 占位说明：文档库挂接后由 freeze_docs.py 从验收 tag 导出对应阶段文档快照（PDF+MD），
#       已发布章节哈希冻结不变（SR-COM-IP-03 §6.1-04/§6.8）。
echo "[步骤4][占位] 文档冻结导出（freeze_docs）待文档库挂接后启用" | tee "${DIST}/04-docs/README.txt"

# ── 步骤 5 数据导出（占位）：业务库转储 + 知识原始数据 OSS 导出 + 向量索引备份 ──
# 占位说明：接入 OSS 与 milvus-backup 后执行三步——pg_dump 业务转储、按语料版本导出
#       原始素材、milvus-backup 全量备份 collection（含嵌入权重指纹配套校验，§6.4）。
echo "[步骤5][占位] 数据导出与向量索引备份待 OSS/milvus-backup 接入后启用" | tee "${DIST}/05-data/README.txt"

# ── 步骤 6 模型权重（占位）：BGE-M3 权重打包 + 逐文件 sha256 版本指纹 ──
# 占位说明：TEI 权重目录落位后由 weight_pack.sh 打 tar 包并生成 weights_manifest，
#       指纹须与向量索引登记的 embedding_weight_sha256 一致（§6.1-06/§6.4）。
echo "[步骤6][占位] TEI 权重打包与 weights_manifest 待权重目录落位后启用" | tee "${DIST}/06-weights/README.txt"

# ── 步骤 7 MANIFEST 清单生成（真实实现）：对包内全部交付物逐项 sha256+大小 ──
# 定位：MANIFEST.json 是甲方复核的单一事实源（「清单即契约」）；逐项指纹支撑
#       verify_package V1 完整性复核与 COM-IP-02 移交核对，篡改任意一字节即暴露。
MANIFEST="${DIST}/MANIFEST.json"
{
  printf '{\n'
  printf '  "delivery_no": "DEL-%s-%s",\n' "$PHASE" "$SEQ"
  printf '  "phase": "%s",\n' "$PHASE"
  printf '  "accept_tag": "%s",\n' "$TAG"
  printf '  "items": [\n'
  first=1
  while IFS= read -r f; do
    [ "$first" -eq 1 ] || printf ',\n'
    first=0
    printf '    {"path": "%s", "sha256": "%s", "size_bytes": %s}' \
      "${f#"$DIST"/}" "$(hash256 "$f")" "$(wc -c < "$f" | tr -d '[:space:]')"
  done < <(find "$DIST" -type f ! -name MANIFEST.json | sort)
  printf '\n  ],\n'
  printf '  "excluded_secrets": ["凭证与密钥不入包，另见 07-handover/keys_inventory.md（占位）"]\n'
  printf '}\n'
} > "$MANIFEST"
echo "[步骤7] MANIFEST 生成完成：${MANIFEST}（整体指纹 $(hash256 "$MANIFEST")，供台账登记与甲方复核）"

# ── 步骤 8 交付台账登记（占位）：delivery_ledger 取号与清单指纹登记 ──
# 占位说明：admin-service 交付台账 API 落地后调用
#       POST /api/admin/delivery/batches + …/manifest 登记指纹（幂等），并按
#       d0+7 个工作日自动算 deadline 触发提前告警（SR-COM-IP-03 §5/§6.6）。
echo "[步骤8][占位] 交付台账登记（admin-service API）待交付台账落地后启用"

echo "[完成] 交付包骨架已装配：${DIST}"
echo "       正式移交前须完成 V1-V7 自验全绿（清单复核/源码复原/干净部署/迁移重放/数据恢复/检索冒烟/涉密复扫，§6.5）"
