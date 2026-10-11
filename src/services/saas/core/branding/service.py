# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""品牌定制（白标）服务（FR-SAAS-006，SR-SAAS-006 §4/§6.1/§6.2/§6.3/§6.5）。

关键决策落地（SR §2）：
1. 运行时读取而非编译期主题：品牌全要素存 brand_config（进程内快照），
   写入即 version+1 并失效缓存（demo 直读，无任何发版动作）；
2. 版本号 + ETag 传播：version 单调递增，ETag = "t{tid}-brand-v{version}"；
3. 自定义域名走「注册-验证-证书-路由」状态机：TXT 归属验证（防恶意绑定）→
   CNAME 接入 → 证书签发（pending→issued）→ active 进路由表并回写
   brand_config.domains（路由权威源）；
4. 防滥用审核开关预留：audit_required 开启后新版本 pending_review，
   approve 后才对外可见。

demo 口径：DNS/Certificate 为鸭子协议（生产接递归解析实查与 cert-manager/acme.sh）；
存储为进程内字典（生产 = tenant.brand_config JSONB + tenant_domain 台账 + Redis 键）。
"""
from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

# --------------------------------------------------------------------------- 常量与口径

CODE_PARAM: int = 40001        # 校验/状态违规（SR-SAAS-006 §5）
CODE_CONFLICT: int = 40901     # 域名抢占冲突（UNIQUE(domain)）
CODE_NOT_FOUND: int = 40401    # 域名不存在

#: 色值键（必须且仅含 5 键，HEX 校验防 CSS 注入，SR §6.1）
COLOR_KEYS: tuple[str, ...] = ("primary", "secondary", "accent", "bg_page", "text_main")
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 字体白名单键（非自由字符串，SR §6.1 ALLOWED_FONTS）
ALLOWED_FONTS: tuple[str, ...] = ("system_cn", "system_en")

#: 素材 URL 仅允许本租户 OSS 前缀（杜绝外链：防失效/像素追踪/换图攻击）
OSS_BRAND_PREFIX: str = "https://oss.example.com/tenants/{tid}/brand/"

#: 平台保留域名（不允许租户自绑平台入口）
PLATFORM_DOMAIN: str = "sale-platform.example.com"
FORBIDDEN_DOMAINS: frozenset[str] = frozenset({PLATFORM_DOMAIN, f"www.{PLATFORM_DOMAIN}"})

#: DNS 指引口径（SR §6.5：_asaverify TXT 归属验证 + CNAME 流量接入）
TXT_PREFIX: str = "_asaverify"
CNAME_TARGET: str = f"cname.{PLATFORM_DOMAIN}"

#: 公开读取白名单字段（state/domains 服务端管理，不出）
PUBLIC_FIELDS: tuple[str, ...] = ("product_name", "logo_url", "favicon_url", "colors",
                                  "font_family", "login_copy", "sdk_widget", "version")

#: 域名格式（注册域级：至少两级；标签不以 - 开头结尾）
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

#: 域名验证状态机（SR §4 tenant_domain.state）
DOMAIN_STATES: tuple[str, ...] = ("pending_verification", "verified", "cert_issuing",
                                  "active", "failed")
#: 证书状态机（pending→issued | failed）
CERT_STATES: tuple[str, ...] = ("pending", "issued", "failed")


def _default_brand() -> dict[str, Any]:
    """内置默认品牌（brand_config={} 时三端回落，降级不白屏）。"""
    return {
        "product_name": "AI 外贸销售智能体",
        "logo_url": None,
        "favicon_url": None,
        "colors": {"primary": "#1E63E9", "secondary": "#00A884", "accent": "#FF7043",
                   "bg_page": "#F7F8FA", "text_main": "#1F2329"},
        "font_family": "system_cn",
        "login_copy": {"title": "AI 外贸销售智能体", "subtitle": "配置化接入 · 白标生效",
                       "footer": "© 2026 AI Sale Agent"},
        "sdk_widget": {"position": "right", "greeting": "Hi, may I help you?"},
        "domains": [],
        "state": "approved",
        "version": 0,
    }


# --------------------------------------------------------------------------- 错误


class BrandingError(Exception):
    """白标域业务错误（40001/40901/40401）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- 校验（SR §6.1）


