# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# connection.py —— PG 连接上下文与迁移执行器（真实存储层，迭代 0.3 容器化联调收口）。
"""PostgreSQL 连接管理（psycopg 3，生产/联调存储层的公共底座）。

    PgConnection(dsn)      连接上下文管理器：with 内复用单条连接，autocommit=False
                           （事务由 transaction() 显式提交/回滚），会话时区固定 UTC
                           （TIMESTAMPTZ 读回恒为 UTC 表示，对拍口径确定）；
    PgConnection.transaction()  单操作事务边界：yield cursor，成功 commit、
                           异常 rollback 后原样上抛（psycopg 连接级事务栈）；
    apply_migrations(dsn, sql_dir)  按文件名序幂等执行 migrations/*.sql
                           （脚本自身 IF NOT EXISTS，重复执行无害）。

设计口径：
- DSN 一律注入（本地联调缺省值见集成测试，库代码不内置凭证——NFR-SEC-04）；
- 连接失败抛 RuntimeError（中文错误，携带 DSN 主机/端口线索），由上层决定降级；
- 单连接多操作复用：与内存实现「同进程内共享存储」对齐；跨线程并发写
  由调用方各持一条连接（集成测试并发双写用例即此形态）。

依据：docs/02-设计文档/03-数据库设计.md §3.1（表结构权威）；
     migrations/001_kb_core.sql（建表脚本）。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import Cursor

#: 会话级连接参数：时区固定 UTC（TIMESTAMPTZ 统一以 UTC 读回，消除本机时区差异）
_SESSION_OPTIONS: str = "-c timezone=UTC"


def ensure_utc(moment: datetime | None) -> datetime | None:
    """时间戳归一：None 透传；naive 视为 UTC 并补时区（写入 TIMESTAMPTZ 前的兜底）。

    内存实现允许 naive datetime 原样存取；PG 形态经 TIMESTAMPTZ 必然带时区读回，
    为保证两侧行为可比，写入前统一归一为 UTC aware（对拍用例均显式传 aware 时间，
    本兜底仅防御外部灌数路径）。
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def utc_now() -> datetime:
    """UTC 当前时刻（与内存实现的时钟口径一致）。"""
    return datetime.now(timezone.utc)


def mask_dsn(dsn: str) -> str:
    """DSN 脱敏（CR S1 修复 / NFR-SEC-04）：``scheme://user:***@host:port/db``。

    - 密码段一律替换为 ``***``（无法证明无密码也统一掩码，fail-closed 口径）；
    - query 串整体丢弃——``?password=...`` 可藏在连接参数里，绝不随消息回显；
    - 主机/端口/库名保留：上层按主机线索降级排障（设计口径「携带 DSN 主机/端口线索」）；
    - 解析异常时兜底为 ``<DSN 已整体脱敏>``——脱敏函数自身绝不抛错掩盖原始故障。
    """
    try:
        parts = urlsplit(dsn.strip())
        scheme = parts.scheme.lower() or "postgresql"
        host = (parts.hostname or "").lower()
        if not host:
            return "<DSN 已整体脱敏>"
        user = parts.username
        try:
            port = parts.port
        except ValueError:
            port = None
        authority = f"{user}:***@" if user else "***@"
        if port is not None:
            authority += f"{host}:{port}"
        else:
            authority += host
        return f"{scheme}://{authority}{parts.path}"
    except Exception:  # noqa: BLE001 - 脱敏兜底：任何解析异常不掩盖原始连接错误
        return "<DSN 已整体脱敏>"


class PgConnection:
    """PG 连接上下文管理器：with 语句内复用单条 psycopg 连接（autocommit=False）。

    用法::

        with PgConnection(dsn) as conn:
            with conn.transaction() as cur:
                cur.execute("SELECT 1")

    - ``__enter__`` 建立连接（失败抛 RuntimeError 中文错误）；
    - ``transaction()`` 为每个存储操作提供事务边界（成功 commit / 异常 rollback）；
    - ``__exit__`` 关闭连接（未提交事务由关闭动作隐式回滚）。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn: str = dsn
        self._conn: psycopg.Connection | None = None

    @property
    def dsn(self) -> str:
        """注入的连接串（展示/日志口径一律先经 :func:`mask_dsn` 脱敏，CR S1）。"""
        return self._dsn

    @property
    def connected(self) -> bool:
        """连接是否已建立且未关闭。"""
        return self._conn is not None and not self._conn.closed

    def __enter__(self) -> PgConnection:
        """建立连接：autocommit=False + 会话 UTC；失败转中文 RuntimeError（DSN 先脱敏，CR S1）。"""
        try:
            self._conn = psycopg.connect(self._dsn, autocommit=False, options=_SESSION_OPTIONS)
        except psycopg.OperationalError as exc:
            # CR S1 修复：异常消息先经 mask_dsn 脱敏，密码段绝不随消息外泄（NFR-SEC-04）
            raise RuntimeError(
                f"PostgreSQL 连接失败（DSN={mask_dsn(self._dsn)}）：{exc}"
            ) from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        """关闭连接（简单收口：任何未决事务随连接关闭而回滚）。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self) -> Iterator[Cursor]:
        """单操作事务边界：yield cursor；正常退出提交、异常回滚并原样上抛。

        psycopg 连接级事务栈支持嵌套（内层降级为保存点），存储实现按
        「一方法一事务」使用即可；连接未建立时显式失败（fail-loud）。
        """
        if self._conn is None:
            raise RuntimeError("PG 连接未建立：PgConnection 必须在 with 语句内使用")
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                yield cur


def apply_migrations(dsn: str, sql_dir: str | Path) -> list[str]:
    """按文件名序幂等执行 ``sql_dir`` 下全部 ``*.sql`` 迁移脚本，返回执行清单。

    - 排序口径：文件名升序（``001_kb_core.sql`` → ``002_...``，与版本号命名约定一致）；
    - 幂等性：脚本自身以 ``IF NOT EXISTS`` 等幂等语句编写，重复执行无害
      （本函数不维护 schema_history 表，迁移簿记由部署侧承接）；
    - 每个脚本在独立事务内执行：单脚本失败即中止并回滚该脚本，已成功者保留。
    """
    directory = Path(sql_dir)
    scripts: list[Path] = sorted(directory.glob("*.sql"))
    if not scripts:
        raise FileNotFoundError(f"迁移目录不存在或无 .sql 脚本: {directory}")
    applied: list[str] = []
    with PgConnection(dsn) as conn:
        for script in scripts:
            sql = script.read_text(encoding="utf-8")
            with conn.transaction() as cur:
                cur.execute(sql)  # 无参数 → 简单查询协议，支持多语句脚本
            applied.append(script.name)
    return applied
