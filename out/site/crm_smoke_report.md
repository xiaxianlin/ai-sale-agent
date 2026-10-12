# Phase 3 独立站第二批 端到端冒烟报告（FR-SITE-004/005/006）

- 生成时间：2026-10-12T07:49:26+00:00
- 运行方式：`python3 tools/site/crm_smoke.py`（全内存确定性 Fake，零网络零 LLM）
- **demo 口径声明**：合成数据 + Fake SMTP/IMAP；正式验收须按 SRS §16.4 以服务商后台统计对账复测。

## 步骤断言（全部 PASS 才退出码 0）

| # | 步骤 | 结论 |
|---|---|---|
| ① | 三渠道线索归一化（alibaba/site/email） | PASS |
| ② | 五级瀑布：L5 新建 / L3 attach / 锚点跨渠道自动合并（恰一档案） | PASS |
| ③ | 合并可撤销：窗口内 undo 快照回滚 → 重并恢复 | PASS |
| ④ | 客户 360：三渠道时间线 4 条按时间归并，consent 过滤生效 | PASS |
| ⑤ | 弃购 → cart_recover 1h/24h/72h 排产 → 确定性送达/打开/硬退 | PASS |
| ⑥ | 邮件统计：sent=22 delivered=21 opened=1 failed=1；送达率 95.5% ≥95% 判定 PASS | PASS |
| ⑦ | V2 五指标/空日补零/漏斗/钻取/与 SITE-004 双读一致 | PASS |

## 关键数值

| 指标 | 值 | 口径 |
|---|---|---|
| 统一档案 | site=#2 ← alibaba=#1 | merged_into 指针 |
| 邮件送达率 | 95.45% | delivered ÷ sent（发送→送达口径） |
| 打开率 | 4.76% | opened ÷ delivered（像素去重） |
| 挽回成交 | 1 | 发送后 7d 窗口内首单 |
| UV / 会话 / 留资率 / 挽回率 | 1 / 1 / 100% / 100% | SR-SITE-006 §6.1 |
| SMTP 实发（Fake 接收） | 22 | 250=送达口径 |

## 追溯

- SR-SITE-004 §6/§9（序列/调度/追踪/统计）；SR-SITE-005 §6/§9（瀑布/合并/360）；SR-SITE-006 §6/§9（口径/预聚合/漏斗/钻取）；SRS §16.4（送达率 ≥95%）
- 用例证据：out/tdd_32_red.txt → out/tdd_32_green.txt（110 条）
