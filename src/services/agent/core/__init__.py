# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""agent-runner 执行器核心包（FR-AG-001/002/008、NFR-REL-03/SEC-04，迭代 2.1）。

模块分工（SR-AG-002/008、SR-NFR-REL-03/SEC-04 §3 落点，demo 口径）：
    state        会话状态机：四态迁移表 / 乐观并发 / state_hist 留痕 / 发送闸门 / 超时巡检
    driver       浏览器驱动：BrowserDriver 协议 + FakeBrowserDriver（确定性模拟）
                 + OpenClawDriver（真实客户端接口，KB_OPENCLAW_ENDPOINT 未配置中文报错）
    engine       任务主循环：step() 单步可测（poll→read→process→send）、处理水位、拟人化 Typer
    recovery     故障恢复：会话重建 / 中断任务重放（幂等键 + CAS 防重发）/ RTO 计时
    vault        凭证保险库：信封加密 demo 形态 / 掩码 / DEK 轮换 / zeroize
    egress_check 出口 IP 自检纯函数：漂移判定 / 连续失败暂停（不换 IP 硬切）

下游一律从各模块导入，不触碰内部私有符号。
"""
# 知识产权归甲方所有（COM-IP-01）
