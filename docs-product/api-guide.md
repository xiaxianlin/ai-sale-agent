<!-- 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。 -->
# AI 外贸销售智能体系统 API 文档

| 文档属性 | 内容 |
|---|---|
| 文档版本 | V1.0 |
| 产品 Tag | phase5-v1.0 |
| 基线规范 | OpenAPI 3.1（由代码注解导出，`gen_openapi --check` 门禁防漂移） |
| 语言 | 简体中文（NFR-USA-02） |

**变更记录**

| 版本 | 日期 | 修订说明 | 修订人 |
|---|---|---|---|
| V1.0 | 2026-10-11 | 初版（随 phase5-v1.0 发布） | ZCode subagent |

> 本文档为产品文档三件套之二（FR-SAAS-005）。配套：[用户手册](user-manual.md)、[部署指南](deploy-guide.md)。
> 内部接口（`x-scope=internal`）不出租户文档；示例凭证一律占位符 `<API-KEY>`。

## 鉴权

- **租户管理端**：JWT Bearer（`Authorization: Bearer <token>`），RBAC 按角色校验
  （NFR-SEC-02）；40101 未授权 / 40301 越权；
- **公开接口**：无需鉴权（如白标品牌读取），仅白名单字段输出；
- **幂等**：写接口支持 `Idempotency-Key` 头，重放返回首次结果（04-接口设计 §1.4）；
- **全链追踪**：请求头 `X-Trace-Id` 透传，响应信封必携带 `trace_id`。

## 错误码

统一响应信封（四键齐全）：`{"code": 0, "message": "ok", "data": {...}, "trace_id": "…"}`。

| code | HTTP | 含义 | 典型场景 |
|---|---|---|---|
| 0 | 200 | 成功 | — |
| 40001 | 400 | 参数/状态违规 | 差异项缺失、状态机非法迁移、非法色值 |
| 40101 | 401 | 未授权 | 缺 token / token 过期 |
| 40301 | 403 | 越权 | RBAC 不满足 |
| 40401 | 404 | 资源不存在 | 未知向导 uid / 域名 id |
| 40901 | 409 | 冲突 | 自定义域名被抢占 |
| 42901 | 429 | 超配额/限流 | 资源预估超套餐（升配额后重试） |
| 50001 | 500 | 服务内部错误 | 联系平台运营并附 trace_id |
| 50002 | 503 | 下游依赖超时 | 稍后重试 |

## 接口清单

### 开通向导（FR-SAAS-002，平台运营管理员）

| 方法与路径 | 说明 |
|---|---|
| POST `/api/v1/saas/onboarding` | 创建向导（预分配 tenant_id） |
| PUT `/api/v1/saas/onboarding/{uid}/company-info` | ①企业信息 |
| POST `/api/v1/saas/onboarding/{uid}/kb/materials` | ②素材登记（回显资源预估） |
| PUT `/api/v1/saas/onboarding/{uid}/accounts` | ③账号参数（凭证仅掩码回显） |
| PUT `/api/v1/saas/onboarding/{uid}/strategy` | ④策略参数（差异项必填） |
| GET `/api/v1/saas/templates/defaults` | 配置模板预填 |
| POST `/api/v1/saas/onboarding/{uid}/activate` | 提交开通（幂等键必带） |
| GET `/api/v1/saas/onboarding/{uid}/progress` | 进度轮询（文件粒度） |
| POST `/api/v1/saas/onboarding/{uid}/kb/retry` | 失败重试（仅补跑失败文件） |
| GET `/api/v1/saas/onboarding/{uid}/drill-report` | 零代码接入清单回执 |

### 品牌定制（FR-SAAS-006，租户管理员）

| 方法与路径 | 说明 |
|---|---|
| PUT `/api/v1/saas/tenants/{id}/brand-config` | 修改品牌（version 单调递增） |
| GET `/api/v1/saas/tenants/{id}/brand-config` | 读取全量配置 |
| GET `/api/v1/public/brand-config` | 公开读取（ETag / 304） |
| POST `/api/v1/saas/domains` | 注册自定义域名（下发 DNS 指引） |
| POST `/api/v1/saas/domains/{id}/verify` | 验证并签发证书 |

### 统一检索（FR-KB-030，租户 API Key）

| 方法与路径 | 说明 |
|---|---|
| POST `/api/v1/knowledge/search` | 统一检索（kb_types / top_k / rerank） |

## 示例

```bash
# 创建开通向导
curl -X POST "https://<PUBLIC_DOMAIN>/api/v1/saas/onboarding" \
  -H "Authorization: Bearer <API-KEY>" \
  -H "Idempotency-Key: onboard-2026-001" \
  -H "Content-Type: application/json" \
  -d '{"plan": "standard"}'
# → {"code":0,"message":"ok","data":{"uid":"ob_xxx","tenant_id":7001,"status":"configuring"},"trace_id":"…"}
```

```bash
# 提交开通（幂等：重放同键返回首次结果）
curl -X POST "https://<PUBLIC_DOMAIN>/api/v1/saas/onboarding/ob_xxx/activate" \
  -H "Authorization: Bearer <API-KEY>" \
  -H "Idempotency-Key: activate-2026-001"
# → {"code":0,"data":{"kb_task_uid":"kbt_xxx","cfg_rows":16,"status":"initializing"},…}
```

```bash
# 公开读取品牌配置（304 协商缓存）
curl -sD - "https://<PUBLIC_DOMAIN>/api/v1/public/brand-config?tenant_id=7" \
  -H "If-None-Match: \"t7-brand-v12\""
# → HTTP 304（未变更零载荷）；变更时 200 + 新 ETag
```

```bash
# 统一检索（错误示例：kb_type 非法 → 40001 信封）
curl -X POST "https://<PUBLIC_DOMAIN>/api/v1/knowledge/search" \
  -H "Authorization: Bearer <API-KEY>" -H "Content-Type: application/json" \
  -d '{"query": "不锈钢滤芯起订量", "kb_types": ["product"]}'
```

## 变更日志

| 版本 | 变更 | 兼容性 |
|---|---|---|
| v1 | phase5-v1.0 首版接口面（上表全部） | — |

> 破坏性变更必须附 v2 迁移说明（04-接口设计 §5 同规）；版本与产品 tag 联动发布。
