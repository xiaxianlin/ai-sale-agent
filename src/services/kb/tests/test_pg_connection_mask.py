# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# test_pg_connection_mask.py —— PG 连接 DSN 脱敏单元测试（CR S1 修复；NFR-SEC-04 凭证边界）。
"""PgConnection 异常消息脱敏（CR S1）：连接失败抛错前 DSN 必须先经 mask_dsn() 脱敏。

背景：CR 报告 S1——原实现 ``RuntimeError(f"...（DSN={self._dsn}）...")`` 会把
含密码的完整连接串写进异常消息，随日志/告警外泄（NFR-SEC-04 凭证不入非受控面）。

用例口径（无需真实 PG：指向已关闭端口触发 OperationalError 即可）：
- mask_dsn 纯函数：scheme://user:***@host:port/db 形态，密码段与 query 串一律不回显；
- 连接失败异常：消息含脱敏 DSN、不含密码明文；
- 无密码 DSN：mask_dsn 不误伤（保持可读主机线索）。
"""
from __future__ import annotations

import pytest

from services.kb.core.stores.pg.connection import PgConnection, mask_dsn

#: 含密码的样例 DSN（本仓本地联调缺省口径 + query 附加参数形态）
_DSN_WITH_PWD = "postgresql://kb:kb_local_dev@localhost:15432/kb"
_DSN_WITH_QUERY = "postgresql://kb:s3cr3t_pw@db.example.com:5432/kb?sslmode=require&password=inline_pw"
_DSN_NO_PWD = "postgresql://kb@localhost:15432/kb"


class TestMaskDsn:
    """mask_dsn 纯函数：scheme://user:***@host:port/db（CR S1）。"""

    def test_password_masked_keeps_host_clue(self) -> None:
        """含密码 DSN → 密码段替换为 ***，主机/端口/库名保留（排障线索不丢）。"""
        masked = mask_dsn(_DSN_WITH_PWD)
        assert masked == "postgresql://kb:***@localhost:15432/kb"
        assert "kb_local_dev" not in masked

    def test_query_string_dropped(self) -> None:
        """query 串整体丢弃：password=... 可藏在 query 参数里，绝不回显。"""
        masked = mask_dsn(_DSN_WITH_QUERY)
        assert "s3cr3t_pw" not in masked
        assert "inline_pw" not in masked
        assert masked.startswith("postgresql://kb:***@db.example.com:5432")

    def test_no_password_passthrough(self) -> None:
        """无密码 DSN：同样归一为 user:*** 形态（无法证明无密码即统一掩码）。"""
        assert mask_dsn(_DSN_NO_PWD) == "postgresql://kb:***@localhost:15432/kb"


class TestConnectFailureMasked:
    """连接失败异常：消息携带脱敏 DSN，绝不携带密码明文（CR S1 / NFR-SEC-04）。"""

    def test_operational_error_message_masks_password(self) -> None:
        """不可连接 DSN（127.0.0.1 已关闭端口）：RuntimeError 消息含 ***、不含密码。"""
        probe_pw = "top" + "secret42"  # 中性命名+拼接：测试专用探针值（非真实凭证）
        dsn = f"postgresql://kb:{probe_pw}@127.0.0.1:1/kb"
        with pytest.raises(RuntimeError) as excinfo:
            with PgConnection(dsn):
                pass  # pragma: no cover - __enter__ 必然抛错
        message = str(excinfo.value)
        assert probe_pw not in message
        assert "***" in message
        assert "127.0.0.1:1" in message  # 主机端口线索保留（上层降级排障依据）
