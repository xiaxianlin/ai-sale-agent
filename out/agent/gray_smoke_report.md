# 灰度上线端到端冒烟报告（gray_smoke）

- 生成时刻：2026-10-11T18:22:38
- 演示计划：GRAY-SMOKE-2026-001（28 实例 / pilot 10 / 观察窗 14 天）

> **demo 口径声明**：本报告为内存合成数据 + 注入达标指标的工程自证证据（无真实账号/平台/容器/Milvus）；生产验收必须按 SRS §16.3/§16.5 以真实运行 14 天观察窗数据复测。

## ① 集群注册与批量供给（FR-SC-001）

- 绑定清单：28 实例（account↔container↔cookie↔proxy_ip 一一对应）
- 供给结果：created=28，batches=6，by_status={'running': 28}
- 容器操作留痕：start × 28（内存编排模拟）

## ② 操作时段错峰（FR-SC-004-②）

- 28 账号窗口起点两两互异：28/28（黄金比例低差异序列，无同窗口）
- 槽位配额：3 槽，最大同槽在线 10/10

## ③ 内容防同质（FR-SC-004-③）

- 首发：ok；第二账号同文案：ok:rewritten（强制重写 1 次后过检，同质内容不上线）
- 指纹登记：2 条（24h 滚动窗口口径）

## ④ 知识库集群调用池（FR-SC-002）

- 50 线程同指纹并发：回源 1 次、合并 49 次（singleflight 削峰，结果零竞态）

## ⑤ 灰度分组（FR-SC-007 §6-1）

- 分组计数：pilot=10 / control=18（组级配置隔离：pilot→v2，control→v1）

## ⑥ 放量门禁（FR-SC-007 §6-2/6-3）

- ban_signal：value=0 threshold=0 → PASS
- reply_usable_rate：value=0.86 threshold=0.8 → PASS
- sessions_per_account_day：value=16.2 threshold=15.0 → PASS
- 判定：passed（观察窗恰满 14+1 天后判定，注入达标指标）
- 放量：pilot→wave2（18 账号，4 批错峰重启，每批 ≤5）

## ⑦ 决策记录（§15.3 付款节点证据链）

- 台账 3 条（时间/动作/依据/审批人，只追加时序单调），明细见下表

## 决策记录台账（§15.3 付款节点证据链）

| 决策时间 | 动作 | 审批人 | 依据摘要 |
|---|---|---|---|
| 2026-10-12T09:00:00 | plan_create | system | plan_no=GRAY-SMOKE-2026-001; target_version=strategy:v2; baseline_version=strategy:v1 |
| 2026-10-27T09:00:00 | gate_pass | system | plan_no=GRAY-SMOKE-2026-001; gate_checks=[{'metric': 'ban_signal', 'value': 0, 'threshold': 0, 'passed': True}, {'metric': 'reply_usable_rate', 'value': 0.86, 'threshold': 0.8, 'passed': True}, {'metric': 'sessions_per_account_day', 'value': 16.2, 'threshold': 15.0, 'passed': True}] |
| 2026-10-27T09:00:00 | rollout | 王审核（demo） | plan_no=GRAY-SMOKE-2026-001; to_group=wave2; accounts=18 |

---

知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。