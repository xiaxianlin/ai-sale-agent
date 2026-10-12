# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化 API 通道用例（SR-SITE-004 §5；TDD 阶段①②，TestClient）。

端点：POST .../mail/sequences/{kind}/enroll、GET .../mail/jobs、POST .../mail/templates、
POST .../mail/tracking/event（回执/打开/退信 webhook 形态，X-Api-Key 内部鉴权）、
GET .../mail/stats（送达率 + ≥95% 判定）。
统一信封四键（04-接口设计 §1.3）；错误码 40001/40101/40301。
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from services.site.api.mail_api import (
    MAIL_STATS_PATH,
    MAIL_TEMPLATE_PATH,
    MAIL_TRACKING_EVENT_PATH,
    create_mail_app,
)
from services.site.core.errors import SiteConfigError
from services.site.core.mailing.scheduler import DomainRateGuard, MailScheduler
from services.site.core.mailing.sequences import MailStore, SequenceEngine
from services.site.core.mailing.templates import TemplateRegistry
from services.site.core.mailing.tracking import DeliveryTracker, SuppressionList

from .fakes import FakeSmtpTransport, MutableClock, T0

ENROLL_PATH = "/api/v1/site/mail/sequences/cart_recover/enroll"
TPL = {"cart_recover_remind": {"en": {"subject": "s {{ customer_name }}", "body_html": "b {{ cart_items }}"}},
       "cart_recover_offer": {"en": {"subject": "o", "body_html": "c {{ coupon_code }}"}},
       "cart_recover_last": {"en": {"subject": "l", "body_html": "f {{ customer_name }}"}}}


TEST_MAIL_API_KEY = "demo" + "-key"  # 测试专用 demo 凭据（非真实凭证）


def make_client(per_hour: int = 100):
    """装配邮件 API 应用（全内存依赖注入）并返回（client, world 组件）。"""
    clock = MutableClock()
    store = MailStore()
    engine = SequenceEngine(store, clock=clock.now)
    templates = TemplateRegistry(clock=clock.now)
    for key, versions in TPL.items():
        templates.upsert(key, versions)
    scheduler = MailScheduler(engine=engine, store=store, transport=FakeSmtpTransport(clock),
                              templates=templates, suppression=SuppressionList(),
                              rate_guard=DomainRateGuard(per_domain_per_hour=per_hour),
                              clock=clock.now)
    tracker = DeliveryTracker(store, engine, SuppressionList(), clock=clock.now)
    app = create_mail_app(scheduler=scheduler, tracker=tracker, templates=templates,
                          store=store, api_key=TEST_MAIL_API_KEY, clock=clock.now,
                          allow_header_claims=True)  # 测试显式声明 demo 形态（CR P0-3 修复）
    return TestClient(app), clock, store, tracker, scheduler


def enroll_body(**kw) -> dict:
    """合法 enroll 请求体。"""
    body = {"customer_id": 7, "email": "lead@buyer.com",
            "context": {"dedup_key": "c-9", "customer_name": "Ace", "cart_items": "ST-3200 x2"}}
    body.update(kw)
    return body


