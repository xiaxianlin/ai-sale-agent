# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""SDK 会话 API 用例（FR-SITE-001：POST /api/v1/site/session，统一信封 + 错误码）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.site.api.app import SITE_SDK_KEY_ENV, SITE_TOKEN_SECRET_ENV, create_site_app
from services.site.core.errors import SiteConfigError

from .conftest import DEMO_KEY, DEMO_ORIGIN, TEST_SITE_SECRET


def build_client(**kw: object) -> TestClient:
    """装配被测应用（工厂缺省内存实现；密钥显式传 demo 值——CR P0-5 修复口径）。"""
    kw.setdefault("secret", TEST_SITE_SECRET)
    kw.setdefault("sdk_key", DEMO_KEY)
    return TestClient(create_site_app(**kw))


def test_session_endpoint_envelope_ok() -> None:
    """A1 会话端点：code=0 信封 + data 关键键齐全（等价类，04 §3.7 契约形态）。"""
    client = build_client()
    resp = client.post(
        "/api/v1/site/session",
        json={"key": DEMO_KEY, "page": "https://shop.example.com/p/1", "lang": "en-US"},
        headers={"Origin": DEMO_ORIGIN},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["trace_id"]
    data = body["data"]
    for field in ("visitor_token", "visitor_uid", "lang", "theme", "faq", "consent", "ws_url"):
        assert field in data
    assert data["lang"] == "en"
    assert data["consent"] == {"required": True, "granted": False}
    assert data["ws_url"] == "/ws/chat"


def test_session_endpoint_denials() -> None:
    """A2+A3 白名单外 Origin → 40301；未知 data-key → 40101（异常类）。"""
    client = build_client()
    bad_origin = client.post(
        "/api/v1/site/session", json={"key": DEMO_KEY},
        headers={"Origin": "https://evil.example.com"},
    )
    assert bad_origin.status_code == 403 and bad_origin.json()["code"] == 40301
    bad_key = client.post(
        "/api/v1/site/session", json={"key": "pk_site_nope"},
        headers={"Origin": DEMO_ORIGIN},
    )
    assert bad_key.status_code == 401 and bad_key.json()["code"] == 40101


def test_session_endpoint_missing_key_40001() -> None:
    """A4 缺 key（入参校验失败）→ 40001 信封（异常类，RequestValidationError 收敛）。"""
    client = build_client()
    resp = client.post("/api/v1/site/session", json={"page": "/"}, headers={"Origin": DEMO_ORIGIN})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_session_endpoint_lang_fallback_and_consent_flag() -> None:
    """A5+A6 非法 lang 回落 en；consent_required=false 租户透出（边界+等价类）。"""
    client = build_client(consent_required=False)
    resp = client.post(
        "/api/v1/site/session",
        json={"key": DEMO_KEY, "lang": "xx-YY"},
        headers={"Origin": DEMO_ORIGIN},
    )
    data = resp.json()["data"]
    assert data["lang"] == "en"
    assert data["consent"]["required"] is False


def test_missing_secrets_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR P0-5 修复：未传 secret/sdk_key 且对应环境变量未配置 → 启动即
    SiteConfigError（fail-fast，拒绝缺省密钥运行；对齐 vault 缺主密钥口径）。"""
    monkeypatch.delenv(SITE_TOKEN_SECRET_ENV, raising=False)
    monkeypatch.delenv(SITE_SDK_KEY_ENV, raising=False)
    with pytest.raises(SiteConfigError, match=SITE_TOKEN_SECRET_ENV):
        create_site_app()


def test_secrets_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量注入形态：secret/sdk_key 均可经 KB_SITE_* 环境变量注入（显式参数优先）。"""
    monkeypatch.setenv(SITE_TOKEN_SECRET_ENV, TEST_SITE_SECRET)
    monkeypatch.setenv(SITE_SDK_KEY_ENV, DEMO_KEY)
    client = TestClient(create_site_app())
    resp = client.post(
        "/api/v1/site/session",
        json={"key": DEMO_KEY, "page": "https://shop.example.com/p/1"},
        headers={"Origin": DEMO_ORIGIN},
    )
    assert resp.status_code == 200 and resp.json()["code"] == 0
