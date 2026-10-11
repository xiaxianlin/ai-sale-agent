# Phase 1 KPI 预跑报告（SRS §16.2，迭代 1.3 收口）

- 生成时间：2026-10-11T02:22:57+00:00（UTC）
- 需求依据：SRS §16.2 Phase 1 验收 KPI；SR-TR-007/008；开发规范 §9.1（TDD）

> **demo 口径声明**：本预跑为工程自证证据——全部使用确定性合成语料/案例与
> Fake LLM 桩（无真实甲方素材、无 DeepSeek API Key、无真人评委/主管）。SRS
> §16.2 四项 KPI 的正式测量方式（导师抽检/老销售盲测/主管真人对比/真实业务月
> 成单率对比）须在甲方真实素材与 DeepSeek-V3 生产链路下复测；本报告②的盲测
> 人评与「培训周期缩短 ≥50%」「新人首月成单率」为**正式验收占位项**，不在
> 脚本化范围内。

## 一、§16.2 判定表

| # | KPI 项 | 目标 | 预跑实测 | 结论 |
|---|---|---|---|---|
| ① | 导师回答可溯源 | 100% | 27/27 命中回答有效引用并对回 chunk（100.0%）；零命中 3 条固定话术 | 通过 |
| ② | 模拟逼真度（结构口径） | 6 组断言全过 | 10 类型 × 5 轮演化/差异化、结束三分支 ['breakdown', 'deal', 'timeout']、复盘/幂等通过 | 通过 |
| ③ | 评分一致性 | ≥85% | 90.0%（9/10 组一致，frozen=False）| 通过 |
| ④ | 上岗拦截实测 | 未认证/过期 403 | 未认证 403/40301/E_CERT_REQUIRED；认证后 200 放行；过期 expired 再拦截；审计链 CERT_BLOCKED → CERT_EXPIRE → CERT_GRANT → EXAM_CREATED | 通过 |
| **总体判定** | — | — | — | **通过（exit 0）** |

## 二、口径映射说明（demo → 正式验收）

| KPI（SRS §16.2） | demo 预跑口径 | 正式验收口径（占位说明） |
|---|---|---|
| AI 导师回答准确率 ≥90% | 30 条抽检 100% 可溯源（qa_smoke 冻结链路重跑） | 双方联合抽检 ≥100 条、甲方真实素材、DeepSeek-V3 生产链路 |
| 模拟客户逼真度 ≥80% | 结构逼真度 6 组断言（状态演化/差异化/三分支/复盘/幂等） | 老销售盲测人评（≥3 名评委、四维 1-5 分量表，sim_realism_eval 取证） |
| 评分一致性 ≥85% | drill 模拟校准 10 组一致率 | 销售主管真人盲评 vs AI 对比采样（同 rubric 口径） |
| 上岗拦截（认证强制） | protected-demo 403/40301 实测 + 审计链 | 同机制在生产业务域写操作上验证（Phase 2 接管） |
| 培训周期缩短 ≥50% / 首月成单率 | 不适用（需真实业务月数据） | 正式验收占位：合同口径的对比组统计 |

## 三、各项明细

### ① AI 导师回答可溯源（KPI：回答准确率 ≥90% 的溯源口径）——通过

```json
{
  "corpus_chunks": 28,
  "hit_questions": 27,
  "traceability_rate": 100.0,
  "traceable": 27,
  "zero_hit_fixed_reply": 3
}
```

### ② 模拟客户逼真度 ≥80%（demo：结构逼真度；正式：老销售盲测）——通过

```json
{
  "branches": [
    "breakdown",
    "deal",
    "timeout"
  ],
  "note": "正式口径为老销售盲测人评（≥3 名评委四维量表，SR-TR-002 §6.4）——demo 以结构逼真度替代",
  "persona_types": 10,
  "structural_items": 6
}
```

### ③ 评分一致性 ≥85%（demo：模拟校准；正式：主管真人对比）——通过

```json
{
  "cases_imported": 100,
  "consistency_rate": 90.0,
  "consistent": 9,
  "frozen": false,
  "note": "正式口径为销售主管真人盲评对比采样（SRS §16.2）——demo 为模拟校准回路",
  "samples": 10,
  "threshold": 85.0
}
```

### ④ 上岗拦截实测（FR-TR-007 系统级强制，SRS §6.2/§16.2）——通过

```json
{
  "audit_chain": "CERT_BLOCKED → CERT_EXPIRE → CERT_GRANT → EXAM_CREATED",
  "block_expired": "expired",
  "block_uncertified": "403/40301/E_CERT_REQUIRED",
  "cert_status_final": "expired",
  "grant_after_pass": "certified",
  "pass_through": 200
}
```

