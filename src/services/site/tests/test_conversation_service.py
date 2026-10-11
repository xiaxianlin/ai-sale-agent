# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""独立站对话引擎用例（FR-SITE-003 / SR-SITE-003 §9：画像注入/话术分流/防线/邀聊/留资）。

验收口径映射：SRS §16.4「主动邀聊阈值有效」「留资率基线」+ R-03 幻觉防线（全渠道）。
覆盖五类：等价类（C1~C5/C13/C21）、边界（C11/C15~C17）、异常（C6~C9/C23）、
并发幂等（C18/C24）、降级（C9/C10/C20）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from services.site.core.conversation.service import (
    DEFAULT_KB_TYPES_SITE,
    DEFAULT_SCENE_TAGS,
    FakeSiteLlm,
    InviteEngine,
    LlmDownstreamError,
    LeadCapture,
    MemoryConversationRepo,
    MemoryCustomerWriter,
    MemoryInviteLogStore,
    MemoryLeadStore,
    MemoryGapRecorder,
    SiteChatService,
    TrackedEvent,
    parse_hits,
)
from services.site.core.session.service import SiteSessionRecord
from services.site.core.tracking.service import MemoryProfileStore, ProfileBuilder

from .conftest import FakeClock, FakeSearch, StaticGate

CLOCK0 = 1_760_000_000.0
CHUNK = {"title": "ST-3200 MOQ", "text": "ST-3200 起订量 500 件，标准交期 25 天。"}
INVITE_CHUNK = {"title": "site_invite/dwell", "text": "Saw you checking {sku} — need a quick quote?"}


class BoomLlm:
    """下游故障桩：complete 直接抛 LlmDownstreamError（降级路径）。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, *, timeout_s: float, max_tokens: int, temperature: float) -> str:
        self.calls += 1
        raise LlmDownstreamError("agent-core 超时")


def make_session(lang: str = "en", uid: str = "v-site-1") -> SiteSessionRecord:
    """构造访客会话记录（对话引擎入参）。"""
    return SiteSessionRecord(
        uid=uid, tenant_id=1, sdk_key_id=1, lang=lang, consent="granted", created_at=CLOCK0,
        page="https://shop.example.com/products/st-3200",
    )


def make_chat(
    *,
    chunks: list[dict] | None = None,
    script: dict[str, str] | None = None,
    llm: Any = None,
    hint: str | None = "[访客画像] 访客浏览过 ST-3200(3次/8分钟,强)",
    card_fn: Any = None,
    repo: MemoryConversationRepo | None = None,
    gaps: MemoryGapRecorder | None = None,
    invites: MemoryInviteLogStore | None = None,
    lang: str = "en",
) -> tuple[SiteChatService, FakeSiteLlm, FakeSearch]:
    """构造对话服务（默认单命中 chunk + 画像 hint）。"""
    llm = llm or FakeSiteLlm(script)
    search = FakeSearch(chunks if chunks is not None else [CHUNK])
    service = SiteChatService(
        llm, search, (lambda uid, rnd: hint),
        card_fn=card_fn, repo=repo or MemoryConversationRepo(),
        gaps=gaps or MemoryGapRecorder(), invites=invites,
    )
    return service, llm, search  # type: ignore[return-value]


# --------------------------------------------------------------------------- 对话主链路（等价类）


def test_prompt_contains_profile_hint() -> None:
    """C1+C13 画像注入与多语言：Prompt 含「访客浏览过 ST-3200」；会话语言（de）写入 Prompt（等价类，SRS §16.4）。"""
    service, llm, _ = make_chat()
    r = service.reply(make_session(), "MOQ for ST-3200?")
    assert not r.fallback
    assert "[访客画像]" in llm.last_prompt
    assert "访客浏览过 ST-3200" in llm.last_prompt
    de_service, de_llm, _ = make_chat(lang="de")
    de_service.reply(make_session(lang="de"), "MOQ?")
    assert "de" in de_llm.last_prompt.split("【访客画像】")[0]


def test_search_receives_site_kb_types_and_scene_filter() -> None:
    """C2 site 检索分流：kb_types=product,skill + scene_tag 过滤含 site_chat（等价类，SR-SITE-003 §2.4）。"""
    service, _, search = make_chat()
    service.reply(make_session(), "MOQ?")
    call = search.calls[0]
    assert tuple(call["kb_types"]) == DEFAULT_KB_TYPES_SITE
    assert set(call["filters"]["scene_tag"]) & set(DEFAULT_SCENE_TAGS)


def test_reply_refs_and_envelope_parse() -> None:
    """C3+C26 引用随行与信封解析：R#→chunk 映射；检索信封容忍缺字段（等价+边界）。"""
    service, _, _ = make_chat()
    r = service.reply(make_session(), "MOQ?")
    assert r.knowledge_refs[0]["ref"] == "R1"
    assert r.knowledge_refs[0]["chunk_id"] == 1
    assert "[R1]" in r.content
    hits = parse_hits({"results": [{"chunk_id": 7}, {"chunk_id": 8, "score": "0.5"}]})
    assert hits[0].score == 0.0 and hits[1].score == 0.5 and hits[0].title == ""
    assert parse_hits(None) == []


