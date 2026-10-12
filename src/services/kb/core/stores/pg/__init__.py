# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# stores/pg 包 —— PostgreSQL 真实存储层公开门面（迭代 0.3 容器化联调收口）。
"""PG 存储实现包：三大存储端口的 psycopg 3 实现 + 连接/迁移底座。

    connection       PgConnection（连接上下文 + 事务边界）、apply_migrations、
                     ensure_utc/utc_now（时间归一工具）
    item_store       PsycopgKnowledgeItemStore（api/admin/items.py 协议）
    version_store    PsycopgVersionStore（core/versioning/store.py 协议）
    ingest_log_store PsycopgIngestLogStore（core/reporting/cleaning_report.py 协议）

三个实现与各自内存实现**行为对拍**（同操作序列同结果，集成测试
tests/integration/test_pg_stores.py 守护）；DSN 一律注入，本地联调缺省
postgresql://kb:***@localhost:15432/kb（凭证见 deploy/compose/compose.yaml，
生产经密钥管理注入——NFR-SEC-04）。
"""
from __future__ import annotations

from .connection import PgConnection, apply_migrations, ensure_utc, mask_dsn, utc_now
from .ingest_log_store import PsycopgIngestLogStore
from .item_store import PsycopgKnowledgeItemStore
from .version_store import PsycopgVersionStore

__all__ = [
    "PgConnection",
    "PsycopgIngestLogStore",
    "PsycopgKnowledgeItemStore",
    "PsycopgVersionStore",
    "apply_migrations",
    "ensure_utc",
    "mask_dsn",
    "utc_now",
]
