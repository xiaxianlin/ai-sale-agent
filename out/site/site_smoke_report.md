# Phase 3 独立站第一批全链路冒烟报告（FR-SITE-001/002/003/007）

- 生成时间：2026-10-12T07:49:26+00:00（UTC）
- 链路：session（Origin 白名单）→ consent → events/画像 → invite（阈值+日限）→ chat（画像注入+价格防线）→ leads（Customer 渠道 site）→ DSR（导出/删除）

> **demo 口径声明**：确定性 Fake 桩（无真实甲方素材、无 DeepSeek API Key）；
> 正式验收按 SRS §16.4 以真实浏览器矩阵 + 生产链路复测。

## 一、步骤明细

| 步骤 | 结果 |
|---|---|
| ① 会话初始化 | visitor_uid=ac93cb44… lang=en（白名单外 40301 已拦截） |
| ② 行为追踪 | 7 条事件全收（轨迹+ST-3200×3+停留 60s+加购）；画像已重建 |
| ③ 主动邀聊 | dwell/repeat_view 两次下发（模板槽位 ST-3200），第 3 次日限拦截 |
| ④ AI 对话 | 画像注入 Prompt（访客浏览过 ST-3200/来源 google）；价格泄漏走兜底话术 |
| ⑤ 留资 | customer_id=1（渠道 site，邮箱归一化）；非法表单已拒收 |
| ⑥ 隐私合规 | 撤回后 40302；DSR 导出 6 段；删除 {'visitor_event': 7, 'site_session': 1, 'visitor_profile': 1, 'conversation': 1, 'message': 4, 'site_lead': 1, 'customer': 1, 'mail_job': 0, 'site_invite': 2}；工单幂等 |

## 二、DSR 删除跨表计数

| 表 | 删除数 |
|---|---|
| conversation | 1 |
| customer | 1 |
| mail_job | 0 |
| message | 4 |
| site_invite | 2 |
| site_lead | 1 |
| site_session | 1 |
| visitor_event | 7 |
| visitor_profile | 1 |

## 结论

**通过**：全链路 6 步全绿，隐私红线（consent 撤回拒收/DSR 零残留）成立。
