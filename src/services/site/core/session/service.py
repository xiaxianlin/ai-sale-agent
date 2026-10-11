# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""访客会话与 visitor token（FR-SITE-001，SR-SITE-001 §6.7 / 04-接口设计 §1.2、§3.7）。

职责：
- data-key 校验（SHA-256 哈希存储，明文不落库）+ Origin 白名单（防 data-key 盗用，40301）；
- visitor token 签发/校验/续期：``vt_<base64url(载荷)>.<HMAC-SHA256 签名>``，
  仅限本访客会话（04 §1.2），TTL 默认 86400s（SITE_SESSION_TTL_S）；
- 语言检测复检（navigator.lang → 支持语种，不中回落 en，防客户端伪造异常值）；
- 跨会话访客识别：existing_uid（SDK localStorage asa_uid）复用同号（SR-SITE-002 §2.3）。

依赖全部注入：密钥库/会话存储为鸭子协议，demo 内存实现、生产替换 PG/Redis 形态不变。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from typing import Protocol
from dataclasses import dataclass, field
from typing import Any

from services.site.core.errors import SiteServiceError

#: visitor token 有效期（SR-SITE-001 §7 SITE_SESSION_TTL_S 默认值）
SESSION_TTL_S: float = 86400.0
#: data-key 前缀（一行接入 data-key 治理口径）
KEY_PREFIX: str = "pk_site_"
#: 支持语种（SR-SITE-001 §7 SITE_LANG_SUPPORT；检测不中回落 en）
DEFAULT_LANG_SUPPORT: tuple[str, ...] = ("en", "zh", "es", "de", "fr", "ar", "pt", "ru")
#: 语言回落缺省
FALLBACK_LANG: str = "en"


class SessionError(SiteServiceError):
    """会话域业务错误（40001/40101/40301）。"""


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class SdkKey:
    """SDK 接入密钥（site_sdk_key 行的进程内形态；明文 key 不保留）。"""

    key_id: int
    origins: tuple[str, ...]
    widget_config: dict[str, Any] = field(default_factory=dict)
    status: str = "active"  # active | revoked


@dataclass(frozen=True)
class TokenClaims:
    """visitor token 载荷（签发方写入、校验方还原）。"""

    visitor_uid: str
    tenant_id: int
    exp: float


@dataclass
class SiteSessionRecord:
    """访客会话（site_session 行的进程内形态；consent 三态见 FR-SITE-007）。"""

    uid: str
    tenant_id: int
    sdk_key_id: int
    lang: str
    consent: str = "unknown"  # unknown | granted | necessary | denied
    created_at: float = 0.0
    page: str = ""
    referrer: str = ""


@dataclass(frozen=True)
class SessionInit:
    """会话初始化出参（POST /site/session data 段的进程内形态）。"""

    visitor_token: str
    visitor_uid: str
    lang: str
    theme: dict[str, Any]
    faq: list[dict[str, Any]]
    consent_required: bool
    ws_url: str


# --------------------------------------------------------------------------- 存储协议与内存实现


class SdkKeyStore(Protocol):
    """密钥库协议（生产 = PG site_sdk_key，key_hash 查找）。"""

    def find(self, key: str) -> SdkKey | None: ...


class MemorySdkKeyStore:
    """内存密钥库（demo）：SHA-256(key) 哈希登记，明文仅用于注册期换算。"""

    def __init__(self) -> None:
        self._by_hash: dict[str, SdkKey] = {}
        self._next_id = 1

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def register(
        self,
        key: str,
        *,
        origins: tuple[str, ...] = (),
        widget_config: dict[str, Any] | None = None,
        status: str = "active",
    ) -> SdkKey:
        """登记一把密钥（明文不落库，仅存哈希）。"""
        sdk_key = SdkKey(self._next_id, tuple(origins), dict(widget_config or {}), status)
        self._next_id += 1
        self._by_hash[self._hash(key)] = sdk_key
        return sdk_key

    def find(self, key: str) -> SdkKey | None:
        """按明文 key 查找（内部换算哈希）。"""
        return self._by_hash.get(self._hash(key))

    def set_status(self, key: str, status: str) -> None:
        """吊销/恢复（吊销即 40101）。"""
        found = self._by_hash.get(self._hash(key))
        if found is not None:
            self._by_hash[self._hash(key)] = SdkKey(
                found.key_id, found.origins, found.widget_config, status
            )


