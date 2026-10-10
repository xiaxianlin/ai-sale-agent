"""知识产权归甲方所有（COM-IP-01）

FR-KB-031 密钥生命周期单元测试（签发/校验/吊销/轮换宽限期/IP 白名单）。

覆盖（SR-KB-031 §9 测试要点前两行 + §6.1/§6.2）：
- 签发：kbk_ 前缀明文一次性返回、库中仅存 SHA-256 + 末 4 位（明文永不在库）；
- 校验：正确 Key → 消费方上下文；错误/缺失/过短 Key → 40101（文案统一防探测）；
- 吊销：原 Key 立即 40101（RevokedKeyError 子类归因）；未知调用方幂等拒绝；
- 轮换：新 Key 立即生效；宽限窗口内旧 Key 可用、过期后 40101；连续轮换仅保留最近一代；
- IP 白名单：名单外/缺失来源 IP 拒绝（40301，fail-closed）；空名单不限制。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from foundation.apikey.keys import (
    KEY_PREFIX,
    ConsumerContext,
    DuplicateConsumerError,
    InvalidApiKeyError,
    IpWhitelistDeniedError,
    IssuedKey,
    KeyRegistry,
    KeyStatus,
    QuotaConfig,
    RevokedKeyError,
    RotatedKey,
    RotatedKeyExpiredError,
    UnknownConsumerError,
    generate_api_key,
    hash_key,
)


class FakeClock:
    """可推进的假时钟（UTC 感知）：宽限窗口边界判定的确定性时间源。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.moment = start or datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def registry(clock: FakeClock) -> KeyRegistry:
    return KeyRegistry(clock=clock)


