# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004/005/006（第二批）测试共用确定性 Fake 与工具（TDD 阶段①②）。

- :class:`MutableClock`——可手动推进的确定性时钟（全部核心模块注入 ``clock`` 可调用）；
- :class:`FakeSmtpTransport`——SMTP 发送协议（:class:`services.site.core.mailing.scheduler.MailTransport`）
  的确定性 Fake：默认 100% 接收（250=送达口径），可脚本化软失败（4xx 重试路径）；
  硬/软退信由 DeliveryTracker 事件直接注入（IMAP DSN webhook 形态），确定性可复现。

依据文档：docs/07-实现方案/SITE/SR-SITE-004 §6/§9、SR-SITE-005 §6/§9、SR-SITE-006 §6/§9。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from services.site.core.mailing.scheduler import SmtpSoftError  # 协议契约异常（唯一权威定义）

#: 全批测试统一的时间原点（UTC，tz-aware；所有模块只处理 aware datetime）
T0: datetime = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)


class MutableClock:
    """确定性测试时钟：``now()`` 返回当前时刻，``advance`` 手动推进（替代 sleep）。"""

    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        """当前时刻（tz-aware UTC）。"""
        return self._now

    def advance(self, **delta: Any) -> datetime:
        """按关键字参数推进（如 ``advance(hours=1)``），返回推进后的时刻。"""
        self._now = self._now + timedelta(**delta)
        return self._now

    def advance_to(self, moment: datetime) -> datetime:
        """推进到指定绝对时刻（用于按 T0+offset 的边界用例）。"""
        self._now = moment
        return self._now


@dataclass(frozen=True)
class SentMail:
    """一封已被 SMTP 接收的邮件快照（供断言收件人/主题/退订头）。"""

    to: str
    subject: str
    html: str
    headers: dict[str, str]
    message_id: str
    at: datetime


class FakeSmtpTransport:
    """SMTP 发送 Fake：确定性接收 + 可脚本化软失败（降级路径测试用）。"""

    def __init__(self, clock: MutableClock | None = None) -> None:
        self.clock = clock or MutableClock()
        self.sent: list[SentMail] = []
        self._soft_fail_left: dict[str, int] = {}

    def fail_next(self, email: str, times: int = 1) -> None:
        """脚本化：该收件人接下来 ``times`` 次发送抛 :class:`SmtpSoftError`。"""
        self._soft_fail_left[email.lower()] = times

    def send(self, to: str, subject: str, html: str, headers: dict[str, str]) -> str:
        """发送接口：软失败脚本命中则抛错；否则记录并返回 message_id（250=服务商接收）。"""
        left = self._soft_fail_left.get(to.lower(), 0)
        if left > 0:
            self._soft_fail_left[to.lower()] = left - 1
            raise SmtpSoftError("451 4.7.1 temporary local problem")
        message_id = f"<{len(self.sent) + 1:04d}@em1.demo-mail>"
        self.sent.append(
            SentMail(to=to, subject=subject, html=html, headers=dict(headers),
                     message_id=message_id, at=self.clock.now())
        )
        return message_id
