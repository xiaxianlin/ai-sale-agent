# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""后台易用性工具包（FR-KB-023 / NFR-USA-01 / NFR-USA-02，迭代 0.3）。

四个交付物（均可 ``python3 tools/usability/<脚本>.py`` 直跑，脚本内自举仓库根路径）：
- ``operations.yaml``    运维操作→API 声明清单（无 SQL 覆盖核对的单一事实源）；
- ``coverage_check.py``  无 SQL/命令行覆盖核对：AST 提取 services/kb/api/ 全部端点，
                         与 operations.yaml 比对，缺 API 支撑的操作报缺口；
- ``task_sheet.py``      甲方实测任务单生成器（20 任务 → xlsx + md，附实测记录表）；
- ``zh_scan.py``         界面文案中文扫描（错误 message / schema label 英文残留）。
"""
