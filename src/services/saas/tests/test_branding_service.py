# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""品牌定制（白标）服务单元测试（FR-SAAS-006，SR-SAAS-006 §4/§6.1/§6.2/§6.5/§9；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：合法 brand_config 全要素写入、version 单调递增、公开视图白名单、ETag 口径、
  域名「注册→TXT→CNAME→签发→active」全链 + 验证幂等重放 + Host 路由解析；
- 边界：产品名长度 1/30、色值恰 6 位 HEX、字体恰白名单键、域名规范化后冲突、
  TXT 不匹配可重试、CNAME 缺失停留 verified、证书失败可重试；
- 异常：非法色值（rgb()/red/url() 注入）、五键缺一多一、素材外链/他租户前缀/非 HTTPS、
  平台域名与非法域名拒绝；
- 并发：10 线程并发 PUT version 严格 +10 无回退；
- 降级：未配置租户回落内置默认品牌（version 0）；审核开关 pending_review 预留。

验收口径映射：SRS §16.6「白标配置生效」；UC-14。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-006-品牌定制白标.md §6.1、§6.2、§6.5、§9
"""

from __future__ import annotations

import threading

import pytest

from services.saas.core.branding.service import (
    BrandingError,
    BrandingService,
    brand_etag,
    validate_brand_patch,
)

VALID_COLORS = {"primary": "#1A73E8", "secondary": "#00A884", "accent": "#FF7043",
                "bg_page": "#F7F8FA", "text_main": "#1F2329"}


def valid_patch() -> dict:
    """合法品牌补丁（三要素齐备：产品名/配色/素材 URL）。"""
    return {
        "product_name": "Acme Sales AI",
        "colors": dict(VALID_COLORS),
        "font_family": "system_cn",
        "logo_url": "https://oss.example.com/tenants/7/brand/logo.png",
        "favicon_url": "https://oss.example.com/tenants/7/brand/favicon.png",
        "login_copy": {"title": "欢迎使用 Acme 销售 AI", "footer": "© 2026 Acme"},
        "sdk_widget": {"position": "right", "greeting": "Hi, may I help you?"},
    }


class FakeDns:
    """假 DNS：TXT/CNAME 可编程翻转（demo 口径，生产为递归解析实查）。"""

    def __init__(self, txt_ok: bool = True, cname_ok: bool = True) -> None:
        self.txt_ok, self.cname_ok = txt_ok, cname_ok

    def txt_matches(self, name: str, token: str) -> bool:
        return self.txt_ok

    def cname_points_to(self, domain: str, target: str) -> bool:
        return self.cname_ok


class FakeCert:
    """假证书签发器（demo：K8s cert-manager / Compose acme.sh 的鸭子契约）。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.issued: list[str] = []

    def issue(self, domain: str) -> bool:
        if self.ok:
            self.issued.append(domain)
        return self.ok


def make_service(dns: FakeDns | None = None, cert: FakeCert | None = None) -> BrandingService:
    return BrandingService(dns=dns or FakeDns(), cert=cert or FakeCert())


# ───────────────── 等价类：合法写入 / 版本递增 / 白名单 / 域名全链 ─────────────────


