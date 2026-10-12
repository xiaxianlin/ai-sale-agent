# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-006 管理后台 V2 API 通道用例（SR-SITE-006 §5/§6.3；TDD 阶段①②，TestClient）。

端点：GET /api/v1/admin/site/overview | funnel | mail-effect | drill。
RBAC：site:dashboard:read（supervisor/admin），销售默认无 → 40301；
未注册下钻指标 → 40001；统一信封四键。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from services.site.api.admin_v2_api import (
    ADMIN_DRILL_PATH,
    ADMIN_FUNNEL_PATH,
    ADMIN_MAIL_EFFECT_PATH,
    ADMIN_OVERVIEW_PATH,
    create_admin_app,
)
from services.site.core.admin_v2.metrics import (
    DealFact,
    MailFact,
    SessionFact,
    SiteMetrics,
    VisitorFact,
)

from .fakes import MutableClock
from .test_admin_v2_metrics import FakeSiteSource

D1 = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
SUPERVISOR = {"X-User-Id": "sv-1", "X-User-Roles": "supervisor"}
SALES = {"X-User-Id": "s-1", "X-User-Roles": "sales"}


def make_client():
    """装配 admin API：预置一天数据（5 访客/2 会话/1 留资/1 邮件送达）。"""
    src = FakeSiteSource()
    for i in range(5):
        src.visitor_rows.append(VisitorFact(f"v{i}", "page_view", "direct", True, D1 + timedelta(minutes=i)))
    src.visitor_rows.append(VisitorFact("v0", "lead", "direct", True, D1 + timedelta(minutes=30)))
    for i in range(2):
        src.session_facts.append(SessionFact(i + 1, D1 + timedelta(minutes=i), customer_id=10 + i))
    src.mail_rows.append(MailFact("cart_recover", "delivered", "a@x.com", D1))
    src.mail_rows.append(MailFact("cart_recover", "failed", "b@x.com", D1))
    metrics = SiteMetrics(src, clock=MutableClock(D1).now)
    # allow_header_claims=True：测试显式声明 demo 形态（CR P0-3 修复后缺省关门）
    app = create_admin_app(metrics=metrics, allow_header_claims=True)
    return TestClient(app), metrics


def params(**kw) -> dict:
    """默认查询区间（D1 当日）。"""
    base = {"date_from": "2026-10-11", "date_to": "2026-10-11"}
    base.update(kw)
    return base


class TestAdminApi:
    """API 通道。"""

    def test_overview_five_kpis(self) -> None:
        """GET overview：五指标键齐全（访客/会话/留资率/挽回率/邮件效果）。"""
        client, _ = make_client()
        r = client.get(ADMIN_OVERVIEW_PATH, params=params(), headers=SUPERVISOR)
        data = r.json()["data"]
        assert r.json()["code"] == 0
        for key in ("visitors", "sessions", "lead_rate", "recover_rate", "mail"):
            assert key in data
        assert data["visitors"] == 5 and data["sessions"] == 2 and data["lead_rate"] == 50.0  # API 层百分比

    def test_funnel_endpoint(self) -> None:
        """GET funnel：四层 + 层间率。"""
        client, _ = make_client()
        r = client.get(ADMIN_FUNNEL_PATH, params=params(), headers=SUPERVISOR)
        data = r.json()["data"]
        assert [l["value"] for l in data["layers"]] == [5, 2, 1, 0]
        assert data["rates"]["会话率"] == 40.0 and data["rates"]["留资率"] == 50.0  # API 层百分比

    def test_mail_effect_endpoint(self) -> None:
        """GET mail-effect：送达率 = 1/2（1 送达 1 失败），失败单列。"""
        client, _ = make_client()
        r = client.get(ADMIN_MAIL_EFFECT_PATH, params=params(), headers=SUPERVISOR)
        row = r.json()["data"]["by_type"]["cart_recover"]
        assert row["sent"] == 2 and row["delivered"] == 1 and row["delivery_rate"] == 50.0  # API 层百分比

    def test_drill_endpoint_ok(self) -> None:
        """GET drill：注册指标返回 columns/items/total。"""
        client, _ = make_client()
        r = client.get(ADMIN_DRILL_PATH, params={"metric": "visitors", "date": "2026-10-11"},
                       headers=SUPERVISOR)
        data = r.json()["data"]
        assert data["total"] == 0 and isinstance(data["columns"], list)

    def test_drill_unknown_metric_40001(self) -> None:
        """未注册指标 → HTTP 400 + 40001。"""
        client, _ = make_client()
        r = client.get(ADMIN_DRILL_PATH, params={"metric": "foo", "date": "2026-10-11"},
                       headers=SUPERVISOR)
        assert r.status_code == 400 and r.json()["code"] == 40001

    def test_sales_role_denied_40301(self) -> None:
        """销售角色无 site:dashboard:read → HTTP 403 + 40301（NFR-SEC-02）。"""
        client, _ = make_client()
        r = client.get(ADMIN_OVERVIEW_PATH, params=params(), headers=SALES)
        assert r.status_code == 403 and r.json()["code"] == 40301


def test_default_denies_self_declared_header_roles() -> None:
    """CR P0-3 修复：缺省不信任客户端自声明头——未注入 claims_provider 且未显式
    开启 allow_header_claims 时，携带 X-User-Roles: supervisor 头也一律 40301。"""
    metrics = SiteMetrics(FakeSiteSource(), clock=MutableClock(D1).now)
    app = create_admin_app(metrics=metrics)  # 全缺省（关门形态）
    r = TestClient(app).get(ADMIN_OVERVIEW_PATH, params=params(), headers=SUPERVISOR)
    assert r.status_code == 403 and r.json()["code"] == 40301
