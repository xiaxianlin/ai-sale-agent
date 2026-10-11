# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""访客行为追踪用例（FR-SITE-002 / SR-SITE-002 §9：批量上报/枚举/consent 门控/兴趣画像/留存/注入）。"""

from __future__ import annotations

import pytest

from services.site.core.tracking.service import (
    BATCH_MAX_DEFAULT,
    ContextInjector,
    EventIngest,
    MemoryAuditTrail,
    MemoryEventStore,
    MemoryProfileStore,
    ProfileBuilder,
    RetentionCleaner,
    TrackingError,
    default_mask,
    render_prompt_hint,
)

from .conftest import FakeClock, StaticGate

DAY = 86400.0
CLOCK0 = 1_760_000_000.0


def make_event(**kw: object) -> dict:
    """构造一条合法原始事件（ev_id/ts 缺省自动补）。"""
    base = {"ev_id": f"e-{kw.get('type', 'page_view')}-x", "type": "page_view",
            "page": "/p/1", "ts": CLOCK0}
    base.update(kw)
    return base


def make_ingest(
    clock: FakeClock | None = None,
    *,
    gate: StaticGate | None = None,
    daily_max: int = 2000,
    store: MemoryEventStore | None = None,
) -> tuple[EventIngest, MemoryEventStore]:
    """构造被测接入器（默认放行门控）。"""
    store = store or MemoryEventStore()
    ingest = EventIngest(
        store, gate or StaticGate(True), mask_fn=default_mask,
        daily_max=daily_max, clock=clock or FakeClock(),
    )
    return ingest, store


def build_profile(events: list[dict], **kw: object):
    """辅助：接入事件 → 事件存储 → 画像构建（与 API 端点同构）。"""
    ingest, store = make_ingest(**{k: v for k, v in kw.items() if k in ("gate", "clock")})
    ingest.ingest("v-1", 1, True, events)
    builder = ProfileBuilder(**{k: v for k, v in kw.items() if k not in ("gate", "clock")})
    return builder.build("v-1", store.events_of("v-1")), store


# --------------------------------------------------------------------------- 批量接入（等价/异常/边界）


def test_batch_ingest_accepts_valid_events() -> None:
    """T1 批量上报解析：3 条合法事件 accepted=3 / dropped=0，入库字段完整（等价类）。"""
    ingest, store = make_ingest()
    events = [
        make_event(ev_id="e1", type="source_visit", referrer="https://www.google.com/"),
        make_event(ev_id="e2", type="product_view", payload={"sku": "ST-3200"}),
        make_event(ev_id="e3", type="dwell", dwell_ms=183000),
    ]
    result = ingest.ingest("v-1", 1, True, events)
    assert (result.accepted, result.dropped) == (3, 0)
    stored = store.events_of("v-1")
    assert len(stored) == 3
    assert stored[1].payload == {"sku": "ST-3200"}
    assert stored[2].dwell_ms == 183000


def test_invalid_type_dropped() -> None:
    """T2 事件类型枚举校验：非法 type 计入 dropped 且不落库（异常类）。"""
    ingest, store = make_ingest()
    result = ingest.ingest(
        "v-1", 1, True,
        [make_event(ev_id="e1", type="page_view"), make_event(ev_id="e2", type="mouse_move")],
    )
    assert (result.accepted, result.dropped) == (1, 1)
    assert [e.type for e in store.events_of("v-1")] == ["page_view"]


def test_pii_masked_and_truncated() -> None:
    """T8 PII 脱敏（邮箱/电话 → [redacted]）+ 超长截断 2048；内置 mask_fn 直接断言（等价+边界）。"""
    assert default_mask("mail a@b.io now") == "mail [redacted] now"
    assert default_mask("call +8613800000000") == "call [redacted]"
    ingest, store = make_ingest()
    long_page = "/p/" + "a" * 3000 + "?mail=buyer@x.com"
    ingest.ingest(
        "v-1", 1, True,
        [
            make_event(ev_id="e1", page="https://x.com/?c=buyer@x.com"),
            make_event(ev_id="e2", page="https://x.com/?t=+8613800000000"),
            make_event(ev_id="e3", page=long_page),
        ],
    )
    pages = [e.page for e in store.events_of("v-1")]
    assert "buyer@x.com" not in pages[0] and "[redacted]" in pages[0]
    assert "13800000000" not in pages[1]
    assert len(pages[2]) <= 2048 and "buyer@x.com" not in pages[2]


