<!-- 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。 -->
# AI 外贸销售智能体系统 部署指南

| 文档属性 | 内容 |
|---|---|
| 文档版本 | V1.0 |
| 产品 Tag | phase5-v1.0 |
| 适用形态 | Docker Compose（单机/小规模）与 K8s Helm（P5 主推） |
| 语言 | 简体中文（NFR-USA-02） |

**变更记录**

| 版本 | 日期 | 修订说明 | 修订人 |
|---|---|---|---|
| V1.0 | 2026-10-11 | 初版（随 phase5-v1.0 发布） | ZCode subagent |

> 本文档为产品文档三件套之三（FR-SAAS-005），操作对象为安装部署包（FR-SAAS-004）。
> **单一来源约定**：可执行命令一律引用部署包内脚本，本文只解释参数与判读结果。
> 配套：[用户手册](user-manual.md)、[API 文档](api-guide.md)。

## 前置要求

| 项 | 要求 | 自动检测 |
|---|---|---|
| 操作系统 | Ubuntu 22.04 LTS / Alibaba Cloud Linux 3（x86_64/arm64） | preflight |
| Docker（Compose 形态） | Engine ≥ 24.0 + Compose v2 ≥ 2.20 | preflight |
| K8s（Helm 形态） | ≥ 1.28 + Helm ≥ 3.14，默认 StorageClass 支持 PVC 动态供给 | preflight |
| 资源 | ≥ 8C32G，数据盘 ≥ 200G SSD（可用 ≥ 60%） | preflight |
| 网络 | 在线模式仅放行镜像仓库 + DeepSeek API 域名白名单（NFR-SEC-06） | preflight |
| 安全 | 非 root 部署账号；密钥文件权限 600 | bootstrap |

部署包结构（`deploy/compose-package/`）：`install.sh`（一键部署）、`rollback.sh`
（回滚）、`.env.example`（模板，由生成器产出 `.env`）、`tenant-config.example.yaml`
（租户配置单一源示例）。Helm 形态见 `deploy/helm/`（`Chart.yaml` + `values.yaml`）。

## Compose 部署

一条命令完成部署（七步：环境检测 → 密钥 → 镜像 → 配置 → 迁移 → 健康等待 → 管理员）：

```bash
cd deploy/compose-package
./install.sh --offline --tenant-config tenant-config.example.yaml   # 离线优先（NFR-SEC-06）
```

判读要点：

- **配置漂移硬门禁**：`.env` 由 `gen_config.py` 从租户配置源生成并携带指纹，
  手改生成物会在第④步被拦截（退出码 12）——请改源后重新生成；
- **自检（verify）**：部署完成前自动执行四类检查（服务健康 / 依赖版本 / 端口连通 /
  示例检索），任一红即非零退出，报告入 deploy_record；
- **退出码契约**：0 成功 / 10 环境检测 / 11 镜像校验 / 12 配置漂移 / 13 迁移失败 /
  14 健康等待超时；
- **初始凭证**：`.secrets/admin-init.txt`（600，首登强制改密）。

## Helm 部署

```bash
helm dependency build deploy/helm/aisale
helm install aisale deploy/helm/aisale -n aisale-t001 --create-namespace \
  -f values-<tenant>.yaml     # 由 gen_config.py --target helm 生成
```

`values.yaml` 全参数说明（节选，逐项见文件内注释）：

| 参数 | 说明 |
|---|---|
| `image.tag` | 版本化不可变（CI 注入 chart-version + git SHA） |
| `saasTenant.namespace` | 租户级 namespace 隔离（FR-SAAS-001） |
| `apps.*.replicas / resources / hpa` | 12 服务逐个参数化 |
| `brand.logoUrl / primaryColor` | 白标冷启动初值（运行时读取，改配置免发版） |
| `postgresql / milvus / redis` | subchart 依赖与持久化容量 |
| `migrate.enabled` | pre-install Job 执行 alembic 迁移 |

自检（与 Compose 同口径四类检查）：

```bash
kubectl -n aisale-t001 run verify --rm -i --restart=Never \
  --image=registry.example.com/aisale-app:<TAG> -- verify
```

## 升级与回滚

- **升级**：新版本镜像 + Chart → 重跑 `install.sh`（幂等）或 `helm upgrade`；
  数据库迁移先自动快照（保留 ≥30 天，NFR-REL-04）；
- **Compose 回滚**：`./rollback.sh --to <version>`（未备份拒绝执行 / 目标不存在分别
  退出码 21 / 20；`--skip-db` 仅回滚应用层）；重复回滚同版本为 no-op 成功；
- **Helm 回滚**：`helm rollback aisale <revision> -n aisale-t001`；数据库迁移不随
  helm 自动回滚，需按提示显式执行 downgrade（仅保证 N-1 相邻版本自动可回，跨版本
  须人工评估双人确认）；
- **回滚演练**：每次发版后 24h 内在 staging 执行五步演练并归档 deploy_record
  （升级留痕 → 注入故障 → 回滚 → 再自检全绿 → 记录归档）。

## 依赖与合规

依赖清单（SBOM 源）随包交付，关键结论（COM-IP-05）：PostgreSQL（PostgreSQL License）、
Milvus / TEI / BGE-M3（Apache-2.0）、nginx（BSD-2）均无传染；Redis 7.2（RSALv2/SSPL）
以未修改容器镜像使用、Grafana（AGPL-3.0）以独立容器内部使用，均不构成传染，
如需完全宽松许可可替换 valkey / VictoriaMetrics。

## 故障排查

| 症状 | 处置 |
|---|---|
| install.sh 退出码 14（健康超时） | `docker compose ps` 定位卡点容器，查其日志 |
| 退出码 12（配置漂移） | 恢复 `.env` 由生成器产出；确认无人手改 |
| 迁移失败（退出码 13） | 检查迁移前快照已生成；修复后重跑（迁移幂等） |
| 白标域名验证不过 | 检查 TXT `_asaverify.<域名>` 与 CNAME 指向平台入口 |
| 检索时延超阈值 | 确认 TEI（BGE-M3）容器资源预留 4G 起步 |