def validate_brand_patch(patch: dict, tenant_id: int) -> dict:
    """校验租户可写字段并规范化（version/state/domains 服务端管理，入参即越权）。

    允许键：product_name / colors / font_family / logo_url / favicon_url /
    login_copy / sdk_widget；至少一项且全部合法才返回规范化补丁。
    """
    allowed = {"product_name", "colors", "font_family", "logo_url", "favicon_url",
               "login_copy", "sdk_widget"}
    unknown = set(patch) - allowed
    if unknown:
        raise BrandingError(CODE_PARAM, f"含不允许租户写入的字段：{sorted(unknown)}")
    if not patch:
        raise BrandingError(CODE_PARAM, "品牌配置补丁为空")

    normalized: dict[str, Any] = {}
    if "product_name" in patch:
        name = patch["product_name"]
        if not isinstance(name, str) or not (1 <= len(name) <= 30):
            raise BrandingError(CODE_PARAM, "产品名须为 1-30 字符")
        normalized["product_name"] = name
    if "colors" in patch:
        colors = patch["colors"]
        if not isinstance(colors, dict) or set(colors) != set(COLOR_KEYS):
            raise BrandingError(
                CODE_PARAM, f"配色必须且仅含 {sorted(COLOR_KEYS)}")
        for key, color in colors.items():
            if not isinstance(color, str) or not HEX_COLOR.match(color):
                raise BrandingError(
                    CODE_PARAM, f"{key} 非法色值（须 #RRGGBB）：{color}")
        normalized["colors"] = dict(colors)
    if "font_family" in patch:
        if patch["font_family"] not in ALLOWED_FONTS:
            raise BrandingError(CODE_PARAM,
                                f"字体须为白名单键 {ALLOWED_FONTS}：{patch['font_family']}")
        normalized["font_family"] = patch["font_family"]
    for key in ("logo_url", "favicon_url"):
        if key in patch:
            check_asset_url(tenant_id, patch[key], key)
            normalized[key] = patch[key]
    if "login_copy" in patch:
        copy_ = patch["login_copy"]
        if not isinstance(copy_, dict) or not set(copy_) <= {"title", "subtitle", "footer"}:
            raise BrandingError(CODE_PARAM, "文案键仅允许 title/subtitle/footer")
        for k, v in copy_.items():
            if not isinstance(v, str) or not (1 <= len(v) <= 100):
                raise BrandingError(CODE_PARAM, f"文案 {k} 须为 1-100 字符")
        normalized["login_copy"] = dict(copy_)
    if "sdk_widget" in patch:
        widget = patch["sdk_widget"]
        if not isinstance(widget, dict):
            raise BrandingError(CODE_PARAM, "sdk_widget 须为对象")
        if "position" in widget and widget["position"] not in ("left", "right"):
            raise BrandingError(CODE_PARAM, "sdk_widget.position 仅允许 left/right")
        normalized["sdk_widget"] = dict(widget)
    return normalized


def check_asset_url(tenant_id: int, url: str | None, kind: str) -> None:
    """素材 URL 只允许本租户 OSS 前缀（且必须 HTTPS，NFR-SEC-05/07）。"""
    if url is None:
        return
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BrandingError(CODE_PARAM, f"{kind} 必须为 HTTPS URL")
    if not url.startswith(OSS_BRAND_PREFIX.format(tid=tenant_id)):
        raise BrandingError(CODE_PARAM,
                            f"{kind} 必须上传至本租户品牌目录"
                            f"（{OSS_BRAND_PREFIX.format(tid=tenant_id)}）")


def brand_etag(tenant_id: int, version: int) -> str:
    """公开接口 ETag（SR §6.3：'"t{tid}-brand-v{version}"'，带引号口径）。"""
    return f'"t{tenant_id}-brand-v{version}"'


# --------------------------------------------------------------------------- 外部依赖协议


class DnsChecker(Protocol):
    """DNS 实查协议（生产 = 递归解析；demo 可编程 Fake）。"""

    def txt_matches(self, name: str, token: str) -> bool: ...

    def cname_points_to(self, domain: str, target: str) -> bool: ...


class CertIssuer(Protocol):
    """证书签发协议（K8s=cert-manager/ACME；Compose=acme.sh）。"""

    def issue(self, domain: str) -> bool: ...


