# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-005 CRM 集成·线索归一化与五级身份瀑布用例（SR-SITE-005 §6.1/§6.2/§9；TDD 阶段①②）。

覆盖：邮箱归一化（gmail 去点 / +alias 剥离 / 非法输入）、三渠道适配器、
五级瀑布顺序（L1 external_id → L2 邮箱精确 → L3 归一化+alias → L4 公司名+国家模糊（阈值，
一律人工）→ L5 新建）、多命中→人工队列、同名不同国不参与 L4（误合并=0 防线）、
锚点跨渠道自动合并与层级超限转人工、同 (channel, external_id) 幂等重报。
"""
from __future__ import annotations

import pytest

from services.site.core.crm.identity import (
    CRMStore,
    IdentityResolver,
    LeadNormalizer,
    norm_email,
)
from services.site.core.crm.merge import CustomerMerger

from .fakes import MutableClock, T0


def make_world(auto_levels: int = 3, fuzzy_threshold: float = 0.85):
    """构造（归一化器, 解析器, 存储, 合并器, 时钟）。"""
    clock = MutableClock()
    store = CRMStore()
    resolver = IdentityResolver(store, auto_levels=auto_levels, fuzzy_threshold=fuzzy_threshold)
    merger = CustomerMerger(store, clock=clock.now)
    normalizer = LeadNormalizer(store, resolver, merger=merger, clock=clock.now)
    return normalizer, resolver, store, clock


ALI_RAW = {"buyer_login_id": "buyer_881", "email": "Bob@Corp.COM", "buyer_name": "Bob",
           "company": "Acme Trading", "country": "US", "conversation_uid": "cv-1"}
SITE_RAW = {"visitor_uid": "v-3f2a", "email": "bob@corp.com", "name": "Bob",
            "company": "Acme Trading", "country": "US", "form_id": "f-9"}
MAIL_RAW = {"from": "bob@corp.com", "from_name": "Bob", "company": "Acme Trading",
            "country": "US", "message_id": "<m-1@x>", "thread_customer_id": None}


class TestNormalization:
    """邮箱归一化与渠道适配（SR §9 用例表第 1 行）。"""

    @pytest.mark.parametrize("raw,expected", [
        ("John.A+promo@GMAIL.com", "johna@gmail.com"),   # gmail 类域：剥 alias + 去点
        ("john.a@gmail.com", "johna@gmail.com"),          # 仅去点
        ("bob+1@corp.com", "bob@corp.com"),               # 普通域：剥 alias 不去点
    ])
    def test_norm_email(self, raw: str, expected: str) -> None:
        assert norm_email(raw) == expected

    @pytest.mark.parametrize("raw", [None, "not-an-email"])
    def test_norm_email_invalid(self, raw: str | None) -> None:
        """非法输入 → None（异常族，不抛错）。"""
        assert norm_email(raw) is None

    def test_adapter_field_mapping(self) -> None:
        """三渠道适配器字段映射：alibaba/site/email 各自 raw 形态 → UnifiedLead（SR §6.1 ADAPTERS）。"""
        normalizer, _, _, _ = make_world()
        la = normalizer.adapt("alibaba", ALI_RAW)
        ls = normalizer.adapt("site", SITE_RAW)
        lm = normalizer.adapt("email", MAIL_RAW)
        assert (la.channel, la.external_id,
                la.raw_ref["conversation_uid"]) == ("alibaba", "buyer_881", "cv-1")
        assert (ls.channel, ls.external_id, ls.raw_ref["form_id"]) == ("site", "v-3f2a", "f-9")
        assert (lm.channel, lm.email) == ("email", "bob@corp.com")
        with pytest.raises(KeyError):
            normalizer.adapt("weixin", {})  # 未注册渠道（异常族）


class TestFiveLevelWaterfall:
    """五级识别瀑布（顺序与分流，SR §6.2）。"""

    def test_l1_external_id_attach(self) -> None:
        """L1：同渠道同 external_id 二次来 → attach level=1（阿里老买家二次询盘）。"""
        normalizer, _, store, _ = make_world()
        r1 = normalizer.ingest("alibaba", ALI_RAW)
        r2 = normalizer.ingest("alibaba", {**ALI_RAW, "conversation_uid": "cv-2"})
        assert r1.result.action == "create" and r1.result.level == 5
        assert r2.result.action == "attach" and r2.result.level == 1
        assert r2.lead.customer_id == r1.lead.customer_id

    def test_l2_email_exact_cross_channel(self) -> None:
        """L2：site 留资邮箱=已建档 email（小写精确）→ attach level=2，channels 追加新渠道。"""
        normalizer, _, store, _ = make_world()
        r1 = normalizer.ingest("email", MAIL_RAW)
        r2 = normalizer.ingest("site", SITE_RAW)
        assert r2.result.action == "attach" and r2.result.level == 2
        cust = store.customers[r2.lead.customer_id]
        assert {c["channel"] for c in cust.channels} == {"email", "site"}

    def test_l3_norm_alias_variant(self) -> None:
        """L3：+alias 变体归一化后唯一命中 → attach level=3。"""
        normalizer, _, _, _ = make_world()
        normalizer.ingest("site", SITE_RAW)  # bob@corp.com
        r2 = normalizer.ingest("email", {**MAIL_RAW, "from": "bob+news@corp.com"})
        assert r2.result.action == "attach" and r2.result.level == 3

    def test_l3_multi_hit_goes_manual(self) -> None:
        """L3 命中 ≥2 客户 → 不判定，进人工队列且候选全保留（误并防护，SR §2.2）。"""
        normalizer, _, store, _ = make_world()
        normalizer.ingest("site", {**SITE_RAW, "visitor_uid": "v-1", "email": "bob@corp.com"})
        normalizer.ingest("site", {**SITE_RAW, "visitor_uid": "v-2", "email": "bob@corp.com"})  # attach 同档
        # 第二个独立档案（无邮箱建档）经身份表注入归一化相同的邮箱（模拟历史脏数据）
        r_company = normalizer.ingest("alibaba", {**ALI_RAW, "buyer_login_id": "b2",
                                                  "email": None, "company": None})
        assert r_company.result.action == "create"
        cust2 = store.customers[r_company.lead.customer_id]
        store.ensure_identity(cust2.customer_id, "email", "bob+x@corp.com", email="bob+x@corp.com")
        r3 = normalizer.ingest("email", {**MAIL_RAW, "from": "bob+y@corp.com"})
        assert r3.result.action == "manual"
        assert len(r3.result.candidates) >= 2
        assert r3.lead.status == "pending_match" and r3.lead.customer_id is None

    def test_l4_fuzzy_generates_candidates_only(self) -> None:
        """L4：公司名相似（Acme Trading LLC vs Acme Trading）且国家一致 → 仅生成候选，绝不自动合并。"""
        normalizer, _, store, _ = make_world()
        r1 = normalizer.ingest("alibaba", ALI_RAW)
        r2 = normalizer.ingest("site", {**SITE_RAW, "email": None, "company": "Acme Trading LLC"})
        assert r2.result.action == "manual"
        assert r2.result.level == 4
        assert [c.customer_id for c in r2.result.candidates] == [r1.lead.customer_id]
        assert r2.lead.customer_id is None  # 未归属

    def test_l4_threshold_boundary(self) -> None:
        """L4 阈值 0.85 两侧：低相似公司名不参与该级 → L5 新建（边界）。"""
        normalizer, resolver, _, _ = make_world(fuzzy_threshold=0.85)
        normalizer.ingest("alibaba", ALI_RAW)
        r = normalizer.ingest("site", {"visitor_uid": "v-9", "email": None,
                                       "company": "Zebra Steel Co", "country": "US", "form_id": "f-8"})
        assert r.result.action == "create"

    def test_l4_country_mismatch_not_matched(self) -> None:
        """同名不同国：不参与 L4（同名不同国不合并，误并=0 红线）→ 新建。"""
        normalizer, _, _, _ = make_world()
        normalizer.ingest("alibaba", ALI_RAW)
        r = normalizer.ingest("site", {"visitor_uid": "v-8", "email": None,
                                       "company": "Acme Trading", "country": "DE", "form_id": "f-7"})
        assert r.result.action == "create"

class TestAnchorRouting:
    """渠道锚点路由：跨渠道合并（SR §6.2 _route）。"""

    def test_anchor_cross_channel_auto_merge(self) -> None:
        """邮件回复（线程锚点=客户 A）+ 归一化邮箱唯一命中客户 B（L3）→ 自动合并 A→B。"""
        from .test_crm_merge import first_merge_task
        normalizer, _, store, _ = make_world()
        # 锚点客户 A（公司名与 B 差异化，避免 L4 模糊命中干扰建档）
        r_alibaba = normalizer.ingest("alibaba", {**ALI_RAW, "email": "anchor@corp.com",
                                                  "company": "Wayne Imports"})
        normalizer.ingest("site", SITE_RAW)  # 建档 B（bob@corp.com）
        anchor = r_alibaba.lead.customer_id
        target = store.leads[("site", "v-3f2a")].customer_id
        # +alias 变体：L2 精确不中、L3 归一化唯一命中 → 跨渠道自动合并（level 3）
        r = normalizer.ingest("email", {**MAIL_RAW, "from": "bob+news@corp.com",
                                        "thread_customer_id": anchor})
        assert r.result.action == "merge" and r.result.level == 3
        merged = store.customers[anchor]
        assert merged.merged_into == target
        assert first_merge_task(store).trigger_by == "auto"

    def test_anchor_beyond_auto_level_goes_manual(self) -> None:
        """auto_levels=2：L3 命中超出自动合并上限 → 人工队列（误并防护②）。"""
        normalizer, _, _, _ = make_world(auto_levels=2)
        r_alibaba = normalizer.ingest("alibaba", {**ALI_RAW, "email": "anchor@corp.com",
                                                  "company": "Wayne Imports"})
        normalizer.ingest("site", SITE_RAW)
        r = normalizer.ingest("email", {**MAIL_RAW, "from": "bob+news@corp.com",
                                        "thread_customer_id": r_alibaba.lead.customer_id})
        assert r.result.action == "manual" and r.result.level == 3

    def test_lead_reingest_idempotent(self) -> None:
        """同 (channel, external_id) 重复上报：不新建 lead/customer，仅刷新属性（幂等键 UNIQUE）。"""
        normalizer, _, store, _ = make_world()
        r1 = normalizer.ingest("site", SITE_RAW)
        r2 = normalizer.ingest("site", {**SITE_RAW, "company": "Acme Trading GmbH"})
        assert r2.lead.lead_id == r1.lead.lead_id
        assert len(store.customers) == 1
        assert store.leads[("site", "v-3f2a")].company == "Acme Trading GmbH"