class TestMailApi:
    """API 通道（等价类 + 异常 + 鉴权）。"""

    def test_enroll_ok(self) -> None:
        """enroll 成功：code=0、返回 3 个排产 job 与计划时刻。"""
        client, *_ = make_client()
        r = client.post(ENROLL_PATH, json=enroll_body())
        assert r.status_code == 200
        payload = r.json()
        assert payload["code"] == 0
        assert len(payload["data"]["jobs"]) == 3
        assert payload["data"]["jobs"][0]["step_no"] == 1

    def test_enroll_invalid_kind_40001(self) -> None:
        """未知序列类型 → HTTP 400 + 40001（参数校验）。"""
        client, *_ = make_client()
        r = client.post("/api/v1/site/mail/sequences/flash_sale/enroll", json=enroll_body())
        assert r.status_code == 400 and r.json()["code"] == 40001

    def test_jobs_endpoint_filter(self) -> None:
        """GET jobs：按 sequence_id 过滤返回任务与状态。"""
        client, clock, store, *_ = make_client()
        seq_id = client.post(ENROLL_PATH, json=enroll_body()).json()["data"]["sequence_id"]
        r = client.get("/api/v1/site/mail/jobs", params={"sequence_id": seq_id})
        assert r.json()["data"]["total"] == 3

    def test_template_endpoint_ok_and_invalid(self) -> None:
        """POST templates：合法 upsert code=0；versions 缺字段 → 40001。"""
        client, *_ = make_client()
        ok = client.post(MAIL_TEMPLATE_PATH, json={
            "template_key": "festival_greet",
            "versions": {"en": {"subject": "Hi", "body_html": "{{ customer_name }}"}}})
        assert ok.status_code == 200 and ok.json()["code"] == 0
        bad = client.post(MAIL_TEMPLATE_PATH, json={
            "template_key": "festival_bad", "versions": {"en": {"subject": "only"}}})
        assert bad.status_code == 400 and bad.json()["code"] == 40001

    def test_tracking_event_open_advances(self) -> None:
        """tracking/event（webhook 形态）：open 事件使 job → opened。"""
        client, clock, store, tracker, scheduler = make_client()
        client.post(ENROLL_PATH, json=enroll_body())
        clock.advance(hours=1)
        scheduler.poll()
        job_id = next(j.job_id for j in store.jobs.values() if j.step_no == 1)
        r = client.post(MAIL_TRACKING_EVENT_PATH, json={"kind": "open", "job_id": job_id},
                        headers={"X-Api-Key": "demo-key"})
        assert r.status_code == 200 and r.json()["data"]["counted"] is True
        assert next(j for j in store.jobs.values() if j.job_id == job_id).status == "opened"

    def test_tracking_event_requires_api_key(self) -> None:
        """内部事件入口缺 X-Api-Key → 40101（服务间鉴权，04 §1.2）。"""
        client, *_ = make_client()
        r = client.post(MAIL_TRACKING_EVENT_PATH, json={"kind": "open", "job_id": 1})
        assert r.status_code == 401 and r.json()["code"] == 40101

    def test_stats_endpoint_reports_rate_and_target(self) -> None:
        """GET stats：送达率口径 + meets_target（≥95% 判定，SRS §16.4）。"""
        client, clock, store, tracker, scheduler = make_client()
        client.post(ENROLL_PATH, json=enroll_body())
        clock.advance(hours=1)
        scheduler.poll()
        r = client.get(MAIL_STATS_PATH)
        data = r.json()["data"]
        assert data["sent"] == 1 and data["delivered"] == 1
        assert data["delivery_rate"] == 1.0 and data["meets_target"] is True


def test_default_denies_write_without_claims_provider() -> None:
    """CR P0-3 修复：缺省不信任客户端自声明头——未注入 claims_provider 且未显式
    开启 allow_header_claims 时，写操作（enroll/templates）一律 40301（读/内部入口不受影响）。"""
    clock = MutableClock()
    store = MailStore()
    templates = TemplateRegistry(clock=clock.now)
    app = create_mail_app(  # 全缺省（关门形态；api_key 显式注入 demo 值）
        scheduler=MailScheduler(engine=SequenceEngine(store, clock=clock.now), store=store,
                                transport=FakeSmtpTransport(clock), templates=templates,
                                suppression=SuppressionList(),
                                rate_guard=DomainRateGuard(per_domain_per_hour=100), clock=clock.now),
        tracker=DeliveryTracker(store, SequenceEngine(store, clock=clock.now),
                                SuppressionList(), clock=clock.now),
        templates=templates, store=store, api_key=TEST_MAIL_API_KEY)
    r = TestClient(app).post(ENROLL_PATH, json=enroll_body())
    assert r.status_code == 403 and r.json()["code"] == 40301


def test_missing_api_key_fails_fast(monkeypatch) -> None:
    """CR P0-5 修复：未传 api_key 且环境变量 KB_MAIL_WEBHOOK_KEY 未配置 → 启动即
    SiteConfigError（fail-fast，拒绝无密钥运行；对齐 vault 缺主密钥口径）。"""
    monkeypatch.delenv("KB_MAIL_WEBHOOK_KEY", raising=False)
    clock = MutableClock()
    store = MailStore()
    templates = TemplateRegistry(clock=clock.now)
    with pytest.raises(SiteConfigError, match="KB_MAIL_WEBHOOK_KEY"):
        create_mail_app(
            scheduler=MailScheduler(engine=SequenceEngine(store, clock=clock.now), store=store,
                                    transport=FakeSmtpTransport(clock), templates=templates,
                                    suppression=SuppressionList(),
                                    rate_guard=DomainRateGuard(per_domain_per_hour=100),
                                    clock=clock.now),
            tracker=DeliveryTracker(store, SequenceEngine(store, clock=clock.now),
                                    SuppressionList(), clock=clock.now),
            templates=templates, store=store)
