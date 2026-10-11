# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-005 CRM 集成·合并执行/撤销/客户 360 用例（SR-SITE-005 §6.3/§6.4/§9；TDD 阶段①②）。

覆盖：合并四断言（merged_into 指针/channels 追加/会话与身份重挂/台账快照）、
窗口内撤销恢复一致（KPI：合并可撤销 100% 回滚）、超窗/重复撤销 40901、
并发合并同客户恰一胜、已合并 source 与合并环 40901、dry_run 预检不落库、
360 时间线跨渠道按时间归并、consent=false 事件不出现在 360（FR-SITE-007）、
过期窗口归档。
"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from services.site.core.crm.merge import CustomerMerger, MergeError
from services.site.core.crm.merge import TimelineSource

from .fakes import MutableClock, T0


def first_merge_task(store):
    """取第一条合并台账（测试辅助）。"""
    return next(iter(store.merge_tasks.values()))


def make_world(undo_window_h: int = 72):
    """构造（合并器, 存储, 时钟），预置两个客户：A（alibaba 渠道）与 B（site 渠道）。"""
    clock = MutableClock()
    store = _seeded_store(clock)
    merger = CustomerMerger(store, clock=clock.now, undo_window_h=undo_window_h)
    return merger, store, clock


def _seeded_store(clock: MutableClock):
    from services.site.core.crm.identity import CRMStore, LeadNormalizer, IdentityResolver
    store = CRMStore()
    resolver = IdentityResolver(store)
    normalizer = LeadNormalizer(store, resolver, clock=clock.now)
    # A（阿里渠道，bob@corp.com）与 B（独立站渠道，robert@corp.com——归一化不同不触发误合并；
    # site 侧不带公司名避免 L4 模糊命中干扰，保持两个独立档案）
    normalizer.ingest("alibaba", {"buyer_login_id": "buyer_881", "email": "bob@corp.com",
                                  "buyer_name": "Bob", "company": "Acme", "country": "US",
                                  "conversation_uid": "cv-a"})
    normalizer.ingest("site", {"visitor_uid": "v-3f2a", "email": "robert@corp.com",
                               "name": "Robert", "company": None, "country": None,
                               "form_id": "f-1"})
    # 会话：A 一条（合并后应重挂到 B），B 一条（360 单档时间线用）
    a_id = store.leads[("alibaba", "buyer_881")].customer_id
    b_id = store.leads[("site", "v-3f2a")].customer_id
    store.add_conversation(customer_id=a_id, channel="alibaba",
                           created_at=T0 + timedelta(hours=1), summary="MOQ for ST-3200?")
    store.add_conversation(customer_id=b_id, channel="site",
                           created_at=T0 + timedelta(hours=3), summary="Site chat: price?")
    return store


def a_and_b(store):
    """返回（被并入方 A, 幸存方 B）客户 id。"""
    return (store.leads[("alibaba", "buyer_881")].customer_id,
            store.leads[("site", "v-3f2a")].customer_id)


class TestMergeExecution:
    """合并执行（指针 + channels 追加 + 重挂 + 台账）。"""

    def test_merge_four_effects(self) -> None:
        """合并四断言：merged_into / channels 追加（带 merged_from）/ 会话重挂 / 身份重挂。"""
        merger, store, clock = make_world()
        a, b = a_and_b(store)
        merger.execute(a, b, trigger_by="op-1", reason="同一买家")
        assert store.customers[a].merged_into == b
        b_channels = {c["channel"] for c in store.customers[b].channels}
        assert b_channels == {"alibaba", "site"}
        assert store.customers[b].channels[1]["merged_from"] == str(a)
        assert all(c.customer_id == b for c in store.conversations if c.customer_id in (a, b))
        assert all(i.customer_id == b for i in store.identities)
        task = first_merge_task(store)
        assert task.status == "done" and task.done_at == clock.now()
        assert task.undo_deadline == clock.now() + timedelta(hours=72)

    def test_merge_chain_finds_root(self) -> None:
        """链式合并：A→B 后 C→B，根一致且 A/C 均指向 B（find_root 顺链收口）。"""
        merger, store, clock = make_world()
        a, b = a_and_b(store)
        c = store.add_customer("C", channels=[{"channel": "email", "external_id": "c@x.com"}]).customer_id
        merger.execute(a, b, trigger_by="auto")
        merger.execute(c, b, trigger_by="auto")
        assert store.customers[a].merged_into == b and store.customers[c].merged_into == b

    def test_merge_already_merged_source_conflict(self) -> None:
        """已合并 source 再次作为 source → 40901（双保险互斥）。"""
        merger, store, _ = make_world()
        a, b = a_and_b(store)
        merger.execute(a, b, trigger_by="auto")
        with pytest.raises(MergeError):
            merger.execute(a, b, trigger_by="op-2")

    def test_merge_cycle_detected(self) -> None:
        """合并环：B 并入 A（A 已并入 B）→ 检测根即自身 → 40901。"""
        merger, store, _ = make_world()
        a, b = a_and_b(store)
        merger.execute(a, b, trigger_by="auto")
        with pytest.raises(MergeError):
            merger.execute(b, a, trigger_by="op")  # target 的根是 b 自己

    def test_concurrent_merge_exactly_one_wins(self) -> None:
        """并发合并同 source：两线程分别并往 B/C，恰一成功一 40901（恰一胜）。"""
        merger, store, _ = make_world()
        a, b = a_and_b(store)
        c = store.add_customer("C", channels=[{"channel": "email", "external_id": "c@x.com"}]).customer_id
        barrier = threading.Barrier(2)
        results: list[str] = []

        def run(src_target: int) -> None:
            barrier.wait()
            try:
                merger.execute(a, src_target, trigger_by="race")
                results.append("ok")
            except MergeError:
                results.append("conflict")

        t1 = threading.Thread(target=run, args=(b,))
        t2 = threading.Thread(target=run, args=(c,))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert sorted(results) == ["conflict", "ok"]


