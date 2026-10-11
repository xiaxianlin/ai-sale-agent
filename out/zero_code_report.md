# 零代码守护审计报告（NFR-SCA-02 / FR-SAAS-007）

## 1. 执行概要

- 被审计仓库根：`/Users/xiaxianlin/workspace/ai-sale-agent/repo`
- 执行时间：2026-10-11T12:31:34.223287+00:00
- 结论：**PASS**（违例 0 项）
- 出厂种子扫描：5 行；覆盖链检查：5 键

## 2. 依赖方向（不变式①）

规则：foundation 禁止反向 import services；产品侧禁止静态/动态引用 tenant-content。

## 3. 出厂种子（不变式②）

规则：全部种子挂默认域 tenant_id=0、登记于产品默认集、甲方关键词零命中（清单经 --keywords 参数注入，COM-IP-06 不入产品包）。

## 4. 覆盖链（不变式③）

规则：内置模板键在默认域全部可达；租户覆盖 > 平台默认，变更即时生效（新租户差异全走配置/模板覆盖，代码零分叉）。

## 5. 违例清单

- 无违例（三条不变式全部成立）

## 6. 复跑命令

```bash
python3 tools/saas/zero_code_check.py --repo . --report-out out/zero_code_report.md
```
