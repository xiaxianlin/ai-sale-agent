# AI 外贸销售智能体系统

代码仓库（Phase 0 迭代 0.1 起）。需求与设计见 `../docs/`：SRS → 需求拆分清单（92 子需求）→ 07-实现方案（每子需求工程方案）→ 05-项目管理/03-开发计划（迭代编排）。

## 结构（开发规范 §2）

```
src/         src 布局：foundation/（通用基座：配置中心/RBAC/审计/租户上下文，原 platform——避标准库冲突改名）+ services/
services/    业务服务：kb（知识库）/ training / agent-core / agent-runner / site / saas（按阶段落位）
tools/       工程门禁与交付：CI 门禁 / 架构守护 / 交付流水线
migrations/  数据库迁移（alembic，随迭代补齐）
deploy/      部署配置（nginx TLS / compose / helm，随阶段补齐）
docs/openapi/ 接口契约（随 04-接口设计 演进）
```

## 开发约定

- 全部代码中文注释（COM-IP-04），关键逻辑附 `@doc` 指向设计文档章节。
- 提交信息 `<type>: <摘要> (需求编号)`；PR 经 CI 六门禁（开发规范 §5）。
- 测试：`python -m pytest`；本仓库当前覆盖迭代 0.1 范围（平台基座 + KB-010/011/012）。