def test_messages_persisted_both_directions() -> None:
    """C4 会话消息落库：inbound(buyer)/outbound(ai) 双向（等价类）。"""
    repo: MemoryConversationRepo = MemoryConversationRepo()
    service, _, _ = make_chat(repo=repo)
    service.reply(make_session(), "MOQ?")
    msgs = repo.messages_of(repo.conversation_ids_of("v-site-1")[0])
    assert [m["role"] for m in msgs] == ["buyer", "ai"]
    assert msgs[0]["content"] == "MOQ?"


def test_placeholder_filled_and_unresolved() -> None:
    """C5+C6 占位符：有产品卡按阶梯价结构化填充 USD（绝不猜数字）；无卡悬空 → 兜底（等价+异常）。"""

    def card_fn(sku: str) -> dict | None:
        return {"ladder": [{"qty": 500, "price": 21.0}, {"qty": 1000, "price": 19.0}],
                "moq": 500, "lead_time_days": 25}

    script = {"{{PRICE": json.dumps({
        "reply_draft": "For 1000 pcs: {{PRICE:ST-3200:1000}} [R1].",
        "knowledge_refs": ["R1"], "missing_info": [],
        "intent": "询价", "confidence": 0.9,
    }, ensure_ascii=False)}
    service, _, _ = make_chat(script=script, card_fn=card_fn)
    r = service.reply(make_session(), "price for 1000 pcs?")
    assert not r.fallback
    assert "USD 19.0/pc" in r.content and "{{" not in r.content
    # 无产品卡：占位符悬空 → 兜底不直发（KEY_MISSING）
    bare, _, _ = make_chat(script=script, card_fn=lambda sku: None)
    r2 = bare.reply(make_session(), "price?")
    assert r2.fallback and r2.reason_code == "key_missing"


def test_price_leak_blocked() -> None:
    """C7 价格残留硬拦截：LLM 自填 $ 数字 → 不直发走兜底（异常类，SRS §7.4/R-03 复用断言）。"""
    script = {"best price?": json.dumps({
        "reply_draft": "Sure, it is $9.5 per piece only for you! [R1]",
        "knowledge_refs": ["R1"], "missing_info": [],
    })}
    service, _, _ = make_chat(script=script)
    r = service.reply(make_session(), "best price?")
    assert r.fallback and r.reason_code == "price_leak"
    assert "$9.5" not in r.content


def test_bad_llm_output_fallback() -> None:
    """C8+C11 生成输出防线：伪造/越界引用（R9）与不可解析输出 → 兜底不直发（异常类）。"""
    script_refs = {"R": json.dumps({
        "reply_draft": "Trust me [R9].", "knowledge_refs": ["R9"], "missing_info": [],
    })}
    service, _, _ = make_chat(script=script_refs)
    r = service.reply(make_session(), "MOQ?")
    assert r.fallback and r.reason_code == "invalid_refs"
    raw_service, _, _ = make_chat(script={"R1": "这不是 JSON"})
    r2 = raw_service.reply(make_session(), "MOQ?")
    assert r2.fallback and r2.reason_code == "llm_output_error"


def test_zero_hit_no_llm_and_gap_recorded() -> None:
    """C9 零命中不生成：LLM 零调用 + 缺口登记（降级类，R-03 闸 4）。"""
    gaps = MemoryGapRecorder()
    service, llm, _ = make_chat(chunks=[], gaps=gaps)
    r = service.reply(make_session(), "quantum flux?")
    assert r.fallback and r.reason_code == "kb_no_hit"
    assert llm.calls == 0
    assert gaps.gaps[0]["reason"] == "kb_no_hit"


def test_llm_downstream_error_fallback() -> None:
    """C10 LLM 下游故障 → 固定话术降级（访客侧永不静默）（降级类）。"""
    service, _, _ = make_chat(llm=BoomLlm())
    r = service.reply(make_session(), "MOQ?")
    assert r.fallback and r.reason_code == "downstream"
    assert r.content  # 固定话术非空


def test_fallback_template_by_lang() -> None:
    """C12 兜底话术多语言：en/zh 模板不同且各含转人工/留邮箱引导（降级+等价类）。"""
    svc_en, _, _ = make_chat(chunks=[])
    r_en = svc_en.reply(make_session("en"), "?")
    svc_zh, _, _ = make_chat(chunks=[])
    r_zh = svc_zh.reply(make_session("zh"), "？")
    assert r_en.content != r_zh.content
    assert "@" in r_en.content and "邮箱" in r_zh.content


