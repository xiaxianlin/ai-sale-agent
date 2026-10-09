# deploy/nginx —— Nginx TLS 网关部署说明（NFR-SEC-05 传输加密）

| 文件 | 说明 |
|---|---|
| `ai-sale.conf` | TLS 基线配置：仅 TLS 1.2/1.3、强套件、HSTS、80→301、wss 升级、ACME 例外、内网 upstream |

依据：`docs/07-实现方案/NFR/SR-NFR-SEC-05-传输加密.md`（工作区文档库，仓库外路径 `../docs/07-实现方案/NFR/`）。

## 1. 部署要点

1. 挂载位置：容器内 `/etc/nginx/conf.d/ai-sale.conf`（`http{}` 上下文内 include，勿放主上下文）；
   ACME 挑战目录 `/var/www/challenge` 与证书目录 `/etc/nginx/certs` 需同步挂载。
2. 上线前替换占位符：`<platform-domain>`（平台主域）、证书文件名（按 §2 目录布局核对）。
3. 生效流程：`nginx -t` 校验 → `nginx -s reload`（热加载不中断存量连接）；任何改动必须先过 `nginx -t`。
4. 基线参数（协议下界/HSTS max-age/wss 超时等）外置于 `app_config`（`security.tls.*`，
   SR-NFR-SEC-05 §7）：改基线走配置变更流程重渲染本文件，**禁止手工魔改后不登记**。

## 2. 证书目录布局

```
/etc/nginx/certs/
├── platform_fullchain.pem / platform.key   # 多 SAN 平台证书（app/train/api 子域，HTTP-01）
├── chat_fullchain.pem / chat.key           # chat 子域独立证书（与平台证书错峰续期）
├── domains.list                            # 到期监控域名清单（矩阵 ∪ 租户域名）
└── tenants/<domain>.{fullchain.pem,key}    # P5 租户自定义域名证书（SNI 动态匹配）
```

证书与 ACME 账户密钥**不入 Git**（`.gitignore` 覆盖 `deploy/secrets/`）；AK/SK 走 Docker Secrets 注入。

## 3. 证书自动续期要点（acme.sh）

- **cron 每日 03:20** 执行 `acme.sh --cron`：acme.sh 自身在到期前 30 天自动续期，
  续期成功经 `--install-cert ... --reloadcmd "nginx -s reload"` 落位新证书并热加载网关，
  随后回调 `POST /api/v1/infra/tls/renewed` 同步证书台账（cert_ledger）并写审计。
- **签发方式**：平台多 SAN 证书走 HTTP-01（依赖 80 端口 `/.well-known/acme-challenge/` 例外，
  该 location 使用 `^~` 前缀匹配、优先级高于 301，不得被跳转吞掉）；P5 泛域名
  `*.<platform-domain>` 走 DNS-01（`dns_ali` 插件，阿里云 AK/SK 经 Secrets 注入）。
- **续期失败告警**（即时，critical）：包装脚本核对每张 `*fullchain.pem` 落位时间，
  超过 30 天未更新即上报 `TLS_RENEW_FAILED`；Redis 计数器 `tls:renew:fail:{domain}`
  连续失败 ≥3 次将台账状态置为 `renew_failed` 并升级人工介入。

## 4. 证书到期监控要点

- **每 6 小时**经本机 443 实测线上证书（`openssl s_client` 读 `notAfter`，含租户域名），
  剩余 **≤14 天**触发 `TLS_CERT_EXPIRING` high 告警并更新台账 `status='expiring'`——
  这是覆盖平台+租户全部域名的**全局兜底**（自动续期失效时仍有 ≥14 天人工处置窗）。
- **双保险**：同一指标暴露为 Prometheus `tls_cert_expire_days{domain}`，告警规则同 ≤14 天。
- **分层口径**：租户自定义域名另有 SR-SAAS-006 `brand.cert_renew_alert_days`（默认 30 天、
  按租户可配）触达租户侧预警；本处 14 天为最后防线，两层互补不冲突。

## 5. 基线自验命令

```bash
# 80 仅 301（ACME 路径例外应返回 200/404，而非 301）
curl -I http://app.<platform-domain>/x
# 弱协议必须握手失败（TLS1.0/1.1/SSLv3 全禁）
openssl s_client -tls1_1 -connect app.<platform-domain>:443 </dev/null
# HSTS 头存在（含非 2xx 响应）
curl -sI https://app.<platform-domain>/not-exist | grep -i strict-transport
# 每周巡检（testssl + sslscan，目标评级 ≥A-，结果入 tls_scan_result 归档）
ops/tls/tls_audit.sh
```

## 6. 内网边界（upstream 注释同源）

- 上游 `app` / `site` 均为内网容器，compose 中不写公网 `ports:`；网关是唯一发布 80/443 的容器。
- PG / Milvus / Redis / TEI 等数据层**绝不反代暴露**：安全组仅放行 80/443 公网入（22 限堡垒机），
  内网端口只允许集群安全组互访（`deploy/infra/security-group/sg.json`，SR-AG-001 §6.4）。
- `/internal/*` 敏感内部接口不经公网入口（配置中直接 404）；P4 跨主机链路可启用 mTLS 片段增强。
