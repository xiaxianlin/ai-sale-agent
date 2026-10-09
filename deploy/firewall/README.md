# deploy/firewall —— 出网白名单防火墙（NFR-SEC-06 数据不出域·网络层防线）

| 文件 | 说明 |
|---|---|
| `egress.rules` | iptables 出网白名单模板：出方向默认 DROP，仅放行出域目的地注册表条目；含 EGRESS-DROP 兜底日志与 TEI 容器出网 DROP |

依据：`docs/07-实现方案/NFR/SR-NFR-SEC-06-数据不出域.md`（工作区文档库，仓库外路径 `../docs/07-实现方案/NFR/`）§6.1、§6.3、§6.5；`docs/02-设计文档/06-安全与合规设计.md` §6。

## 1. 三层防线中的位置

NFR-SEC-06「数据不出域」由三层防线构成，本目录承载**网络层**：

| 防线 | 落点 | 语义 |
|---|---|---|
| 代码层 | `platform/core/net/egress.py`（EgressGuard）+ `llm_gateway.py`（LLMEgressGateway） | 出域调用前强制断言：目标未注册 / OI-09 未确认即拒（fail-closed），调用留痕 |
| **网络层（本目录）** | `egress.rules` | 出方向默认 DROP，仅放行注册表目的地；白名单外尝试产生 EGRESS-DROP 日志 → 告警 |
| 流程层 | OI-09 数据边界评估单（本文 §5 模板） | LLM 出域启用前必须评估 + 甲方确认（confirmed），变更触发重评 |

**同源约定（决策 D2）**：`egress.rules` 的每条放行规则与代码层 EgressGuard 注册表（`EgressDestination`：host/port/purposes/owner/status）一一对应，规则注释中的 `[code]` 即注册表 `code`。新增/变更出域目的地必须：① 服务层注册表（egress_dest 表）变更；② 本文件同步增改对应规则；③ 走 `docs/06-变更管理/` 取号（CD/CR）评审。任何单侧修改视为配置漂移，月度出域审计报表（`platform/core/net/report.py`）含逐条对账项。

## 2. 部署步骤（每次变更必走）

1. **评审**：变更单附「注册表条目 ↔ 防火墙规则」对照表，安全评审通过后方可进入部署；
2. **固化占位符**：将各 `<...解析CIDR>` 以部署时域名解析结果固化（建议同时写入多个 A 记录的 CIDR，并记录解析时间）；`<内网网段>`、`<内网DNS>`、`<TEI容器网段>` 按环境填充；
3. **应用**：逐条执行（或改造为 `iptables-restore` 输入）；**顺序敏感**——默认 DROP（§0）必须在放行条目（§2）之后不被覆盖，兜底 LOG/DROP（§3）必须位于放行条目之后；
4. **持久化**：`iptables-save > /etc/iptables/rules.v4`（或 netfilter-persistent），重启后规则不丢；
5. **验证**：
   - `iptables -L OUTPUT -v -n --line-numbers`：逐条核对 code 注释与注册表一致；
   - 白名单外连通性演练：`curl -m 5 https://<未注册域名>` 应失败，且 `dmesg | grep EGRESS-DROP` 出现日志（该日志接入 Loki → 告警引擎，即违规出域检测信号①，SR §6.5）；
   - Embedding 零出域核查：TEI 容器内发起任何出网请求均不可达（§4 规则 + OUTPUT 默认 DROP 双保险）。

## 3. 阿里云安全组出方向（同源生成）

安全组与 iptables 同一份目的地清单生成，但**必须注意**：安全组出方向**默认允许**，与 iptables 默认 DROP 相反——因此除放行条目（白名单 CIDR:443/465/代理端口 + 内网网段）外，**必须显式添加兜底拒绝条目**（优先级最低的 0.0.0.0/0 拒绝全部），否则安全组将成为整体防线漏洞。安全组无 LOG 能力，违规检测证据统一取宿主机 iptables 的 EGRESS-DROP 日志。

## 4. Embedding 本地强制（第四道防护）

BGE-M3 向量化零出域（FR-KB-013 硬约束）在网络层由两条规则保证：TEI 容器网段 `DOCKER-USER` DROP（容器出网）+ OUTPUT 默认 DROP（宿主机进程绕过断言的兜底）。配合代码层断言与「注册表永不包含 embedding 类目的地」的口径，任何外部 Embedding API 调用必然失败并产生 EGRESS-DROP 日志 → 告警。

## 5. OI-09 数据边界评估单模板

LLM 及任何新增出域目标启用前，按下表编制评估单（版本化：初评 v1，变更重评递增版本，旧版置 superseded）。**状态机**：`draft`（乙方编制）→ `submitted`（提交甲方）→ `confirmed`（甲方确认后登记确认件归档地址，唯一放行途径：对应目的地方可置 enabled）。

### OI-09 数据边界评估单（编号：OI-09-<目的地code>-v<版本>）

| 数据项 | 目的地（egress_dest.code） | 目的/用途 | 频率 | 保护措施 |
|---|---|---|---|---|
| 已脱敏消息文本 | deepseek_api | LLM 回复生成 / 意图识别 | 实时 | PII 二次过滤（NFR-SEC-01）+ 客户身份脱替 + 敏感词表 + TLS |
| 检索后知识片段 | deepseek_api | LLM 上下文增强 | 实时 | 片段来源可追溯（R-07）+ TLS |
| 加密业务归档 | aliyun_oss | 备份 / 确认件归档 | 每日 | AES 加密 + 同账号体系（06 §6） |
| 告警文本（不含客户 PII） | dingtalk / wechat | 告警通知 | 实时 | 生成侧保证无 PII + TLS |
| 邮件正文（收件人授权提供） | smtp | 业务邮件外发 | 按需 | 收件人授权 + 快照留存 + TLS |
| 仅代理协议流量（不含业务载荷） | proxy_provider | 阿里国际站操作出口 | 实时 | 载荷剥离 + 住宅 IP（§7.5 账号安全） |

**变更触发重评**：新增出域目的地 / data_types（允许数据类型）变化 / 敏感词策略收紧 → 受影响目的地置 `pending_reeval`（代码层随即 EGRESS-1003 拒绝，fail-closed），生成新版本评估单，旧版置 superseded。

### 甲方确认栏（confirmed 状态必填，确认件归档至 OSS 加密对象）

| 字段 | 内容 |
|---|---|
| 评估单编号 / 版本 | OI-09-____________-v____ |
| 甲方确认人（签字） | ____________ |
| 确认日期 | ____年____月____日 |
| 甲方意见 | ____________ |
| 确认件归档地址（OSS 加密对象路径） | oss://<bucket>/egress-assessment/____________ |

> 确认登记后由服务层将对应 `egress_dest.status` 置 `enabled` 并回填 `oi_ref`（评估单号）；确认 / 变更 / 重评全程写审计日志哈希链（NFR-SEC-03）。
