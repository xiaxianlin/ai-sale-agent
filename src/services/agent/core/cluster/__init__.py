# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""Agent 集群管理面（FR-SC-001，SR-SC-001 §3；Phase 4 demo 进程内形态）。

- :mod:`registry`——实例注册表（唯一事实源）：绑定不变式 + 状态机门禁 + 漂移检测；
- :mod:`orchestrator`——集群编排器：批量供给/实例级启停/故障隔离与退避重启；
- :mod:`callpool`——知识库集群调用池：查询指纹合并 + 两级缓存 + 降级链（FR-SC-002）；
- :mod:`gray`——灰度上线机制：分组/门禁三硬指标/放量回滚/决策记录（FR-SC-007）。

demo 形态边界：PG agent_instance / Redis 心跳与锁 / Docker Engine API 由生产形态承载；
本包以内存存储 + ContainerOps Protocol 注入等价实现，模块协议与生产一致（替换存储层时
接口不变）。容器操作经 :class:`orchestrator.ContainerOps` 协议注入（demo = FakeContainerOps
内存编排模拟，测试/演练用）。
"""