def issue(registry: KeyRegistry, consumer: str = "agent-core", **kwargs: object) -> IssuedKey:
    """便捷签发（默认配额 qps=5/daily=100，对齐 SR §9 用例参数）。"""
    quota = kwargs.pop("quota", None) or QuotaConfig(qps=5, daily=100)
    return registry.issue(consumer, quota=quota, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- 签发


class TestIssue:
    def test_issue_returns_kbk_plaintext_once(self, registry: KeyRegistry) -> None:
        """签发返回 kbk_ 前缀 256bit 明文（仅此一次）与末 4 位（SR §9 签发-哈希入库）。"""
        issued = issue(registry)
        assert issued.api_key.startswith(KEY_PREFIX)
        assert len(issued.api_key) > 40  # kbk_ + token_urlsafe(32) ≈ 47 字符
        assert issued.key_tail == issued.api_key[-4:]
        assert issued.notice  # 一次性返回提示语

    def test_issue_stores_hash_not_plaintext(self, registry: KeyRegistry) -> None:
        """库中仅存 SHA-256 哈希 + key_tail，明文与哈希均不等于明文本身（NFR-SEC-04）。"""
        issued = issue(registry)
        record = registry.get("agent-core")
        assert record is not None
        assert record.api_key_hash == hash_key(issued.api_key)
        assert record.api_key_hash != issued.api_key
        assert record.key_tail == issued.api_key[-4:]
        assert record.status == KeyStatus.ACTIVE

    def test_issue_default_quota(self, registry: KeyRegistry) -> None:
        """缺省配额取全局默认（qps=20/daily=50000，SR §7）。"""
        issued = registry.issue("site-service")
        assert issued.quota == QuotaConfig(qps=20, daily=50000)

    def test_issue_custom_quota_and_whitelist(self, registry: KeyRegistry) -> None:
        """配额按 Key 独立配置 + IP 白名单绑定（04 §1.2 限流配置化）。"""
        issued = issue(registry, quota={"qps": 5, "daily": 100}, ip_whitelist=["10.0.0.9"])
        assert issued.quota.qps == 5 and issued.quota.daily == 100
        record = registry.get("agent-core")
        assert record is not None and record.ip_whitelist == ("10.0.0.9",)

    def test_issue_duplicate_consumer_rejected(self, registry: KeyRegistry) -> None:
        """重复 consumer 拒绝（UNIQUE(consumer) 应用层等价，SR §4）。"""
        issue(registry)
        with pytest.raises(DuplicateConsumerError):
            issue(registry)

    def test_issue_empty_consumer_rejected(self, registry: KeyRegistry) -> None:
        """空调用方标识拒绝（入参校验 40001）。"""
        with pytest.raises(UnknownConsumerError):
            registry.issue("  ")


# --------------------------------------------------------------------------- 校验


class TestVerify:
    def test_verify_valid_key_returns_context(self, registry: KeyRegistry) -> None:
        """正确 Key → 消费方上下文（consumer/配额/白名单快照注入，SR §6.2）。"""
        issued = issue(registry)
        context = registry.verify(issued.api_key, client_ip="10.0.0.1")
        assert isinstance(context, ConsumerContext)
        assert context.consumer == "agent-core"
        assert (context.qps, context.daily) == (5, 100)
        assert context.tenant_id == 1

    def test_verify_updates_last_used_at(self, registry: KeyRegistry) -> None:
        """鉴权成功刷新 last_used_at（管理端列表展示，SR §5）。"""
        issued = issue(registry)
        registry.verify(issued.api_key)
        record = registry.get("agent-core")
        assert record is not None and record.last_used_at is not None

    def test_verify_wrong_key(self, registry: KeyRegistry) -> None:
        """错误 Key → 40101/401，文案与缺失一致（防探测）。"""
        issue(registry)
        with pytest.raises(InvalidApiKeyError) as err:
            registry.verify(generate_api_key())
        assert err.value.code == 40101 and err.value.http_status == 401

    def test_verify_short_or_empty_key(self, registry: KeyRegistry) -> None:
        """过短/空 Key 直接 40101（畸形探测，SR §6.2 ①）。"""
        for bad in ("", "kbk_short", "x" * 19):
            with pytest.raises(InvalidApiKeyError):
                registry.verify(bad)


# --------------------------------------------------------------------------- 吊销


class TestRevoke:
    def test_revoke_then_verify_40101(self, registry: KeyRegistry) -> None:
        """吊销后携原 Key → 40101（RevokedKeyError 子类归因，文案不变，SR §9）。"""
        issued = issue(registry)
        revoked = registry.revoke("agent-core", operator="ops", reason="疑似泄露")
        assert revoked.status == KeyStatus.REVOKED and revoked.revoked_at is not None
        with pytest.raises(RevokedKeyError) as err:
            registry.verify(issued.api_key)
        assert err.value.code == 40101
        assert err.value.message == InvalidApiKeyError.message  # 防探测：与无效同文案

    def test_revoke_unknown_or_already_revoked(self, registry: KeyRegistry) -> None:
        """不存在/已吊销调用方再吊销 → 40001 幂等拒绝（SR §6.1 分支）。"""
        with pytest.raises(UnknownConsumerError):
            registry.revoke("nobody")
        issue(registry)
        registry.revoke("agent-core")
        with pytest.raises(UnknownConsumerError):
            registry.revoke("agent-core")

    def test_revoke_kills_grace_old_key(self, registry: KeyRegistry, clock: FakeClock) -> None:
        """吊销同时清除宽限旧哈希：泄露应急不允许轮换宽限期留后门（§2.2 一键吊销优先）。"""
        old = issue(registry)
        rotated = registry.rotate("agent-core", grace_seconds=300)
        registry.revoke("agent-core")
        clock.advance(10)  # 仍在名义宽限期内
        with pytest.raises(InvalidApiKeyError):  # 旧哈希已被清除 → 无主 40101
            registry.verify(old.api_key)
        with pytest.raises(RevokedKeyError):     # 当前哈希 → 吊销归因 40101
            registry.verify(rotated.api_key)


# --------------------------------------------------------------------------- 轮换（宽限窗口可配）


class TestRotate:
    def test_rotate_new_key_immediate_old_expired(self, registry: KeyRegistry) -> None:
        """默认宽限 0（SR §2.2 生产口径）：旧 Key 立即失效，新 Key 立即生效。"""
        old = issue(registry)
        rotated = registry.rotate("agent-core", operator="ops")
        assert isinstance(rotated, RotatedKey)
        assert rotated.api_key.startswith(KEY_PREFIX)
        assert rotated.old_key_valid_until is None and rotated.grace_seconds == 0.0
        registry.verify(rotated.api_key)  # 新 Key 可用
        with pytest.raises(RotatedKeyExpiredError):  # 旧 Key 立即 40101（子类归因）
            registry.verify(old.api_key)

    def test_rotate_grace_window(self, registry: KeyRegistry, clock: FakeClock) -> None:
        """宽限窗口内旧 Key 可鉴权、过期后 40101（宽限期边界判定，SR §2.2 基座形态）。"""
        old = issue(registry)
        rotated = registry.rotate("agent-core", grace_seconds=60)
        assert rotated.grace_seconds == 60
        assert rotated.old_key_valid_until == datetime(2026, 10, 9, 12, 1, 0, tzinfo=timezone.utc)
        clock.advance(30)
        assert registry.verify(old.api_key).consumer == "agent-core"       # 宽限期内旧 Key 可用
        assert registry.verify(rotated.api_key).consumer == "agent-core"   # 新 Key 始终可用
        clock.advance(31)  # 越过宽限截止
        with pytest.raises(RotatedKeyExpiredError):
            registry.verify(old.api_key)
        assert registry.verify(rotated.api_key).consumer == "agent-core"   # 新 Key 不受影响

    def test_rotate_twice_keeps_only_latest_generation(self, registry: KeyRegistry) -> None:
        """连续轮换：仅最近一代旧哈希留宽限，更早代际立即失效（防哈希代际堆积）。"""
        k1 = issue(registry)
        k2 = registry.rotate("agent-core", grace_seconds=300)
        k3 = registry.rotate("agent-core", grace_seconds=300)
        with pytest.raises(InvalidApiKeyError):
            registry.verify(k1.api_key)  # 第一代：哈希已无主 → 普通无效
        registry.verify(k2.api_key)      # 第二代：宽限期内
        registry.verify(k3.api_key)      # 当前代

    def test_rotate_default_grace_from_registry(self, clock: FakeClock) -> None:
        """registry 级默认宽限（default_grace_seconds）可配，rotate 不传参时生效。"""
        reg = KeyRegistry(clock=clock, default_grace_seconds=120)
        old = reg.issue("training-service")
        rotated = reg.rotate("training-service")
        assert rotated.grace_seconds == 120 and rotated.old_key_valid_until is not None
        clock.advance(60)
        reg.verify(old.api_key)  # 默认宽限期内可用

    def test_rotate_revoked_consumer_rejected(self, registry: KeyRegistry) -> None:
        """已吊销调用方不允许轮换（防绕过吊销审计，SR §6.1 分支）。"""
        issue(registry)
        registry.revoke("agent-core")
        with pytest.raises(UnknownConsumerError):
            registry.rotate("agent-core")


# --------------------------------------------------------------------------- IP 白名单


class TestIpWhitelist:
    def test_whitelist_allows_listed_ip(self, registry: KeyRegistry) -> None:
        """名单内来源 IP 放行（04 §1.2 IP 白名单绑定）。"""
        issued = issue(registry, ip_whitelist=["10.0.0.9", "10.0.0.10"])
        assert registry.verify(issued.api_key, client_ip="10.0.0.9").consumer == "agent-core"
        assert registry.verify(issued.api_key, client_ip="10.0.0.10").consumer == "agent-core"

    def test_whitelist_denies_other_ip(self, registry: KeyRegistry) -> None:
        """名单外来源 IP → 40301/403（SR §9 白名单用例：配置 [10.0.0.9] 从 10.0.0.8 调用）。"""
        issued = issue(registry, ip_whitelist=["10.0.0.9"])
        with pytest.raises(IpWhitelistDeniedError) as err:
            registry.verify(issued.api_key, client_ip="10.0.0.8")
        assert err.value.code == 40301 and err.value.http_status == 403

    def test_whitelist_denies_missing_ip_fail_closed(self, registry: KeyRegistry) -> None:
        """来源 IP 缺失（无对端信息）按不在白名单处理——fail-closed。"""
        issued = issue(registry, ip_whitelist=["10.0.0.9"])
        with pytest.raises(IpWhitelistDeniedError):
            registry.verify(issued.api_key, client_ip=None)

    def test_empty_whitelist_unrestricted(self, registry: KeyRegistry) -> None:
        """空白名单不限制来源（缺省行为）。"""
        issued = issue(registry)
        assert registry.verify(issued.api_key, client_ip="198.51.100.7").consumer == "agent-core"