def test_consent_false_rejected_40302() -> None:
    """T3 客户端声明 consent=false → 40302 且零写入（仅必要会话拒收行为事件，异常类）。"""
    ingest, store = make_ingest()
    with pytest.raises(TrackingError) as ei:
        ingest.ingest("v-1", 1, False, [make_event(ev_id="e1")])
    assert ei.value.code == 40302
    assert store.events_of("v-1") == []


def test_gate_denied_rejected_40302() -> None:
    """T4 服务端兜底门：会话 consent 非 granted（SDK 被篡改绕过）→ 40302（异常类，SR-SITE-007 §6.2）。"""
    gate = StaticGate(False)
    ingest, store = make_ingest(gate=gate)
    with pytest.raises(TrackingError) as ei:
        ingest.ingest("v-1", 1, True, [make_event(ev_id="e1")])
    assert ei.value.code == 40302
    assert gate.checks == 1  # 确经服务端门校验（非仅客户端标志位）
    assert store.events_of("v-1") == []


def test_batch_size_boundaries() -> None:
    """T6+T7 批量边界：恰 50 条（上限值）整批通过；51 条 > 上限 → 整批 40001（边界两侧）。"""
    ingest, store = make_ingest()
    at_limit = [make_event(ev_id=f"ok{i}") for i in range(BATCH_MAX_DEFAULT)]
    assert ingest.ingest("v-1", 1, True, at_limit).accepted == 50
    over = [make_event(ev_id=f"over{i}") for i in range(BATCH_MAX_DEFAULT + 1)]
    with pytest.raises(TrackingError) as ei:
        ingest.ingest("v-2", 1, True, over)
    assert ei.value.code == 40001
    assert store.events_of("v-2") == []


def test_daily_limit_rejected_42901() -> None:
    """T9 事件日限流：超 SITE_EVENTS_DAILY_MAX → 42901（边界）。"""
    clock = FakeClock()
    ingest, _ = make_ingest(clock, daily_max=5)
    accepted = ingest.ingest("v-1", 1, True, [make_event(ev_id=f"e{i}") for i in range(5)])
    assert accepted.accepted == 5
    with pytest.raises(TrackingError) as ei:
        ingest.ingest("v-1", 1, True, [make_event(ev_id="e-over")])
    assert ei.value.code == 42901


def test_replay_same_evid_stored_once() -> None:
    """T5 客户端重试重放：同 ev_id 仅首次写入，重放 accepted=0（幂等，Redis SETNX 语义）。"""
    ingest, store = make_ingest()
    batch = [make_event(ev_id="e-dup", type="product_view", payload={"sku": "ST-3200"})]
    first = ingest.ingest("v-1", 1, True, batch)
    second = ingest.ingest("v-1", 1, True, list(batch))
    assert (first.accepted, second.accepted) == (1, 0)
    assert len(store.events_of("v-1")) == 1


# --------------------------------------------------------------------------- 兴趣画像（加权聚合/边界）


def test_profile_weighted_score_with_decay() -> None:
    """T10 加权聚合：旧会话 1 次+8 分钟 → 0.8*(1*1.0+8*0.5)=4.0；新会话 2 次 → +2.0；合计 6.0（等价类）。"""
    events = [
        make_event(ev_id="a1", type="product_view", ts=CLOCK0, payload={"sku": "ST-3200"}),
        make_event(ev_id="a2", type="dwell", ts=CLOCK0 + 60, dwell_ms=480_000, payload={"sku": "ST-3200"}),
        make_event(ev_id="b1", type="product_view", ts=CLOCK0 + 3600, payload={"sku": "ST-3200"}),
        make_event(ev_id="b2", type="product_view", ts=CLOCK0 + 3660, payload={"sku": "ST-3200"}),
    ]
    profile, _ = build_profile(events)
    top = profile.interests[0]
    assert top.sku == "ST-3200"
    assert top.views == 3 and top.dwell_s == 480
    assert top.score == pytest.approx(6.0)
    assert top.level == "high"  # 6.0 ≥ 强兴趣阈值 3.5
    assert profile.sessions == 2


