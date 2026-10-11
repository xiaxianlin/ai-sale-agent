# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-005 CRM 集成·线索归一化 + 五级身份识别瀑布（SR-SITE-005 §6.1/§6.2，Phase 3 第二批）。

模块职责：
- :func:`norm_email`——邮箱归一化：小写 + 剥 +alias；Gmail 类域再去点
  （John.A+ali@GMAIL.com → johna@gmail.com）；
- :class:`UnifiedLead` + ``ADAPTERS``——三渠道适配器（阿里会话/独立站留资/邮件回复），
  各自把原始载体翻译成统一线索结构；
- :class:`CRMStore`——线索/客户/身份/会话/合并台账的内存形态（CustomerStore 协议的
  demo 实现，数据形态对齐 kb/customer：display_name/channels/deal_records/merged_into，
  不 import kb，生产替换 PG 形态、协议不变）；
- :class:`IdentityResolver`——五级瀑布（保守优先）：L1 external_id 精确 → L2 邮箱精确
  → L3 归一化(+alias 剥离) → L4 公司名+国家模糊（阈值，一律只出候选进人工队列）
  → L5 新建；任一层命中 ≥2 客户 → 不判定直接人工（误合并=0 防线，SRS §16.4）；
- :class:`LeadNormalizer`——统一入口：归一化 → 幂等落库 → 识别 → attach/create/
  merge(锚点跨渠道高置信自动)/manual 四分流。

