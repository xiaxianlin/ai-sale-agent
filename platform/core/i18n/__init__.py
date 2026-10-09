# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""界面文案中文化基座包（FR-KB-023 / NFR-USA-02，迭代 0.3）。

对外仅暴露 messages 子模块（错误码→中文可行动文案映射中心 + 术语表常量）；
按 SR-NFR-USA-02 §3 的上收决策，本包是全部服务端「吐给界面」文案的单一事实源。
"""
from platform.core.i18n.messages import (
    DEFAULT_MESSAGE,
    ERROR_CODES,
    ERROR_MESSAGES,
    SUCCESS_MESSAGE,
    ErrorMessage,
    TERMS,
    UI_STANDARD_TERMS,
    ZH_WHITELIST,
    is_registered,
    message,
    missing_codes,
    startup_selfcheck,
    to_hints,
)

__all__ = [
    "DEFAULT_MESSAGE",
    "ERROR_CODES",
    "ERROR_MESSAGES",
    "SUCCESS_MESSAGE",
    "ErrorMessage",
    "TERMS",
    "UI_STANDARD_TERMS",
    "ZH_WHITELIST",
    "is_registered",
    "message",
    "missing_codes",
    "startup_selfcheck",
    "to_hints",
]
