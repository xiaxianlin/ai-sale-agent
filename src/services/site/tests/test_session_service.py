# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""SDK 会话域用例（FR-SITE-001 / SR-SITE-001 §9：visitor token 签发/域校验/续期/幂等）。

验收口径映射：SRS §16.4「聊天组件可用性」——会话建立是一行接入的第一步。
覆盖五类：等价类（S1/S4/S5）、边界（S10）、异常（S2/S3/S6~S9）、并发幂等（S11）。
"""
from __future__ import annotations

import pytest

from services.site.core.session.service import (
    SESSION_TTL_S,
    SessionError,
    VisitorTokenCodec,
    match_lang,
)

from .conftest import DEMO_KEY, DEMO_ORIGIN, FakeClock, make_key_store, make_session_service

# --------------------------------------------------------------------------- 等价类


def test_issue_and_verify_roundtrip() -> None:
    """S1 签发/校验回环：vt_ 前缀 + HMAC 结构（载荷.签名 两段），verify 还原 uid/tenant（等价类）。"""
    service, _ = make_session_service()
    init = service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN, lang="en-US")
    assert init.visitor_token.startswith("vt_")
    assert "." in init.visitor_token[len("vt_"):]
    claims = service.verify_token(init.visitor_token)
    assert claims.visitor_uid == init.visitor_uid
    assert claims.tenant_id == 1
    assert claims.exp > 0


def test_renew_keeps_uid_and_extends_exp() -> None:
    """S4 续期：uid/tenant 不变、exp 随时间顺延（等价类）。"""
    clock = FakeClock()
    service, _ = make_session_service(clock)
    init = service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN)
    old_exp = service.verify_token(init.visitor_token).exp
    clock.advance(3600.0)
    renewed = service.renew_session(init.visitor_token)
    claims = service.verify_token(renewed)
    assert claims.visitor_uid == init.visitor_uid
    assert claims.exp == pytest.approx(old_exp + 3600.0)


def test_origin_within_whitelist_and_widget_defaults() -> None:
    """S5+S12 白名单内 Origin 建会话：widget_config 落位 theme/faq；缺省配置时空但不缺失（等价类）。"""
    keys = make_key_store(
        widget_config={
            "theme": {"primary": "#0a7cff", "position": "left"},
            "faq": [{"id": "f1", "q": "MOQ & lead time"}],
        }
    )
    service, _ = make_session_service(keys=keys)
    init = service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN, lang="zh-CN")
    assert init.lang == "zh"
    assert init.theme["primary"] == "#0a7cff" and init.faq[0]["id"] == "f1"
    assert init.ws_url == "/ws/chat"
    # 缺省 widget_config：theme/faq 空形态（一行接入最小可用）
    bare, _ = make_session_service()
    bare_init = bare.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN)
    assert bare_init.theme == {} and bare_init.faq == []
    assert bare_init.consent_required is True


# --------------------------------------------------------------------------- 边界


def test_lang_mapping_and_fallback() -> None:
    """S10 语言映射与回落：en-US→en、zh→zh、ko-KR→en（支持语种之外的 BCP47 回落 en）。"""
    assert match_lang("en-US") == "en"
    assert match_lang("zh") == "zh"
    assert match_lang("ko-KR") == "en"
    assert match_lang("") == "en"


# --------------------------------------------------------------------------- 异常


def test_tampered_and_garbage_token_rejected() -> None:
    """S2 篡改/异形 token 拒绝 40101（HMAC 签名校验 + 格式兜底，异常类）。"""
    service, _ = make_session_service()
    init = service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN)
    tampered = init.visitor_token[:-2] + ("aa" if not init.visitor_token.endswith("aa") else "bb")
    with pytest.raises(SessionError) as ei:
        service.verify_token(tampered)
    assert ei.value.code == 40101
    codec = VisitorTokenCodec("test-secret")
    for garbage in ("", "abc", "vt_", "vt_eyJ9.sig"):
        with pytest.raises(SessionError):
            codec.verify(garbage)


def test_expired_token_rejected() -> None:
    """S3 过期 token 拒绝 40101（可拨时钟跨 TTL）；缺省 TTL=86400s（SITE_SESSION_TTL_S 口径）。"""
    assert SESSION_TTL_S == 86400
    clock = FakeClock()
    service, _ = make_session_service(clock, ttl_s=60.0)
    init = service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN)
    clock.advance(61.0)
    with pytest.raises(SessionError) as ei:
        service.verify_token(init.visitor_token)
    assert ei.value.code == 40101


def test_origin_outside_whitelist_rejected_40301() -> None:
    """S6 白名单外 Origin 拒绝 40301，且错误信息不泄露 key 有效性细节（异常类）。"""
    service, _ = make_session_service()
    with pytest.raises(SessionError) as ei:
        service.create_session(key=DEMO_KEY, origin="https://evil.example.com")
    assert ei.value.code == 40301
    assert DEMO_KEY not in str(ei.value)


def test_unknown_and_revoked_key_rejected_40101() -> None:
    """S7+S8 data-key 无效（未知）或已吊销均拒绝 40101（异常类）。"""
    service, _ = make_session_service()
    with pytest.raises(SessionError) as ei:
        service.create_session(key="pk_site_unknown", origin=DEMO_ORIGIN)
    assert ei.value.code == 40101
    keys = make_key_store()
    keys.set_status(DEMO_KEY, "revoked")
    revoked_service, _ = make_session_service(keys=keys)
    with pytest.raises(SessionError) as ei:
        revoked_service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN)
    assert ei.value.code == 40101


@pytest.mark.parametrize("bad_key", ["", "sk_live_abc", "pk_site_"])
def test_malformed_key_rejected_40001(bad_key: str) -> None:
    """S9 key 缺失/格式错（非 pk_site_ 前缀/空前缀体）→ 40001（异常类）。"""
    service, _ = make_session_service()
    with pytest.raises(SessionError) as ei:
        service.create_session(key=bad_key, origin=DEMO_ORIGIN)
    assert ei.value.code == 40001


def test_verify_unknown_session_rejected() -> None:
    """S9b token 签名有效但会话已被删（如 DSR 清除）→ 40101（异常类）。"""
    service, sessions = make_session_service()
    init = service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN)
    sessions.delete_uids([init.visitor_uid])
    with pytest.raises(SessionError) as ei:
        service.verify_token(init.visitor_token)
    assert ei.value.code == 40101


# --------------------------------------------------------------------------- 并发/幂等


def test_existing_uid_reused_idempotent() -> None:
    """S11 跨会话访客识别：existing_uid 复用同号不换新（localStorage asa_uid 语义，幂等）。"""
    clock = FakeClock()
    service, _ = make_session_service(clock)
    first = service.create_session(key=DEMO_KEY, origin=DEMO_ORIGIN)
    clock.advance(120.0)  # 后会话晚于前会话：token 载荷 exp 不同（新签）
    second = service.create_session(
        key=DEMO_KEY, origin=DEMO_ORIGIN, existing_uid=first.visitor_uid
    )
    assert second.visitor_uid == first.visitor_uid
    assert second.visitor_token != first.visitor_token  # token 每会话新签