# --------------------------------------------------------------------------- 主动邀聊（边界/并发/降级）


def make_invite(
    *,
    with_template: bool = True,
    gate: StaticGate | None = None,
    clock: FakeClock | None = None,
    daily_max: int = 2,
) -> tuple[InviteEngine, FakeClock]:
    """构造邀聊引擎（with_template=False 模拟 kb 无 site_invite 模板 → 默认开场白）。"""
    clock = clock or FakeClock()

    def kb_search(*, query: str, kb_types=None, filters=None, top_k=1):
        if not with_template:
            return {"results": []}
        return {"results": [{"chunk_id": 1, "score": 0.9, "title": "tpl", "text": INVITE_CHUNK["text"]}]}

    engine = InviteEngine(
        kb_search, gate or StaticGate(True), MemoryInviteLogStore(),
        dwell_s=45, repeat_view_n=3, daily_max=daily_max,
        cooldown_s=600, clock=clock,
    )
    return engine, clock


def dwell_event(ms: int, ts: float | None = None) -> TrackedEvent:
    """停留事件构造器。"""
    return TrackedEvent(ev_id=f"d{ms}-{ts}", type="dwell", page="/products/st-3200",
                        ts=ts if ts is not None else CLOCK0, dwell_ms=ms)


def test_invite_dwell_threshold_and_boundary() -> None:
    """C14+C15 停留阈值：45s 触发 dwell（ctx 快照+模板下发）；44.9s 不触发（等价+边界）。"""
    engine, _ = make_invite()
    below = engine.on_event("v-1", dwell_event(44_999))
    assert not below.triggered and below.reason == "no_rule"
    engine2, _ = make_invite()
    d = engine2.on_event("v-1", dwell_event(45_000))
    assert d.triggered and d.trigger_type == "dwell"
    assert d.invite is not None and d.invite["invite_id"]
    assert "quick quote" in d.invite["content"]  # kb site_invite 模板下发
    log = engine2.logs.logs_of("v-1")[0]
    assert log.trigger_ctx == {"page": "/products/st-3200", "dwell_s": 45}


def test_invite_repeat_view_threshold() -> None:
    """C16 同品重复访问：nth=3 触发 repeat_view 且模板槽位 {sku} 填充；nth=2 不触发（边界）。"""
    engine, _ = make_invite()
    below = engine.on_event("v-1", TrackedEvent(
        "r1", "repeat_view", "/p", CLOCK0, payload={"sku": "A", "nth": 2}))
    hit = engine.on_event("v-2", TrackedEvent(
        "r2", "repeat_view", "/p", CLOCK0, payload={"sku": "ST-3200", "nth": 3}))
    assert not below.triggered
    assert hit.triggered and hit.trigger_type == "repeat_view"
    assert "ST-3200" in hit.invite["content"]  # 模板槽位 {sku} 填充


def test_invite_daily_cap_two() -> None:
    """C17 每访客每日 ≤2 次：第 1/2 次下发，第 3 次 daily_cap 拒绝（边界，防骚扰）。"""
    engine, clock = make_invite(daily_max=2)
    first = engine.on_event("v-1", dwell_event(45_000, CLOCK0))
    clock.advance(3600)  # 跨过冷却窗
    second = engine.on_event("v-1", dwell_event(45_000, clock()))
    clock.advance(3600)
    third = engine.on_event("v-1", dwell_event(45_000, clock()))
    assert first.triggered and second.triggered
    assert not third.triggered and third.reason == "daily_cap"


def test_invite_cooldown_blocks_repeat() -> None:
    """C18 冷却期内重复邀聊拒绝（并发/幂等，防连发骚扰）。"""
    engine, clock = make_invite()
    first = engine.on_event("v-1", dwell_event(45_000, CLOCK0))
    second = engine.on_event("v-1", dwell_event(60_000, CLOCK0 + 5))
    assert first.triggered
    assert not second.triggered and second.reason == "cooldown"


def test_invite_consent_short_circuit() -> None:
    """C19 consent 非 granted → 短路不下发（安全，FR-SITE-007 门控联动）。"""
    engine, _ = make_invite(gate=StaticGate(False))
    d = engine.on_event("v-1", dwell_event(45_000))
    assert not d.triggered and d.reason == "consent"


def test_invite_opener_fallback_default() -> None:
    """C20 开场白降级：kb 无 site_invite 模板 → 默认 en 开场（降级类）。"""
    engine, _ = make_invite(with_template=False)
    d = engine.on_event("v-1", dwell_event(45_000))
    assert d.triggered and d.invite["content"]
    assert "{sku}" not in d.invite["content"]