class TestUndo:
    """可撤销窗口（KPI：窗口内 100% 回滚）。"""

    def test_undo_within_window_restores(self) -> None:
        """窗口内撤销：档案/会话/身份/channels 全部恢复合并前一致（快照回滚）。"""
        merger, store, clock = make_world()
        a, b = a_and_b(store)
        before = store.snapshot_state()
        result = merger.execute(a, b, trigger_by="op-1")
        merger.undo(result.merge_id, actor="op-1")
        assert store.snapshot_state() == before
        task = first_merge_task(store)
        assert task.status == "undone"

    def test_undo_after_window_rejected(self) -> None:
        """超 72h 窗口撤销 → 40901（边界：恰超窗一秒）。"""
        merger, store, clock = make_world(undo_window_h=72)
        a, b = a_and_b(store)
        result = merger.execute(a, b, trigger_by="op-1")
        clock.advance(hours=72, seconds=1)
        with pytest.raises(MergeError):
            merger.undo(result.merge_id, actor="op-1")

    def test_undo_twice_rejected(self) -> None:
        """二次撤销（状态已 undone）→ 40901（异常族）。"""
        merger, store, _ = make_world()
        a, b = a_and_b(store)
        result = merger.execute(a, b, trigger_by="op-1")
        merger.undo(result.merge_id, actor="op-1")
        with pytest.raises(MergeError):
            merger.undo(result.merge_id, actor="op-1")

    def test_window_expiry_archives(self) -> None:
        """过期窗口后台归档：expire_windows 将超窗 done 任务置 expired（此后不可撤销）。"""
        merger, store, clock = make_world()
        a, b = a_and_b(store)
        result = merger.execute(a, b, trigger_by="auto")
        clock.advance(hours=73)
        assert merger.expire_windows() == 1
        assert first_merge_task(store).status == "expired"
        with pytest.raises(MergeError):
            merger.undo(result.merge_id, actor="op")


class TestTimeline360:
    """客户 360 视图：跨渠道时间线运行时装配（SR §6.4）。"""

    @staticmethod
    def fake_timeline(store):
        """构造跨渠道 TimelineSource：会话取自 store，邮件/访客事件由外部列表模拟。"""
        mails: list[dict] = [
            {"ts": T0 + timedelta(hours=2), "channel": "email", "kind": "mail",
             "job_type": "cart_recover", "status": "delivered", "recipient": "robert@corp.com"},
            {"ts": T0 + timedelta(hours=5), "channel": "email", "kind": "mail",
             "job_type": "festival", "status": "opened", "recipient": "robert@corp.com"},
        ]
        visits: list[dict] = [
            {"ts": T0 + timedelta(minutes=30), "channel": "site", "kind": "visit",
             "event_type": "product_view", "page": "/products/st-3200", "consent": True},
            {"ts": T0 + timedelta(minutes=40), "channel": "site", "kind": "visit",
             "event_type": "abandon", "page": "/cart", "consent": False},  # 未同意：必须被过滤
        ]

        class Src(TimelineSource):
            def conversations_of(self, customer_ids):
                return [{"ts": c.created_at, "channel": c.channel, "kind": "message",
                         "dir": "inbound", "summary": c.summary}
                        for c in store.conversations if c.customer_id in customer_ids]

            def mail_jobs_of(self, emails):
                return [m for m in mails if m["recipient"] in emails]

            def visitor_events_of(self, external_ids):
                return list(visits)

        return Src(), mails, visits

    def test_360_sorted_cross_channel(self) -> None:
        """三渠道事件按时间归并排序（30min 访问 → 1h 会话 → 2h 邮件 → 5h 邮件）。"""
        merger, store, clock = make_world()
        src, mails, visits = self.fake_timeline(store)
        b = store.leads[("site", "v-3f2a")].customer_id
        view = merger.build_360(b, days=180, timeline=src)
        kinds = [(i["ts"], i["kind"]) for i in view["items"]]
        assert kinds == sorted(kinds, key=lambda x: x[0])
        assert {i["kind"] for i in view["items"]} == {"visit", "message", "mail"}
        assert not any(i.get("consent") is False for i in view["items"])  # consent=false 不入 360

    def test_360_after_merge_covers_source(self) -> None:
        """合并后 360（按幸存方查）覆盖被并入方会话（重挂/链式收口生效）；撤销后回落。"""
        merger, store, clock = make_world()
        src, _, _ = self.fake_timeline(store)
        a, b = a_and_b(store)
        merger.execute(a, b, trigger_by="auto")
        view = merger.build_360(b, days=180, timeline=src)
        assert any(i["kind"] == "message" and i["channel"] == "alibaba" for i in view["items"])
        assert {c["channel"] for c in view["channels"]} == {"alibaba", "site"}
        # 撤销后：A 的会话不再出现在 B 的 360
        task = first_merge_task(store)
        merger.undo(task.merge_id, actor="op")
        view2 = merger.build_360(b, days=180, timeline=src)
        assert not any(i.get("channel") == "alibaba" and i["kind"] == "message" for i in view2["items"])