def test_profile_high_level_boundary() -> None:
    """T11 强兴趣阈值边界：score=3.5 → high；3.4 → mid（边界两侧）。"""
    at_bar, _ = build_profile([
        make_event(ev_id="1", type="product_view", payload={"sku": "A"}),
        make_event(ev_id="2", type="product_view", payload={"sku": "A"}),
        make_event(ev_id="3", type="product_view", payload={"sku": "A"}),
        make_event(ev_id="4", type="dwell", dwell_ms=60_000, payload={"sku": "A"}),  # 3 + 0.5 = 3.5
    ])
    below, _ = build_profile([
        make_event(ev_id="1", type="product_view", payload={"sku": "B"}),
        make_event(ev_id="2", type="product_view", payload={"sku": "B"}),
        make_event(ev_id="3", type="product_view", payload={"sku": "B"}),
        make_event(ev_id="4", type="dwell", dwell_ms=48_000, payload={"sku": "B"}),  # 3 + 0.4 = 3.4
    ])
    assert at_bar.interests[0].level == "high"
    assert below.interests[0].level == "mid"


def test_profile_shape_top_n_and_seq() -> None:
    """T12+T13 画像形态：TOP N 截断降序；轨迹 page_seq 去 query、环形截取最近 K 条（边界）。"""
    events = [make_event(ev_id=f"p{i}", type="page_view", ts=CLOCK0 + i,
                         page=f"/p/{i}?utm_source=x") for i in range(5)]
    for i in range(3):  # 三个 SKU 差异化访问次数：S0=3、S1=2、S2=1
        sku = f"S{i}"
        for j in range(3 - i):
            events.append(make_event(ev_id=f"{sku}-{j}", type="product_view",
                                     ts=CLOCK0 + 100 + j, payload={"sku": sku}))
    profile, _ = build_profile(events, top_n=2, seq_len=3)
    assert [i.sku for i in profile.interests] == ["S0", "S1"]
    assert profile.interests[0].score >= profile.interests[1].score
    assert profile.page_seq == ("/p/2", "/p/3", "/p/4")


# --------------------------------------------------------------------------- 注入渲染（SRS §16.4 画像注入可用）


def make_profile_with_interests() -> object:
    """构造含 ST-3200(3 次/8 分钟) + 来源/加购的画像（注入断言素材）。"""
    events = [
        make_event(ev_id="s0", type="source_visit", ts=CLOCK0 - 10,
                   referrer="https://www.google.com/", payload={"source": "google"}),
        make_event(ev_id="a1", type="product_view", ts=CLOCK0, payload={"sku": "ST-3200"}),
        make_event(ev_id="a2", type="dwell", ts=CLOCK0 + 60, dwell_ms=480_000, payload={"sku": "ST-3200"}),
        make_event(ev_id="b1", type="product_view", ts=CLOCK0 + 3600, payload={"sku": "ST-3200"}),
        make_event(ev_id="b2", type="product_view", ts=CLOCK0 + 3660, payload={"sku": "ST-3200"}),
        make_event(ev_id="c1", type="add_cart", ts=CLOCK0 + 3700, payload={"sku": "ST-3200"}),
        make_event(ev_id="p1", type="page_view", ts=CLOCK0 + 3800,
                   page="/products/st-3200?utm_source=google"),
    ]
    ingest, store = make_ingest()
    ingest.ingest("v-1", 1, True, events)
    return ProfileBuilder().build("v-1", store.events_of("v-1"))


def test_render_prompt_hint_contains_browsed() -> None:
    """T14+T15 注入格式：含「访客浏览过 ST-3200(3次/8分钟,强)」+来源+当前页+加购未下单；≤上限截断（等价+边界）。"""
    hint = render_prompt_hint(make_profile_with_interests())  # type: ignore[arg-type]
    assert hint.startswith("[访客画像]")
    assert "访客浏览过 ST-3200(3次/8分钟,强)" in hint
    assert "来源:google" in hint
    assert "当前页:/products/st-3200" in hint
    assert "加购未下单:ST-3200" in hint
    assert len(render_prompt_hint(make_profile_with_interests(), max_chars=60)) <= 60  # type: ignore[arg-type]


def test_inject_rounds_gate() -> None:
    """T16 注入轮次：round 0（会话建立）与 N 的倍数注入，其余 None（边界）。"""
    store = MemoryProfileStore()
    store.save(make_profile_with_interests())  # type: ignore[arg-type]
    injector = ContextInjector(store, inject_every_n=3)
    for rnd in (0, 3, 6):
        assert injector.prompt_hint("v-1", rnd) is not None, f"round {rnd} 应注入"
    for rnd in (1, 2, 4):
        assert injector.prompt_hint("v-1", rnd) is None, f"round {rnd} 不应注入"


def test_inject_missing_and_empty_profile() -> None:
    """T17+T18 画像缺失 → None 不抛错（降级无画像应答）；空事件集画像无「浏览过」断言素材（空集）。"""
    injector = ContextInjector(MemoryProfileStore())
    assert injector.prompt_hint("v-missing", 0) is None
    empty_profile, _ = build_profile([])
    assert empty_profile.interests == () and empty_profile.page_seq == ()
    assert "访客浏览过" not in render_prompt_hint(empty_profile)


