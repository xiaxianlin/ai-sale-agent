<!-- 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。 -->
# deploy/backup —— 数据备份恢复 runbook（NFR-REL-04）

| 文件 | 说明 |
|---|---|
| `runbook.md` | 灾难场景恢复 runbook（三场景逐步操作 + RTO/RPO 计量记录表模板）+ 季度恢复演练 checklist |

依据：`docs/07-实现方案/NFR/SR-NFR-REL-04-数据备份.md`（工作区文档库，仓库外路径 `../docs/07-实现方案/NFR/`）§6.2（RPO 达成）、§6.5（演练工程）、§6.6（场景分级）；`docs/02-设计文档/03-数据库设计.md` §6。配套代码：`platform/core/backup/`（planner 对象矩阵 + pipeline 流水线/manifest/保留清理）。

## 0. 备份域与口径速查（操作前必读）

| 对象 | 备份方式 | OSS 备份域前缀 | 保留 | 实际 RPO |
|---|---|---|---|---|
| PostgreSQL（业务库全量，**唯一权威源**） | pg_basebackup 每日全量 + WAL 连续归档（archive_command 直传 OSS） | `backup/pg/base/YYYYMMDD/`、`backup/pg/wal/` | 30 天 / WAL 35 天 | **≤5min**（误删场景 PITR ≈0） |
| Milvus（knowledge_chunks 集合，可由 PG 重建） | milvus-backup → OSS | `backup/milvus/YYYYMMDD/` | 30 天 | 24h（兜底重建可到 PG 水位） |
| OSS 素材（知识原始文件，原始权威资产） | Bucket 版本化 + 跨区复制 | （素材 Bucket 自身，非备份域） | 永久 | ≈0 |
| 配置与密钥引用 | app_config 逻辑导出 + KMS/secret 引用清单（**零密钥本体**） | `backup/config/YYYYMMDD/` | 30 天 | 24h |

- 备份域为独立 Bucket `{{BACKUP_BUCKET}}`（与生产素材 Bucket 分离），全对象 SSE-KMS 加密、私有读；
- 每日流水线 02:00-05:00 低峰窗口触发（`platform/core/backup/pipeline.py` BackupRunner），
  manifest 逐项 sha256 登记于当日运行台账；
- **RPO 原理**：每日基础备份只决定恢复基线新旧，不决定 RPO；真实丢失窗口 =
  `archive_timeout(5min) + 上传时延(秒级)`，故 PG 实际 RPO ≤5 分钟（分钟级）。

环境变量（secrets 注入，不落本文件）：`BACKUP_BUCKET` / `KMS_ID` / `OSS_ENDPOINT` / PG 连接串与 `backup_user` 凭证。

## 1. 场景 A：误删表 / 误删数据（PITR 到事发前一秒）

**判定信号**：应用报错 + 审计日志（audit_log 哈希链）定位误操作时刻 `t`（NFR-SEC-03 联动）。
**恢复路径**：新实例恢复最新基线 → PITR 回放到 `t−1s` → 核对后切流。**RTO 目标 ≤2h，RPO ≈0。**

1. **定位与决策（≤10min）**
   - 审计日志确认误操作时刻 `t`（精确到秒）与影响对象（表名/行范围）；
   - 确认目标恢复点 `recovery_target_time = t − 1s`；通知业务方进入只读/停写窗口。
2. **起隔离恢复实例（≤15min）**：独立容器/端口/网络，**绝不直连生产实例覆盖**。
   ```bash
   # 拉取事发前最近一次基础备份（保留 30 天，任取其一不早于事发日）
   ossutil cp -r oss://$BACKUP_BUCKET/backup/pg/base/<YYYYMMDD>/ /restore/base/ --sse-kms $KMS_ID
   ```
3. **配置 WAL 回放（PITR）**：`/restore/base/` 下建 `recovery.signal` 与 `postgresql.auto.conf`：
   ```ini
   restore_command = 'ossutil cp oss://$BACKUP_BUCKET/backup/pg/wal/%f /restore/wal/%p --sse-kms $KMS_ID'
   recovery_target_time = '<t − 1s，如 2026-10-09 14:29:59+08>'
   recovery_target_action = 'promote'
   ```
4. **回放与就绪（≤60min，库容 ≤200GB 内）**：启动恢复实例，观察日志回放至目标时刻自动 promote；
   `pg_isready` 就绪后进入核对。
5. **完整性核对（§4 季度演练同一套断言）**：
   - 行数比对：恢复库核心表 `count(*)` 与备份台账 `backup_job_run.row_counts` 基线逐一相等；
   - 抽样验证：误删表抽样 N 行与业务方确认；确认误操作**未**出现在恢复库。
6. **切流（≤20min）**：应用数据源指向恢复实例（或逻辑导出受损表回灌生产），恢复业务写；动作写审计。
7. **计量记录**：填写 §3 RTO/RPO 记录表（RTO=业务恢复可用时刻−开始时刻；RPO=恢复出的最新事务时刻与 `t` 的差，理论 ≈0）。

## 2. 场景 B：库损坏 / 存储卷故障（回放到最新）

**判定信号**：PG 实例起不来 / `pg_verifybackup` 失败 / 存储卷不可写。
**恢复路径**：新卷恢复最新基线 + 全量 WAL 回放到最新。**RTO 目标 ≤2h（库容 ≤200GB 内），RPO ≤5min。**

