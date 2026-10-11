# site-sdk（FR-SITE-001 网站聊天组件，源码交付）

知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。

本目录为**源码交付**（无构建链）：`loader.js`（加载器，ES5）+ `chat.js`（主体）。
静态托管于 `https://<域名>/sdk.js` 与 `/chat.js`（Nginx 版本化 URL + 长缓存），
后端为 site-service（`src/services/site/`）。

## 一行接入

```html
<script src="https://<域名>/sdk.js"
        data-key="pk_site_xxx"
        data-host="https://api.<域名>"
        data-position="right"
        defer></script>
```

| data 属性 | 必填 | 说明 |
|---|---|---|
| `data-key` | 是 | 接入密钥（`pk_site_` 前缀；服务端绑定 Origin 白名单，盗用 → 40301） |
| `data-host` | 否 | API 域名，缺省同源推导 |
| `data-position` | 否 | 悬浮按钮位置 `left`/`right`（样式配置的一部分，远端 widget_config 优先级更高，FR-SAAS-006 预留） |

缺 `data-key` 时主体静默不启动、不发任何请求（宿主页零感知）。

## 初始化时序（SR-SITE-001 §6.2）

```
loader.js（defer，ES5）→ 异步注入 chat.js（不阻塞 LCP）
  → POST /api/v1/site/session（data-key + Origin 白名单校验）签发 visitor token
  → 语言检测 navigator.language → 支持语种 → 不命中回落 en
  → consent 判定（FR-SITE-007）：未同意 → 仅基础会话（不追踪/不持久化/邀聊关闭）
  → 挂载悬浮按钮（Shadow DOM 隔离）；首开面板建 WS 通道
```

## 行为与降级

- **WS 实时通道**：心跳 30s ping（连续 3 次无 pong 判死）；断线指数退避重连（2^n + 随机抖动，封顶 30s）；
  token 失效（close 4401）自动重走 `/site/session`；消息 `msg_id` 幂等，断线不丢（pending 重放）。
- **长轮询降级**：连续 3 次连接失败自动切 `GET/POST /api/v1/site/messages`，功能等价、延迟秒级。
- **样式隔离**：自定义元素 `<asa-chat>` open Shadow DOM；无 Shadow DOM 的旧 WebView 降级
  `asa-` 前缀作用域（外观一致）。
- **多语言**：`navigator.language` 检测 + en 兜底；运行时 `__asaChat` 切换（写会话 lang 驱动回复语言）。

## consent 门控（FR-SITE-007）

- 三态：`granted`（全部）/ `necessary`（仅必要）/ `denied`（拒绝），存 `localStorage['asa_consent']`；
- **未同意**：行为上报（`/api/v1/site/events`）零请求、主动邀聊关闭、token 仅存 sessionStorage
  （关页即弃）、不写其他持久存储；
- **同意后**：`visitor_uid` 写 `localStorage['asa_uid']`（跨会话匿名画像合并；清存储/无痕 = 新访客，
  不做跨设备指纹追踪）；
- 服务端二次校验兜底（40302 拒写），SDK 被篡改绕过也采集不到。

## 宿主 API（预初始化队列）

```js
window.__asaChat('track', 'order', 'ST-3200'); // 宿主回报下单（抵消弃购判定）
```

## 后端端点（src/services/site/api/）

| 端点 | 说明 |
|---|---|
| `POST /api/v1/site/session` | 会话初始化，签发 visitor token |
| `POST /api/v1/site/events` | 行为批量上报（consent 门控 + ev_id 幂等 + 单批 ≤50） |
| `POST /api/v1/site/chat` | 聊天（demo REST 形态；生产 WS `/ws/chat` 同协议） |
| `GET\|POST /api/v1/site/messages` | 长轮询降级通道 |
| `POST /api/v1/privacy/consent` | consent 三态落证 |
| `POST /api/v1/privacy/dsr/delete\|export` | DSR 工单（30 天时限） |

## 测试

SDK 源码静态结构断言：`src/services/site/tests/test_sdk_static.py`（读本目录源文件做正则检查：
一行接入/异步注入/ES5 门禁/WS 心跳与退避重连/长轮询降级/Shadow DOM/consent 门控/批量上报/i18n）。