# --------------------------------------------------------------------------- 留存清理（恰 N 天保留 / N+1 清除 + 审计）


def make_cleaner(clock: FakeClock | None = None, **kw: object):
    """构造留存清理器 + 审计轨迹。"""
    clock = clock or FakeClock()
    store = MemoryEventStore()
    audit = MemoryAuditTrail()
    cleaner = RetentionCleaner(store, audit, retention_days=kw.pop("retention_days", 30),
                               clock=clock, **kw)
    return cleaner, store, audit, clock


def test_retention_boundary_exact_days_kept() -> None:
    """R1 恰 N 天（created == cutoff）保留（保留期边界，FR-SITE-007）。"""
    cleaner, store, _, clock = make_cleaner()
    ingest, _ = make_ingest(clock=clock, store=store)
    ingest.ingest("v-1", 1, True, [make_event(ev_id="edge", ts=clock() - 30 * DAY)])
    assert cleaner.sweep()["deleted"] == 0
    assert len(store.events_of("v-1")) == 1


def test_retention_one_day_over_cleared() -> None:
    """R2 N+1 天（created < cutoff）清除（保留期边界）。"""
    cleaner, store, _, clock = make_cleaner()
    ingest, _ = make_ingest(clock=clock, store=store)
    ingest.ingest("v-1", 1, True, [make_event(ev_id="old", ts=clock() - 31 * DAY)])
    assert cleaner.sweep()["deleted"] == 1
    assert store.events_of("v-1") == []


def test_retention_audit_counts_and_cutoff() -> None:
    """R3 清理审计：deleted 计数与 cutoff 写入审计轨迹（等价类，验收「删除审计计数」）。"""
    cleaner, store, audit, clock = make_cleaner()
    ingest, _ = make_ingest(clock=clock, store=store)
    ingest.ingest("v-1", 1, True, [make_event(ev_id="o1", ts=clock() - 40 * DAY)])
    ingest.ingest("v-2", 1, True, [make_event(ev_id="o2", ts=clock() - 40 * DAY)])
    result = cleaner.sweep()
    assert result["deleted"] == 2
    entry = audit.filter("tracking.retention_sweep")[-1]
    assert entry["deleted"] == 2
    assert entry["cutoff"] == pytest.approx(clock() - 30 * DAY)


def test_retention_rerun_idempotent_and_clamped() -> None:
    """R4+R5 重跑幂等（deleted=0 仍记审计）与保留期钳制 1..730 天（幂等+异常输入防呆）。"""
    cleaner, store, audit, clock = make_cleaner()
    ingest, _ = make_ingest(clock=clock, store=store)
    ingest.ingest("v-1", 1, True, [make_event(ev_id="o1", ts=clock() - 40 * DAY)])
    assert cleaner.sweep()["deleted"] == 1
    assert cleaner.sweep()["deleted"] == 0  # 重跑：无可清数据，仍记审计
    assert len(audit.filter("tracking.retention_sweep")) == 2
    clamp_low, _, _, low_clock = make_cleaner(retention_days=0)
    assert clamp_low.sweep()["cutoff"] == pytest.approx(low_clock() - DAY)
    clamp_high, _, _, high_clock = make_cleaner(retention_days=999_999)
    assert clamp_high.sweep()["cutoff"] == pytest.approx(high_clock() - 730 * DAY)


def test_retention_visitor_profile_also_swept() -> None:
    """R5b 画像冷存随清理：last_active 早于 cutoff 的画像一并清除（等价类）。"""
    store = MemoryEventStore()
    profiles = MemoryProfileStore()
    audit = MemoryAuditTrail()
    cleaner = RetentionCleaner(store, audit, profile_store=profiles, retention_days=30,
                               clock=lambda: CLOCK0)
    ingest, _ = make_ingest(store=store)
    ingest.ingest("v-old", 1, True, [make_event(ev_id="o", ts=CLOCK0 - 40 * DAY)])
    ingest.ingest("v-new", 1, True, [make_event(ev_id="n", ts=CLOCK0)])
    profiles.save(ProfileBuilder().build("v-old", store.events_of("v-old")))
    profiles.save(ProfileBuilder().build("v-new", store.events_of("v-new")))
    deleted = cleaner.sweep()
    assert deleted["visitor_profile"] == 1
    assert profiles.load("v-old") is None and profiles.load("v-new") is not None
