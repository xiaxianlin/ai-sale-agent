# 新租户纯配置接入冒烟报告（onboard_smoke）

| 属性 | 内容 |
|---|---|
| 执行时间 | 2026-10-11T12:31:34Z |
| 结论 | **PASS（全链通过）**（6/6 环节通过） |
| 口径 | 进程内 demo（P0 流水线/DNS/证书为 Fake）；生产以部署包复测 |
| 依据 | SR-SAAS-002 §6.5 七步接入清单 + SR-SAAS-006 白标链路 |

| # | 环节 | 结果 | 明细 |
|---|---|---|---|
| 1 | 四步配置（全 API） | PASS | uid=ob_aeaefa4332fc tenant=7000 预估 chunks=9，凭证掩码回显=True |
| 2 | 激活幂等 | PASS | kb_task=kbt_f59ace8062e4（重放同键同结果） |
| 3 | 知识初始化（重试闭环） | PASS | 瞬时抖动重试 attempts=2，确定性失败→failed→补跑 1 文件→active（percent=100.0） |
| 4 | 白标生效（ETag/304） | PASS | version=1 etag="t7-brand-v1" 304 零载荷=True |
| 5 | 自定义域名（注册-验证-证书-路由） | PASS | state=active cert=issued 路由 Host→7=7 |
| 6 | 零代码证据（七步清单/隔离） | PASS | drill 七步=True，素材前缀 t:7000/raw/ 全命中=True |

退出码：0