1. **止损**：停止写入依赖该库的服务（检索降级预案见 NFR-REL-03）；确认旧卷只读保留取证。
2. **起恢复实例**：同场景 A 步骤 2-3，但 `recovery_target_time` 省略（或 `latest`）——回放全部已归档 WAL 到最后一致点。
3. **就绪核对**：同场景 A 步骤 5；RPO = 故障时刻 − 恢复出的最新已提交事务时刻（预期 ≤5min，
   即最后一段归档 WAL 的水位）。
4. **切流与善后**：同场景 A 步骤 6-7；事后跑一次全量备份重置基线，并检查 `backup/pg/wal/` 连续性无缺口。
5. **Milvus 联动检查**：若故障窗口内知识有更新，按需走场景 C 的重建路径把向量库补齐到 PG 新水位。

## 3. RTO/RPO 计量记录表模板（每次真实恢复与季度演练均须填写归档）

| 字段 | 记录 |
|---|---|
| 事件编号 / 日期 | DR-YYYYMMDD-NN / ____ |
| 场景 | A 误删 / B 库损坏 / C Milvus / D OSS 素材（真实事件或演练） |
| 所用备份日期 / manifest sha256 | ____ / ____ |
| 开始时刻 T0（决策入场） | ____ |
| 基线下载完成时刻 | ____ |
| WAL 回放完成 / 实例就绪时刻 | ____ |
| 完整性核对完成时刻（行数比对/抽样结论） | ____ |
| 业务恢复可用时刻 T1 | ____ |
| **RTO 实测（T1−T0）** | ____（目标：PG ≤2h；Milvus 快照 ≤4h / 重建 ≤8h） |
| 故障/注入时刻 T2（RPO 计量锚点） | ____ |
| 恢复出的最新事务时刻 T3 | ____ |
| **RPO 实测（T2−T3）** | ____（目标：PG ≤5min；误删场景 ≈0；Milvus 快照 24h/重建与 PG 同水位） |
| 遗留问题与改进项 | ____ |
| 操作人 / 复核人（双人签字） | ____ / ____ |

## 4. 场景 C：OSS 素材丢失 / 误删（版本化恢复）

**判定信号**：素材对账任务缺口 / 知识详情页原始文件 404。**RTO 目标 ≤4h，RPO ≈0（版本化）。**

1. **定位对象**：对账任务给出缺失对象键清单（bucket/key + 期望版本）；
2. **版本恢复（常见：误删当前版本）**：
   ```bash
   ossutil ls oss://<素材BUCKET>/<前缀>/ --all-versions   # 找到删除前的版本 ID
   ossutil cp oss://<素材BUCKET>/<key>?versionId=<VID> oss://<素材BUCKET>/<key> --meta-directive replace
   ```
3. **区域级故障（Bucket 整体不可用）**：走跨区副本回拷（目标区同加密同权限，§8 备份安全），
   优先恢复知识库引用的高频对象；
4. **核对与记录**：重跑对账任务至零缺口；填 §3 记录表（场景 D/素材行）；动作写审计。
5. **Milvus 附带说明**：若向量库本身损坏（健康检查/检索失败），优先
   `milvus-backup restore -n backup_<YYYYMMDD>`（快照 ≤4h）；快照不可用则从 PG 权威源
   经知识流水线重向量重建（BGE-M3 本地，≤8h，重建到 PG 水位）——重建脚本即季度演练场景。

## 5. 季度恢复演练 checklist（每季度 ≥1 次，验收证据）

按 SR-NFR-REL-04 §6.5 在**隔离环境**执行（独立容器/端口/网络，绝不触碰生产实例），与测试方案 §5「备份恢复」同一套断言：

- [ ] 1. 取号与备案：演练日期、目标备份日期、场景（pitr_latest / pitr_mistake / milvus_snapshot / milvus_rebuild / oss_version）；
- [ ] 2. 隔离环境就绪声明：容器/端口/网络与生产零共享，双人确认；
- [ ] 3. 从备份域拉取指定日期基线（校验 manifest 逐项 sha256 与运行台账一致）；
- [ ] 4. WAL 回放：pitr_mistake 场景 `recovery_target_time` 定到注入误操作前一秒；
- [ ] 5. Milvus：快照场景 restore；重建场景走知识流水线重向量（验证兜底路径真实可用）；
- [ ] 6. 完整性核对①：恢复库核心表行数与 `backup_job_run.row_counts` 基线逐一相等（少一行即 fail）；
- [ ] 7. 完整性核对②：20 条样例 query 统一检索 API，Top5 与备份时点记录一致率 ≥90%；
- [ ] 8. 计量：RTO / RPO 按本文件 §3 模板实测记录（脚本计量，非人工估算）；
- [ ] 9. 篡改演练：故意损坏一份备份 → 次日校验必须失败并告警（证明校验真实有效）；
- [ ] 10. 报告归档：渲染演练报告（步骤耗时分解/行数比对明细/一致率/RTO-RPO 对照/改进项/双人签字）
       → 上传 OSS → 写 `restore_drill_log` 台账；任一核对不过 verdict=fail 并开改进项跟踪；
- [ ] 11. 合规复查：近 30 天 `backup/pg/base/` 每日均有 success（连续性无缺口）；>31 天对象清理审计计数与 OSS 生命周期一致。

## 6. 安全红线（恢复操作同样适用）

- 备份介质零密钥本体：恢复涉及凭证轮换走 KMS SOP（场景 E，NFR-SEC-04），**不得**把密钥写进任何脚本/记录表；
- 恢复/演练/GC 失效处置均属敏感操作：全程动作写审计日志哈希链（NFR-SEC-03）；
- 恢复出的库含真实业务数据：隔离环境用后即毁，禁止复用作开发库。