依据文档：docs/07-实现方案/SITE/SR-SITE-005-CRM集成.md §6.1/§6.2/§9。
"""
from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

#: 合法渠道（crm_lead.channel CHECK 口径）
CHANNELS = ("alibaba", "site", "email")

#: Gmail 类域（归一化时额外去点）
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def norm_email(email: str | None) -> str | None:
    """邮箱归一化：小写 + 剥 +alias；Gmail 类域再去点；非法输入返回 None。"""
    if not email or "@" not in email:
        return None
    local, _, domain = email.strip().lower().partition("@")
    local = local.split("+", 1)[0]  # +alias 剥离（全域名统一）
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def company_similarity(a: str, b: str) -> float:
    """公司名相似度（0~1；demo 用 difflib 比率，生产对齐 pg_trgm similarity 口径）。"""
    a_norm, b_norm = a.strip().lower(), b.strip().lower()
    if not a_norm or not b_norm:
        return 0.0
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio()


# --------------------------------------------------------------------------- 统一线索


@dataclass(frozen=True)
class UnifiedLead:
    """统一线索结构：渠道身份 + 可识别属性（邮箱/公司名/姓名/国家）。"""

    channel: Literal["alibaba", "site", "email"]
    external_id: str  # 渠道内唯一：阿里买家 login_id / 独立站 visitor_uid / 邮箱地址(小写)
    email: str | None = None
    name: str | None = None
    company: str | None = None
    country: str | None = None
    linked_customer_id: int | None = None  # 渠道锚点（邮件线程反查的原线索客户）
    raw_ref: dict = field(default_factory=dict)


def _adapt_alibaba(raw: dict) -> UnifiedLead:
    """阿里国际站询盘会话（FR-AG-002）→ UnifiedLead。"""
    return UnifiedLead("alibaba", raw["buyer_login_id"], email=raw.get("email"),
                       name=raw.get("buyer_name"), company=raw.get("company"),
                       country=raw.get("country"),
                       raw_ref={"conversation_uid": raw.get("conversation_uid")})


def _adapt_site(raw: dict) -> UnifiedLead:
    """独立站留资（FR-SITE-003）→ UnifiedLead。"""
    return UnifiedLead("site", raw["visitor_uid"], email=raw.get("email"), name=raw.get("name"),
                       company=raw.get("company"), country=raw.get("country"),
                       raw_ref={"form_id": raw.get("form_id")})


def _adapt_email(raw: dict) -> UnifiedLead:
    """邮件回复（FR-SITE-004 IMAP 识别）→ UnifiedLead（from 即 external_id）。"""
    return UnifiedLead("email", raw["from"].lower(), email=raw["from"].lower(),
                       name=raw.get("from_name"), company=raw.get("company"),
                       country=raw.get("country"),
                       linked_customer_id=raw.get("thread_customer_id"),
                       raw_ref={"message_id": raw.get("message_id")})


#: 渠道适配器注册表（未注册渠道 KeyError → API 层 40001）
ADAPTERS = {"alibaba": _adapt_alibaba, "site": _adapt_site, "email": _adapt_email}


# --------------------------------------------------------------------------- 存储形态


@dataclass
class Customer:
    """客户档案（kb/customer 共库形态：display_name/channels/merged_into/deal_records，零迁移）。"""

    customer_id: int
    display_name: str
    company: str | None = None
    country: str | None = None
    channels: list[dict] = field(default_factory=list)
    merged_into: int | None = None
    deal_records: list[dict] = field(default_factory=list)
    created_at: datetime | None = None


@dataclass
class IdentityRow:
    """客户身份索引（customer_identity 形态；L1-L3 精确匹配统一入口）。"""

    identity_id: int
    customer_id: int
    channel: str
    external_id: str
    email: str | None = None
    email_norm: str | None = None


@dataclass
class LeadRow:
    """统一线索台账行（crm_lead 形态；UNIQUE (channel, external_id) 幂等键）。"""

    lead_id: int
    channel: str
    external_id: str
    email: str | None = None
    email_norm: str | None = None
    name: str | None = None
    company: str | None = None
    country: str | None = None
    linked_customer_id: int | None = None
    raw_ref: dict = field(default_factory=dict)
    customer_id: int | None = None
    match_level: int | None = None  # 1-4 命中层；5=新建；None=待人工
    status: str = "resolved"  # resolved / pending_match / discarded
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ConversationRow:
    """会话行（conversation 形态；合并时物理重挂 customer_id）。"""

    conversation_id: int
    customer_id: int
    channel: str
    created_at: datetime | None = None
    summary: str = ""


class CRMStore:
    """CRM 内存存储（CustomerStore 协议 demo 实现；生产替换 PG 形态、协议不变）。"""

    def __init__(self) -> None:
        self.customers: dict[int, Customer] = {}
        self.identities: list[IdentityRow] = []
        self.leads: dict[tuple[str, str], LeadRow] = {}
        self.conversations: list[ConversationRow] = []
        self.merge_tasks: dict[int, Any] = {}  # MergeTask（merge.py 形态，避免环 import）
        self._cid = 0
        self._lid = 0
        self._iid = 0
        self._convid = 0

    # —— 客户 ——
    def add_customer(self, display_name: str, *, company: str | None = None,
                     country: str | None = None, channels: list[dict] | None = None,
                     created_at: datetime | None = None) -> Customer:
        """新建客户档案。"""
        self._cid += 1
        cust = Customer(customer_id=self._cid, display_name=display_name, company=company,
                        country=country, channels=list(channels or []), created_at=created_at)
        self.customers[cust.customer_id] = cust
        return cust

    def add_channel(self, customer_id: int, channel: str, external_id: str) -> None:
        """channels 追加（去重；保留全部渠道历史）。"""
        cust = self.customers[customer_id]
        if not any(c["channel"] == channel and c["external_id"] == external_id for c in cust.channels):
            cust.channels.append({"channel": channel, "external_id": external_id})

    # —— 身份 ——
    def ensure_identity(self, customer_id: int, channel: str, external_id: str,
                        email: str | None = None) -> IdentityRow:
        """身份确保（幂等）：L1 命中的依据；email 存小写原文 + 归一化。"""
        for row in self.identities:
            if row.channel == channel and row.external_id == external_id:
                return row
        self._iid += 1
        row = IdentityRow(identity_id=self._iid, customer_id=customer_id, channel=channel,
                          external_id=external_id,
                          email=email.lower() if email else None,
                          email_norm=norm_email(email))
        self.identities.append(row)
        return row

    # —— 线索 ——
    def upsert_lead(self, lead: UnifiedLead, email_norm: str | None,
                    now: datetime | None = None) -> LeadRow:
        """线索幂等落库：同 (channel, external_id) 只更新属性不新建（UNIQUE 语义）。"""
        key = (lead.channel, lead.external_id)
        row = self.leads.get(key)
        if row is None:
            self._lid += 1
            row = LeadRow(lead_id=self._lid, channel=lead.channel, external_id=lead.external_id,
                          created_at=now)
            self.leads[key] = row
        row.email, row.email_norm = lead.email, email_norm
        row.name, row.company, row.country = lead.name, lead.company, lead.country
        row.linked_customer_id = lead.linked_customer_id
        row.raw_ref = dict(lead.raw_ref)
        row.updated_at = now
        return row

    # —— 会话 ——
    def add_conversation(self, *, customer_id: int, channel: str,
                         created_at: datetime | None = None, summary: str = "") -> ConversationRow:
        """新增会话行（360 时间线/会话量数据源）。"""
        self._convid += 1
        conv = ConversationRow(conversation_id=self._convid, customer_id=customer_id,
                               channel=channel, created_at=created_at, summary=summary)
        self.conversations.append(conv)
        return conv

    # —— 合并链 ——
    def find_root(self, customer_id: int) -> int:
        """顺 merged_into 链找根（防并入已合并档；访问集防环）。"""
        seen: set[int] = set()
        current = customer_id
        while True:
            merged = self.customers[current].merged_into
            if merged is None:
                return current
            if current in seen:
                raise ValueError(f"merged_into 链成环：{customer_id}")
            seen.add(current)
            current = merged

    def chain_ids(self, root_id: int) -> list[int]:
        """根 + 全部 merged_into 指向根（含传递）的档案 id（360 装配口径）。"""
        ids = [root_id]
        changed = True
        while changed:
            changed = False
            for cust in self.customers.values():
                if cust.merged_into in ids and cust.customer_id not in ids:
                    ids.append(cust.customer_id)
                    changed = True
        return ids

    def emails_of(self, customer_ids: list[int]) -> set[str]:
        """身份表中的小写邮箱（mail_job 运行时关联键）。"""
        return {row.email for row in self.identities
                if row.customer_id in customer_ids and row.email}

    def snapshot_state(self) -> dict:
        """全量状态快照（dry_run/undo 一致性断言用；仅含可比较原语）。"""
        return {
            "customers": {cid: {"display_name": c.display_name, "company": c.company,
                                 "country": c.country, "merged_into": c.merged_into,
                                 "channels": [dict(ch) for ch in c.channels],
                                 "deals": [dict(d) for d in c.deal_records]}
                          for cid, c in self.customers.items()},
            "identities": sorted((i.identity_id, i.customer_id, i.channel, i.external_id,
                                  i.email, i.email_norm) for i in self.identities),
            "conversations": sorted((c.conversation_id, c.customer_id, c.channel)
                                    for c in self.conversations),
            "leads": {k: (v.customer_id, v.match_level, v.status, v.company) for k, v in self.leads.items()},
        }


# --------------------------------------------------------------------------- 识别瀑布


@dataclass(frozen=True)
class MatchHit:
    """单层命中：客户 + 层级 + 分值。"""

    customer_id: int
    level: int
    score: float


@dataclass(frozen=True)
class ResolveResult:
    """识别分流结果：attach/create/merge/manual 四流。"""

    action: Literal["attach", "create", "merge", "manual"]
    hit: MatchHit | None = None
    candidates: list[MatchHit] = field(default_factory=list)
    level: int | None = None


class IdentityResolver:
    """跨渠道身份识别引擎：五级瀑布，保守优先（误合并=0 红线）。"""

    def __init__(self, store: CRMStore, *, auto_levels: int = 3, fuzzy_threshold: float = 0.85) -> None:
        self.store = store
        self.auto_levels = auto_levels  # ≤该层级且单命中才允许自动合并（CRM_MATCH_AUTO_LEVELS）
        self.fuzzy_threshold = fuzzy_threshold  # L4 公司名相似度阈值（CRM_FUZZY_THRESHOLD）

    def resolve(self, lead: LeadRow, email_norm: str | None = None) -> ResolveResult:
        """五级瀑布主流程（SR §6.2 resolve）。"""
        # L1 渠道外部 ID 精确（同渠道再来）
        hit = self._by_external(lead.channel, lead.external_id)
        if hit is not None:
            return self._route(lead, hit)
        if lead.email and lead.email.lower():
            # L2 邮箱精确（小写等值）
            result = self._by_email_field(lambda r: r.email, lead.email.lower(), 2, 0.95)
            if result is not None:
                return self._routed_or_manual(lead, result)
            # L3 归一化（+alias 剥离后才相等）
            norm = email_norm or norm_email(lead.email)
            if norm:
                result = self._by_email_field(lambda r: r.email_norm, norm, 3, 0.90)
                if result is not None:
                    return self._routed_or_manual(lead, result)
        # L4 公司名+国家双因素模糊：命中一律只出候选进人工队列（绝不自动）
        if lead.company and lead.country:
            candidates = self._fuzzy_company_country(lead.company, lead.country)
            if candidates:
                return ResolveResult("manual", candidates=candidates, level=4)
        return ResolveResult("create", level=5)  # L5 全未命中 → 新建

    # —— 各级查询 ——
    def _distinct_customers(self, rows) -> list[int]:
        """身份行 → 去重客户 id（保留序）。"""
        out: list[int] = []
        for r in rows:
            if r.customer_id not in out:
                out.append(r.customer_id)
        return out

    def _by_external(self, channel: str, external_id: str) -> MatchHit | None:
        """L1：同渠道同 external_id 单命中。"""
        ids = self._distinct_customers(r for r in self.store.identities
                                       if r.channel == channel and r.external_id == external_id)
        if len(ids) == 1:
            return MatchHit(ids[0], 1, 1.0)
        return None  # 0 或多命中：多命中交由上层人工兜底（L1 多命中属脏数据，按 L5 新建保守处理）

    def _by_email_field(self, field_getter, value: str, level: int, score: float) -> ResolveResult | None:
        """L2/L3：按邮箱字段等值查询；单命中 attach 流，多命中 manual（候选全保留）。"""
        ids = self._distinct_customers(r for r in self.store.identities if field_getter(r) == value)
        if len(ids) == 1:
            return ResolveResult("attach", hit=MatchHit(ids[0], level, score), level=level)
        if len(ids) > 1:
            return ResolveResult("manual",
                                 candidates=[MatchHit(cid, level, score) for cid in ids], level=level)
        return None

    def _fuzzy_company_country(self, company: str, country: str) -> list[MatchHit]:
        """L4 双因素：公司名相似度 ≥ 阈值 且 国家一致；任一缺失不参与该级（同名不同国不合并）。"""
        out: list[MatchHit] = []
        for cust in self.store.customers.values():
            if cust.merged_into is not None or not cust.company or not cust.country:
                continue
            if cust.country != country:
                continue
            ratio = company_similarity(company, cust.company)
            if ratio >= self.fuzzy_threshold:
                out.append(MatchHit(cust.customer_id, 4, round(ratio, 3)))
        return out

    # —— 分流 ——
    def _route(self, lead: LeadRow, hit: MatchHit) -> ResolveResult:
        """单命中分流：无锚点 attach；锚点≠命中 → 跨渠道合并（自动/人工按层级）。"""
        if lead.linked_customer_id and lead.linked_customer_id != hit.customer_id:
            if hit.level <= self.auto_levels:  # 高置信自动合并（误并防护①）
                return ResolveResult("merge", hit=hit, level=hit.level)
            return ResolveResult("manual", candidates=[hit], level=hit.level)  # 误并防护②
        return ResolveResult("attach", hit=hit, level=hit.level)

    def _routed_or_manual(self, lead: LeadRow, result: ResolveResult) -> ResolveResult:
        """L2/L3 结果过锚点分流；manual 原样透传（一对多 → 人工队列）。"""
        if result.action == "attach" and result.hit is not None:
            return self._route(lead, result.hit)
        return result


@dataclass(frozen=True)
class LeadResult:
    """线索受理结果：台账行 + 分流结果。"""

    lead: LeadRow
    result: ResolveResult


class LeadNormalizer:
    """线索统一入口：渠道适配 → 归一化 → 幂等落库 → 识别 → 四分流。"""

    def __init__(self, store: CRMStore, resolver: IdentityResolver, *,
                 merger: Any | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.resolver = resolver
        self.merger = merger  # CustomerMerger（仅 TYPE_CHECKING 依赖，避免环 import）
        self.clock = clock or (lambda: datetime.now().astimezone())

    def adapt(self, channel: str, raw: dict) -> UnifiedLead:
        """渠道适配（未注册渠道 KeyError，API 层转 40001）。"""
        return ADAPTERS[channel](raw)

    def ingest(self, channel: str, raw: dict) -> LeadResult:
        """归一化入口（SR §6.1 ingest）：幂等落库 → 五级瀑布 → 归属/合并/人工分流。"""
        now = self.clock()
        lead = self.adapt(channel, raw)
        email_norm = norm_email(lead.email)
        row = self.store.upsert_lead(lead, email_norm, now=now)
        res = self.resolver.resolve(row, email_norm)
        if res.action == "create":  # L5：新建 Customer + 身份 + 渠道
            cust = self.store.add_customer(
                lead.name or lead.company or lead.external_id[:64],
                company=lead.company, country=lead.country,
                channels=[{"channel": channel, "external_id": lead.external_id}], created_at=now)
            row.customer_id, row.match_level, row.status = cust.customer_id, 5, "resolved"
            self.store.ensure_identity(cust.customer_id, channel, lead.external_id, email=lead.email)
        elif res.action == "attach":  # 命中已有客户：绑定线索 + 追加渠道身份与 channels
            assert res.hit is not None
            row.customer_id, row.match_level, row.status = res.hit.customer_id, res.hit.level, "resolved"
            self.store.ensure_identity(res.hit.customer_id, channel, lead.external_id, email=lead.email)
            self.store.add_channel(res.hit.customer_id, channel, lead.external_id)
        elif res.action == "merge":  # 高置信跨渠道：锚点客户并入命中客户（SR §6.3）
            assert res.hit is not None and lead.linked_customer_id is not None
            row.match_level, row.status = res.hit.level, "resolved"
            if self.merger is not None:
                self.merger.execute(lead.linked_customer_id, res.hit.customer_id,
                                    trigger_by="auto", level=res.hit.level, score=res.hit.score)
        else:  # manual：不确定 → 人工确认队列（候选全保留）
            row.status, row.match_level, row.customer_id = "pending_match", res.level, None
            if self.merger is not None:
                self.merger.create_pending_task(row, res.candidates, trigger_by="auto")
        return LeadResult(row, res)
