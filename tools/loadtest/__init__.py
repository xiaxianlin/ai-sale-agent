# 知识产权归甲方所有（COM-IP-01）
"""压测工具包（NFR-PER-02 验收跑分基线，迭代 0.2）。

两种形态共用同一判定口径（P95≤500ms 且 错误率<0.1%）：
- retrieval_load.py：纯 httpx 线程版（零压测框架依赖，本机即可运行）；
- retrieval_locust.py：locust 编排版（CI/联调，标准 locustfile 配置形态）。
"""