class TestEquivalence:
    def test_validate_and_put_full_brand_with_etag(self) -> None:
        """合法全要素补丁：version 1、state approved、ETag 口径 "t7-brand-v1"。"""
        svc = make_service()
        assert svc.put_config(7, valid_patch()) == {"version": 1, "state": "approved"}
        assert brand_etag(7, 1) == '"t7-brand-v1"'

    def test_version_increments_monotonically_with_merge(self) -> None:
        """连续 PUT：version 严格递增，未触及键保留（合并语义）。"""
        svc = make_service()
        svc.put_config(7, {"product_name": "V1 品牌"})
        svc.put_config(7, {"colors": dict(VALID_COLORS)})
        cfg = svc.get_config(7)
        assert cfg["version"] == 2 and cfg["product_name"] == "V1 品牌"

    def test_public_view_whitelist_fields(self) -> None:
        """公开读取仅白名单字段：state/domains 服务端管理字段不出（防泄露）。"""
        svc = make_service()
        svc.put_config(7, valid_patch())
        view = svc.public_view(7)
        assert "state" not in view and "domains" not in view
        assert set(view) >= {"product_name", "logo_url", "colors", "font_family",
                             "login_copy", "sdk_widget", "version"}

    def test_domain_full_chain_to_active_route_and_idempotent(self) -> None:
        """域名全链：注册→DNS 指引→验证→签发→active→路由解析；重复 verify 幂等不重签。"""
        cert = FakeCert(ok=True)
        svc = make_service(dns=FakeDns(True, True), cert=cert)
        reg = svc.register_domain(7, "App.Acme-Example.com", actor="tenant-admin")
        assert reg["dns_instructions"]["txt_name"] == "_asaverify.app.acme-example.com"
        assert svc.resolve_host("app.acme-example.com") is None  # 未验证不进路由表
        first = svc.verify_domain(reg["id"])
        assert first["state"] == "active" and first["cert_state"] == "issued"
        again = svc.verify_domain(reg["id"])
        assert again["state"] == "active" and len(cert.issued) == 1  # 幂等不重签
        assert svc.resolve_host("app.acme-example.com") == 7  # 网关 Host→tenant
        assert "app.acme-example.com" in svc.get_config(7)["domains"]  # 回写权威源


# ───────────────── 边界：长度 / 恰 6 位 HEX / 规范化冲突 ─────────────────


class TestBoundary:
    def test_product_name_length_edges(self) -> None:
        """产品名长度边界：1 与 30 合法，0 与 31 拒绝。"""
        for name, ok in (("A", True), ("企" * 30, True), ("", False), ("企" * 31, False)):
            if ok:
                assert validate_brand_patch({"product_name": name}, 7)["product_name"] == name
            else:
                with pytest.raises(BrandingError):
                    validate_brand_patch({"product_name": name}, 7)

    def test_hex_color_and_font_whitelist_exact(self) -> None:
        """色值恰 6 位 HEX；字体恰白名单键——"#12345"与自由字符串均拒绝。"""
        assert validate_brand_patch({"colors": dict(VALID_COLORS)}, 7)["colors"]
        assert validate_brand_patch({"font_family": "system_en"}, 7)
        with pytest.raises(BrandingError):
            validate_brand_patch({"colors": dict(VALID_COLORS, primary="#12345")}, 7)
        with pytest.raises(BrandingError):
            validate_brand_patch({"font_family": "Comic Sans, monospace"}, 7)

    def test_domain_normalized_conflict(self) -> None:
        """大小写/端口/路径规范化后同一域名仍判冲突（UNIQUE(domain)）。"""
        svc = make_service()
        first = svc.register_domain(7, "app.acme-example.com")
        with pytest.raises(BrandingError) as ei:
            svc.register_domain(8, "HTTPS://App.Acme-Example.com:443/")
        assert ei.value.code == 40901
        assert first["id"] > 0


# ───────────────── 异常：色值注入 / 外链 / 平台域名 / TXT 失败 / 证书失败 ─────────────────