class SessionStore(Protocol):
    """会话存储协议（生产 = PG site_session + Redis site:ses:{uid} 热缓存）。"""

    def save(self, record: SiteSessionRecord) -> None: ...

    def get(self, uid: str) -> SiteSessionRecord | None: ...

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int: ...


class MemorySessionStore:
    """内存会话存储（demo；DSR 删除按 uid 级联）。"""

    def __init__(self) -> None:
        self._by_uid: dict[str, SiteSessionRecord] = {}

    def save(self, record: SiteSessionRecord) -> None:
        self._by_uid[record.uid] = record

    def get(self, uid: str) -> SiteSessionRecord | None:
        return self._by_uid.get(uid)

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int:
        removed = 0
        for uid in uids:
            if self._by_uid.pop(uid, None) is not None:
                removed += 1
        return removed

    def all(self) -> list[SiteSessionRecord]:
        return list(self._by_uid.values())


# --------------------------------------------------------------------------- visitor token 编解码


class VisitorTokenCodec:
    """visitor token 编解码（HMAC-SHA256 签名，格式 ``vt_<b64url 载荷>.<签名>``）。

    一次性签发、无状态校验（服务端不存 token 明文）；签名防篡改，exp 防重放。
    """

    def __init__(
        self, secret: str, *, ttl_s: float = SESSION_TTL_S, clock: Callable[[], float] = time.time
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl_s = ttl_s
        self._clock = clock

    def _sign(self, payload_b64: str) -> str:
        digest = hmac.new(self._secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:32]

    def issue(self, visitor_uid: str, tenant_id: int) -> str:
        """签发：载荷 = {uid, tenant, exp}。"""
        payload = json.dumps(
            {"uid": visitor_uid, "tenant": tenant_id, "exp": self._clock() + self._ttl_s},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        return f"vt_{payload_b64}.{self._sign(payload_b64)}"

    def verify(self, token: str) -> TokenClaims:
        """校验：格式/签名/有效期三重门，任一失败 → 40101。"""
        if not token.startswith("vt_") or token.count(".") != 1:
            raise SessionError(40101, "visitor token 无效")
        payload_b64, signature = token[len("vt_"):].split(".", 1)
        if not hmac.compare_digest(signature, self._sign(payload_b64)):
            raise SessionError(40101, "visitor token 签名校验失败")
        try:
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SessionError(40101, "visitor token 载荷不可解析") from exc
        if float(payload.get("exp", 0)) <= self._clock():
            raise SessionError(40101, "visitor token 已过期")
        return TokenClaims(str(payload.get("uid", "")), int(payload.get("tenant", 0)), float(payload["exp"]))

    def renew(self, token: str) -> str:
        """续期：校验旧 token 后按同 uid/tenant 重签（exp 顺延）。"""
        claims = self.verify(token)
        return self.issue(claims.visitor_uid, claims.tenant_id)


# --------------------------------------------------------------------------- 语言匹配


def match_lang(bcp47: str, supported: tuple[str, ...] = DEFAULT_LANG_SUPPORT) -> str:
    """BCP47 → 支持语种：主子标签精确匹配，不中回落 en（SR-SITE-001 §6.2②）。"""
    primary = (bcp47 or "").strip().split("-")[0].lower()
    return primary if primary in supported else FALLBACK_LANG


# --------------------------------------------------------------------------- 会话服务


class SessionService:
    """会话服务：data-key/Origin 校验 → visitor token 签发（SR-SITE-001 §6.7）。"""

    def __init__(
        self,
        keys: SdkKeyStore,
        sessions: SessionStore,
        codec: VisitorTokenCodec,
        *,
        lang_support: tuple[str, ...] = DEFAULT_LANG_SUPPORT,
        consent_required: bool = True,
        clock: Callable[[], float] = time.time,
        tenant_id: int = 1,
    ) -> None:
        self._keys = keys
        self._sessions = sessions
        self._codec = codec
        self._langs = lang_support
        self._consent_required = consent_required
        self._clock = clock
        # 租户维度经构造注入（默认 1=甲方本体；P5 多租户由部署配置注入，NFR-SCA-02）
        self._tenant_id = tenant_id

    def create_session(
        self,
        *,
        key: str,
        origin: str,
        page: str = "",
        referrer: str = "",
        lang: str = "",
        existing_uid: str = "",
    ) -> SessionInit:
        """会话初始化：三道门（格式 40001 → 密钥 40101 → 白名单 40301）后签发。

        existing_uid：SDK 同浏览器跨会话复用匿名号（SR-SITE-002 §2.3），格式非法 → 40001。
        """
        if not key or not key.startswith(KEY_PREFIX) or key == KEY_PREFIX:
            raise SessionError(40001, "data-key 缺失或格式错误")
        sdk_key = self._keys.find(key)
        if sdk_key is None or sdk_key.status != "active":
            raise SessionError(40101, "data-key 无效或已吊销")
        if origin not in sdk_key.origins:
            # 不泄露 key 有效性细节（SR-SITE-001 §9：40301 不泄露 key 细节）
            raise SessionError(40301, "Origin 不在接入白名单")
        uid = self._resolve_uid(existing_uid)
        resolved_lang = match_lang(lang, self._langs)
        record = SiteSessionRecord(
            uid=uid,
            tenant_id=self._tenant_id,
            sdk_key_id=sdk_key.key_id,
            lang=resolved_lang,
            consent="unknown",
            created_at=self._clock(),
            page=page,
            referrer=referrer,
        )
        self._sessions.save(record)
        return SessionInit(
            visitor_token=self._codec.issue(uid, record.tenant_id),
            visitor_uid=uid,
            lang=resolved_lang,
            theme=dict(sdk_key.widget_config.get("theme", {})),
            faq=list(sdk_key.widget_config.get("faq", [])),
            consent_required=self._consent_required,
            ws_url="/ws/chat",
        )

    def verify_token(self, token: str) -> TokenClaims:
        """token 校验 + 会话存在性复核（DSR 删除/吊销后旧 token 即失效）。"""
        claims = self._codec.verify(token)
        if self._sessions.get(claims.visitor_uid) is None:
            raise SessionError(40101, "会话不存在或已失效")
        return claims

    def renew_session(self, token: str) -> str:
        """续期（滑动过期：活跃访客免重走 /site/session）。"""
        return self._codec.renew(token)

    def _resolve_uid(self, existing_uid: str) -> str:
        """解析访客号：无入参新签 UUID；入参须为合法 UUID（防伪造脏号）。"""
        if not existing_uid:
            return str(uuid.uuid4())
        try:
            return str(uuid.UUID(existing_uid))
        except ValueError as exc:
            raise SessionError(40001, "existing_uid 格式非法") from exc


__all__ = [
    "DEFAULT_LANG_SUPPORT",
    "FALLBACK_LANG",
    "KEY_PREFIX",
    "SESSION_TTL_S",
    "MemorySdkKeyStore",
    "MemorySessionStore",
    "SdkKey",
    "SdkKeyStore",
    "SessionError",
    "SessionInit",
    "SessionService",
    "SessionStore",
    "SiteSessionRecord",
    "TokenClaims",
    "VisitorTokenCodec",
    "match_lang",
]