def test_invite_funnel_marks() -> None:
    """C20b 漏斗回填：opened/replied/lead 三阶段时间戳条件写（等价类，幂等列）。"""
    engine, clock = make_invite()
    d = engine.on_event("v-1", dwell_event(45_000))
    engine.mark_opened("v-1", clock())
    engine.mark_opened("v-1", clock() + 5)  # 重复上报不覆盖
    engine.mark_replied("v-1", clock() + 10)
    engine.mark_lead("v-1", clock() + 20)
    log = engine.logs.logs_of("v-1")[0]
    assert log.opened_at == clock() and log.replied_at == clock() + 10 and log.lead_at == clock() + 20


# --------------------------------------------------------------------------- 留资（等价/异常/幂等）


def make_lead(
    *, profiles: MemoryProfileStore | None = None, invites: MemoryInviteLogStore | None = None,
    events_sink: Any = None,
) -> tuple[LeadCapture, MemoryCustomerWriter, MemoryLeadStore, MemoryInviteLogStore]:
    """构造留资引擎（含画像/事件/邀聊联动桩）。"""
    customers = MemoryCustomerWriter()
    leads = MemoryLeadStore()
    invites = invites or MemoryInviteLogStore()
    capture = LeadCapture(
        customers, leads, profiles=profiles or MemoryProfileStore(), invites=invites,
        events_sink=events_sink,
    )
    return capture, customers, leads, invites


def test_lead_capture_valid_email_creates_customer() -> None:
    """C21+C22 合法留资：email（归一化+画像标签）/WhatsApp（E.164）均建 Customer 渠道 site（等价类）。"""
    from services.site.core.tracking.service import TrackedEvent as TE

    profiles = MemoryProfileStore()
    events = [TE("e1", "product_view", "/p", CLOCK0, payload={"sku": "ST-3200"})]
    profiles.save(ProfileBuilder().build("v-site-1", events))
    capture, customers, leads, _ = make_lead(profiles=profiles)
    result = capture.capture(make_session(), {"email": "Buyer@Example.COM"})
    assert result["ok"] is True and result["customer_created"] is True
    ref = customers.find_by_email("buyer@example.com")
    assert ref is not None and ref["channels"]["site"] == "v-site-1"
    assert "interest:ST-3200" in ref["profile_tags"]  # 画像标签随留资写入 Customer
    assert leads.leads_of("v-site-1")[0]["email"] == "buyer@example.com"
    # WhatsApp E.164 形态
    wa_result = capture.capture(make_session(uid="v-site-2"), {"whatsapp": "+4915112345678"})
    assert wa_result["ok"] is True


def test_lead_capture_invalid_fields_rejected() -> None:
    """C23 非法 email / 非法 whatsapp / 双空 → ok=False + 字段级错误（异常类，lead_ack 形态）。"""
    capture, _, _, _ = make_lead()
    bad_mail = capture.capture(make_session(), {"email": "not-an-email"})
    bad_wa = capture.capture(make_session(), {"whatsapp": "12345"})
    empty = capture.capture(make_session(), {})
    assert bad_mail["ok"] is False and "email" in bad_mail["errors"]
    assert bad_wa["ok"] is False and "whatsapp" in bad_wa["errors"]
    assert empty["ok"] is False and set(empty["errors"]) == {"email", "whatsapp"}


def test_lead_capture_same_email_no_duplicate_customer() -> None:
    """C24 同邮箱重复留资不重建 Customer（幂等，FR-SITE-005 合并入口）。"""
    capture, _, _, _ = make_lead()
    first = capture.capture(make_session(), {"email": "buyer@x.com"})
    second = capture.capture(make_session(uid="v-site-2"), {"email": "buyer@x.com"})
    assert first["customer_id"] == second["customer_id"]
    assert first["customer_created"] is True and second["customer_created"] is False


def test_lead_capture_side_effects() -> None:
    """C25 留资联动：lead 事件追加 + 邀聊 lead_at 回填 + chat 先行 replied_at 回填（等价类）。"""
    invites = MemoryInviteLogStore()
    sink: list[Any] = []
    capture, _, _, _ = make_lead(invites=invites, events_sink=sink.append)
    invites.append_dwell_log("v-site-1", CLOCK0)
    service, _, _ = make_chat(invites=invites)
    service.reply(make_session(), "ok i want a quote")  # 对话阶段回填 replied_at
    result = capture.capture(make_session(), {"email": "b@x.com"})
    assert result["ok"] is True
    log = invites.logs_of("v-site-1")[0]
    assert log.replied_at is not None and log.lead_at is not None
    assert sink and sink[0].type == "lead"
