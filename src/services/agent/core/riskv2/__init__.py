# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""多账号风控 V2 扩展模块（FR-SC-004，SR-SC-004 §3；risk-engine V2 扩展的 demo 进程内形态）。

- :mod:`profiles`——账号行为画像：分布参数数据驱动（种子/拟合/EWMA 漂移）；
- :mod:`stagger`——错峰调度：黄金比例窗口起点错开 + 槽位配额；
- :mod:`uniqueness`——内容防同质：simhash 指纹库 24h 比对 + 强制重写钩子；
- :mod:`correlation`——账号关联风险监控：时间相关系数 / 内容相似率。

与 V1（:mod:`services.agent.core.risk`）的关系：只扩展不替换——V1 判定总门/Permit 机制/
fail-safe 原则不变，本包为其提供账号级分布参数、错峰窗口与内容防同质判定（SR-SC-004 §2
「V1 分布→账号级分布」零破坏升级）。demo 形态边界：PG behavior_profile/content_fingerprint
与 Redis 键由生产形态承载，存储替换时模块协议不变。
"""