@dataclass
class TenantDomain:
    """自定义域名台账（tenant_domain 行的进程内形态）。"""

    id: int
    tenant_id: int
    domain: str                      # 小写 punycode，全局唯一
    state: str = "pending_verification"
    txt_token: str = ""
    txt_verified_at: str | None = None
    cname_ok: bool = False
    cert_state: str = "pending"      # 证书状态机 pending→issued|failed
    cert_expire_at: str | None = None
    fail_reason: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "tenant_id": self.tenant_id, "domain": self.domain,
                "state": self.state, "cert_state": self.cert_state,
                "cname_ok": self.cname_ok, "fail_reason": self.fail_reason}


# --------------------------------------------------------------------------- 服务本体


class BrandingService:
    """品牌配置读写 + 生效引擎 + 域名状态机（SR §6.2/§6.3/§6.5）。

    并发口径：写路径持全局写锁（生产 SELECT…FOR UPDATE），version 严格递增无回退。
    """

    def __init__(self, *, dns: DnsChecker | None = None, cert: CertIssuer | None = None,
                 token_factory: Any = None) -> None:
        self.dns = dns
        self.cert = cert
        self._token_factory = token_factory or (lambda: secrets.token_hex(32))
        self._lock = threading.RLock()
        self._configs: dict[int, dict[str, Any]] = {}      # tid → 最新 brand_config
        self._approved: dict[int, dict[str, Any]] = {}     # tid → 最近 approved 版本
        self._domains: dict[int, TenantDomain] = {}        # domain_id → 台账
        self._routes: dict[str, int] = {}                  # active host → tid（路由表）
        self._next_domain_id = 1
        self.audit_required = False                        # brand.audit_required（默认关）

    # -- 品牌配置（SR §6.2）-------------------------------------------------

    def put_config(self, tenant_id: int, patch: dict,
                   actor: str = "tenant-admin") -> dict:
        """PUT 品牌配置：校验 → 合并 → version+1 →（approved 即时生效）。"""
        normalized = validate_brand_patch(patch, tenant_id)
        with self._lock:
            cur = self._configs.get(tenant_id, _default_brand())
            merged = {**cur, **normalized}
            merged["version"] = int(cur.get("version", 0)) + 1  # 单调递增（ETag 来源）
            merged["state"] = "pending_review" if self.audit_required else "approved"
            merged["domains"] = list(cur.get("domains", []))    # 服务端管理，不改
            self._configs[tenant_id] = merged
            if merged["state"] == "approved":
                self._approved[tenant_id] = merged  # 即时生效（写入即发布，无发版动作）
        return {"version": merged["version"], "state": merged["state"]}

    def approve(self, tenant_id: int, actor: str = "platform-admin") -> dict:
        """平台运营审核通过（审核开关开启时使用）：pending_review → 对外生效。"""
        with self._lock:
            cur = self._configs.get(tenant_id)
            if cur is None:
                raise BrandingError(CODE_NOT_FOUND, f"租户无品牌配置：{tenant_id}")
            cur["state"] = "approved"
            self._approved[tenant_id] = cur
            return {"version": cur["version"], "state": cur["state"]}

    def get_config(self, tenant_id: int) -> dict:
        """全量 brand_config（管理面读取；未配置回落默认品牌副本）。"""
        with self._lock:
            cfg = self._configs.get(tenant_id)
            return _deep_copy(cfg) if cfg is not None else _deep_copy(_default_brand())

    def public_view(self, tenant_id: int) -> dict:
        """公开读取（SR §6.3）：仅 approved 最新版 + 白名单字段（匿名三端拉取）。"""
        with self._lock:
            cfg = self._approved.get(tenant_id)
            if cfg is None:  # 未配置/待审：回落内置默认（可用性优先，仅改外观不损功能）
                cfg = _default_brand()
            return {k: _deep_copy(cfg.get(k)) for k in PUBLIC_FIELDS}

    # -- 域名状态机（SR §6.5）-----------------------------------------------

    def register_domain(self, tenant_id: int, domain: str,
                        actor: str = "tenant-admin") -> dict:
        """注册自定义域名：规范化 → 保留域/格式校验 → 全局唯一 → 下发 DNS 指引。"""
        d = _normalize_domain(domain)
        if d in FORBIDDEN_DOMAINS:
            raise BrandingError(CODE_PARAM, f"该域名不允许注册：{d}")
        if not _DOMAIN_RE.match(d):
            raise BrandingError(CODE_PARAM, f"域名格式非法：{d}")
        token = self._token_factory()
        with self._lock:
            if any(row.domain == d for row in self._domains.values()):
                raise BrandingError(CODE_CONFLICT, f"域名已被绑定：{d}")
            row = TenantDomain(id=self._next_domain_id, tenant_id=tenant_id, domain=d,
                               txt_token=token)
            self._domains[row.id] = row
            self._next_domain_id += 1
        return {"id": row.id, "domain": d,
                "dns_instructions": {"txt_name": f"{TXT_PREFIX}.{d}",
                                     "txt_value": token, "cname_target": CNAME_TARGET}}

    def verify_domain(self, domain_id: int) -> dict:
        """验证域名归属并签发证书：TXT→CNAME→cert_issuing→active（回写路由表）。

        幂等：已 active 重放返回当前态不重签；TXT 不匹配停留 pending_verification
        可重试；CNAME 缺失停留 verified；签发失败落 failed 可重试。
        """
        if self.dns is None or self.cert is None:
            raise BrandingError(CODE_PARAM, "DNS/证书依赖未装配")
        row = self._domains.get(domain_id)
        if row is None:
            raise BrandingError(CODE_NOT_FOUND, f"域名不存在：{domain_id}")
        with self._lock:
            if row.state == "active":  # 幂等重放
                return row.as_dict()
        # ① TXT 归属验证（DNS 传播延迟可重试）
        if not self.dns.txt_matches(f"{TXT_PREFIX}.{row.domain}", row.txt_token):
            row.state = "pending_verification"
            row.fail_reason = "TXT 记录未生效或不匹配"
            return row.as_dict()
        row.txt_verified_at = _now_iso()
        # ② CNAME 流量接入校验（未指向平台：停留 verified，暂不签发证书）
        if not self.dns.cname_points_to(row.domain, CNAME_TARGET):
            row.state = "verified"
            row.cname_ok = False
            row.fail_reason = "CNAME 未指向平台，暂不签发证书"
            return row.as_dict()
        row.cname_ok = True
        # ③ 证书签发（cert-manager/acme.sh；失败告警可重试）
        row.state = "cert_issuing"
        if not self.cert.issue(row.domain):
            row.state = "failed"
            row.cert_state = "failed"
            row.fail_reason = "证书签发失败，可重试"
            return row.as_dict()
        # ④ active：进路由表 + 回写 brand_config.domains（路由权威源）
        row.state = "active"
        row.cert_state = "issued"
        row.cert_expire_at = (datetime.now(timezone.utc) + timedelta(days=90)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        row.fail_reason = ""
        with self._lock:
            self._routes[row.domain] = row.tenant_id
            cfg = self._configs.get(row.tenant_id)
            if cfg is None:
                cfg = _default_brand()
                self._configs[row.tenant_id] = cfg
            if row.domain not in cfg["domains"]:
                cfg["domains"] = sorted([*cfg["domains"], row.domain])
            if cfg.get("state") == "approved":
                self._approved[row.tenant_id] = cfg
        return row.as_dict()

    def list_domains(self, tenant_id: int) -> list[TenantDomain]:
        """租户域名台账（运营后台展示）。"""
        with self._lock:
            return [row for row in self._domains.values()
                    if row.tenant_id == tenant_id]

    def resolve_host(self, host: str) -> int | None:
        """网关 Host→tenant 解析（仅 active 域名；未绑定拒绝 404，不透应用层）。"""
        with self._lock:
            return self._routes.get(host.lower().strip("."))


# --------------------------------------------------------------------------- 内部工具


def _normalize_domain(domain: str) -> str:
    """域名规范化：去 scheme/端口/路径 → 小写 → IDN punycode（SR §6.5 normalize）。"""
    d = domain.strip().lower()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0].split(":", 1)[0].strip(".")
    try:
        d = d.encode("idna").decode("ascii")  # IDN → punycode
    except UnicodeError as exc:
        raise BrandingError(CODE_PARAM, f"域名含非法字符：{domain}") from exc
    return d


def _deep_copy(value: Any) -> Any:
    """防御性拷贝（dict/list 容器；标量原样）——快照不外泄内部引用。"""
    if isinstance(value, dict):
        return {k: _deep_copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy(v) for v in value]
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