class TestExceptions:
    @pytest.mark.parametrize("bad", ["rgb(1,2,3)", "red", "url(#x)"])
    def test_illegal_color_values_rejected(self, bad: str) -> None:
        """非法色值（rgb()/关键字/url()）一律 40001 防 CSS 注入。"""
        with pytest.raises(BrandingError) as ei:
            validate_brand_patch({"colors": dict(VALID_COLORS, primary=bad)}, 7)
        assert ei.value.code == 40001

    def test_colors_must_exactly_five_keys(self) -> None:
        """配色必须且仅含 5 键：缺一/多一均拒绝。"""
        with pytest.raises(BrandingError):
            validate_brand_patch({"colors": {k: v for k, v in VALID_COLORS.items()
                                             if k != "accent"}}, 7)
        with pytest.raises(BrandingError):
            validate_brand_patch({"colors": dict(VALID_COLORS, extra="#111111")}, 7)

    def test_asset_external_url_rejected(self) -> None:
        """素材外链/他租户前缀/非 HTTPS 一律拒绝（防外链失效与换图攻击）。"""
        with pytest.raises(BrandingError):
            validate_brand_patch({"logo_url": "https://evil.example.com/logo.png"}, 7)
        with pytest.raises(BrandingError):
            validate_brand_patch(
                {"logo_url": "https://oss.example.com/tenants/8/brand/logo.png"}, 7)
        with pytest.raises(BrandingError):
            validate_brand_patch(
                {"favicon_url": "http://oss.example.com/tenants/7/brand/f.png"}, 7)

    def test_platform_and_invalid_domain_rejected(self) -> None:
        """平台域名与非法域名注册 → 40001（防自绑入口）。"""
        svc = make_service()
        for bad in ("sale-platform.example.com", "www.sale-platform.example.com",
                    "not a domain", "-bad-.com", "shop"):
            with pytest.raises(BrandingError):
                svc.register_domain(7, bad)

    def test_txt_mismatch_and_cname_missing_hold_retryable(self) -> None:
        """TXT 不匹配→pending_verification 可重试；TXT 过但 CNAME 缺→停留 verified 不签发。"""
        svc = make_service(dns=FakeDns(txt_ok=False), cert=FakeCert(ok=True))
        reg = svc.register_domain(7, "retry.acme-example.com")
        result = svc.verify_domain(reg["id"])
        assert result["state"] == "pending_verification" and result["cert_state"] == "pending"
        assert "TXT" in result["fail_reason"]
        # TXT 传播生效但 CNAME 未指向平台：停留 verified，不进路由表
        svc.dns.txt_ok = True
        svc.dns.cname_ok = False
        held = svc.verify_domain(reg["id"])
        assert held["state"] == "verified" and held["cert_state"] == "pending"
        assert svc.resolve_host("retry.acme-example.com") is None
        # CNAME 就位后重试通过：active + 证书 issued + 路由生效
        svc.dns.cname_ok = True
        final = svc.verify_domain(reg["id"])
        assert final["state"] == "active" and final["cert_state"] == "issued"
        assert svc.resolve_host("retry.acme-example.com") == 7

    def test_cert_issue_failure_lands_failed_retryable(self) -> None:
        """证书签发失败 → failed 可重试；修复后重新 verify 签发成功（pending→issued）。"""
        svc = make_service(cert=FakeCert(ok=False))
        reg = svc.register_domain(7, "certfail.acme-example.com")
        result = svc.verify_domain(reg["id"])
        assert result["state"] == "failed" and result["cert_state"] == "failed"
        svc.cert.ok = True
        result2 = svc.verify_domain(reg["id"])
        assert result2["state"] == "active" and result2["cert_state"] == "issued"


# ───────────────── 并发（version 无回退）/ 降级（默认品牌 / 审核预留） ─────────────────


class TestConcurrencyAndDegrade:
    def test_concurrent_puts_version_strict_plus_n(self) -> None:
        """10 线程并发 PUT：version 严格 +10 无回退/重复（FOR UPDATE 语义，锁内递增）。"""
        svc = make_service()
        barrier = threading.Barrier(10)

        def put(i: int) -> None:
            barrier.wait()
            svc.put_config(7, {"colors": dict(VALID_COLORS, primary=f"#{i:06X}")})

        threads = [threading.Thread(target=put, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert svc.get_config(7)["version"] == 10

    def test_unconfigured_tenant_falls_back_to_default_brand(self) -> None:
        """未配置租户：回落内置默认品牌（version 0，三端不白屏）。"""
        view = make_service().public_view(42)
        assert view["version"] == 0 and view["product_name"]
        assert set(view["colors"]) == set(VALID_COLORS)

    def test_audit_switch_holds_pending_review(self) -> None:
        """审核开关（防滥用预留）：开启后新版本 pending_review 不可见，approve 后生效。"""
        svc = make_service()
        svc.audit_required = True
        svc.put_config(7, {"product_name": "待审品牌"})
        assert svc.get_config(7)["state"] == "pending_review"
        assert svc.public_view(7)["product_name"] != "待审品牌"  # 公开侧仍上一 approved 版
        svc.approve(7)
        assert svc.public_view(7)["product_name"] == "待审品牌"
