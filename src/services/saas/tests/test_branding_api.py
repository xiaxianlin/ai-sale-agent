# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""品牌定制（白标）API 测试（FR-SAAS-006，SR-SAAS-006 §5/§6.3；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：PUT 后 GET 全量一致、公开读取带 ETag 与 no-cache、If-None-Match 未变 304
  零载荷、域名注册/验证 API 全链；
- 边界：304 响应体为空、ETag 引号口径、公开视图白名单；
- 异常：非法色值 400 信封 40001、跨租户域名抢占 409 信封 40901；
- 并发：并发 PUT 后 ETag 版本与最终 version 一致；
- 降级：未配置租户公开读取返回默认品牌（version 0，可用性优先）。

验收口径映射：SRS §16.6「白标配置生效（免发版）」；04-接口设计 §1.3/§3.8。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-006-品牌定制白标.md §5、§6.3
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.saas.api.branding_api import create_branding_app
from services.saas.core.branding.service import BrandingService, brand_etag


class AlwaysOkDns:
    """假 DNS（demo）：TXT/CNAME 恒通过。"""

    def txt_matches(self, name: str, token: str) -> bool:
        return True

    def cname_points_to(self, domain: str, target: str) -> bool:
        return True


class AlwaysOkCert:
    """假证书签发器（demo）：恒成功。"""

    def issue(self, domain: str) -> bool:
        return True


def make_client() -> tuple[TestClient, BrandingService]:
    svc = BrandingService(dns=AlwaysOkDns(), cert=AlwaysOkCert())
    return TestClient(create_branding_app(svc)), svc


VALID = {
    "product_name": "Acme Sales AI",
    "colors": {"primary": "#1A73E8", "secondary": "#00A884", "accent": "#FF7043",
               "bg_page": "#F7F8FA", "text_main": "#1F2329"},
    "font_family": "system_cn",
    "logo_url": "https://oss.example.com/tenants/7/brand/logo.png",
}


# ───────────────────────── 等价类：PUT/GET 一致 / ETag / 域名 API 全链 ─────────────────────────


class TestEquivalence:
    def test_put_then_get_roundtrip(self) -> None:
        """PUT 品牌配置 → {version,state}；GET 全量一致（免发版：无构建动作）。"""
        client, _ = make_client()
        put = client.put("/api/v1/saas/tenants/7/brand-config", json=VALID)
        assert put.status_code == 200
        assert put.json()["data"] == {"version": 1, "state": "approved"}
        cfg = client.get("/api/v1/saas/tenants/7/brand-config").json()["data"]
        assert cfg["product_name"] == "Acme Sales AI" and cfg["version"] == 1

    def test_public_read_carries_etag_and_no_cache(self) -> None:
        """公开读取：ETag 头 + Cache-Control: no-cache（禁中间层长缓存）。"""
        client, _ = make_client()
        client.put("/api/v1/saas/tenants/7/brand-config", json=VALID)
        resp = client.get("/api/v1/public/brand-config", params={"tenant_id": 7})
        assert resp.status_code == 200
        assert resp.headers["etag"] == brand_etag(7, 1)
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.json()["data"]["product_name"] == "Acme Sales AI"

    def test_if_none_match_returns_304_empty(self) -> None:
        """If-None-Match 命中当前 ETag → 304 零载荷（未变更免传输）。"""
        client, _ = make_client()
        client.put("/api/v1/saas/tenants/7/brand-config", json=VALID)
        etag = brand_etag(7, 1)
        resp = client.get("/api/v1/public/brand-config", params={"tenant_id": 7},
                          headers={"If-None-Match": etag})
        assert resp.status_code == 304
        assert resp.headers["etag"] == etag
        assert resp.content == b""

    def test_domain_register_and_verify_via_api(self) -> None:
        """域名 API 全链：注册下发 DNS 指引 → 验证 active → 公开路由生效。"""
        client, svc = make_client()
        reg = client.post("/api/v1/saas/domains",
                          json={"tenant_id": 7, "domain": "api.acme-example.com"})
        assert reg.status_code == 200
        data = reg.json()["data"]
        assert data["dns_instructions"]["txt_value"]
        verify = client.post(f"/api/v1/saas/domains/{data['id']}/verify")
        assert verify.json()["data"]["state"] == "active"
        listing = client.get("/api/v1/saas/domains", params={"tenant_id": 7}).json()["data"]
        assert listing["domains"][0]["state"] == "active"
        assert svc.resolve_host("api.acme-example.com") == 7


# ───────────────────────── 异常：校验 40001 / 抢占 40901 ─────────────────────────


class TestExceptions:
    def test_illegal_color_maps_400_envelope(self) -> None:
        """非法色值 → HTTP 400 + code 40001 信封（指明字段与格式）。"""
        client, _ = make_client()
        bad = dict(VALID, colors=dict(VALID["colors"], primary="rgb(1,2,3)"))
        resp = client.put("/api/v1/saas/tenants/7/brand-config", json=bad)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40001 and "primary" in body["message"]

    def test_domain_conflict_maps_409_envelope(self) -> None:
        """跨租户域名抢占 → HTTP 409 + code 40901（UNIQUE(domain)）。"""
        client, _ = make_client()
        assert client.post("/api/v1/saas/domains",
                           json={"tenant_id": 7, "domain": "dupe.acme-example.com"}
                           ).status_code == 200
        resp = client.post("/api/v1/saas/domains",
                           json={"tenant_id": 8, "domain": "dupe.acme-example.com"})
        assert resp.status_code == 409
        assert resp.json()["code"] == 40901

# ───────────────────────── 并发 / 降级 ─────────────────────────


class TestConcurrencyAndDegrade:
    def test_etag_tracks_final_version_after_concurrent_puts(self) -> None:
        """并发 PUT 后：公开读取 ETag 与最终 version 一致（版本号无回退）。"""
        from concurrent.futures import ThreadPoolExecutor

        client, svc = make_client()

        def put(i: int) -> None:
            client.put("/api/v1/saas/tenants/7/brand-config",
                       json={"product_name": f"品牌 V{i}"})

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(put, range(5)))
        final_version = svc.get_config(7)["version"]
        resp = client.get("/api/v1/public/brand-config", params={"tenant_id": 7})
        assert resp.headers["etag"] == brand_etag(7, final_version)

    def test_unconfigured_tenant_public_read_default_brand(self) -> None:
        """未配置租户公开读取：默认品牌 version 0（降级不白屏，可用性优先）。"""
        client, _ = make_client()
        resp = client.get("/api/v1/public/brand-config", params={"tenant_id": 99})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["version"] == 0 and data["product_name"]
