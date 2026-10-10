# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""六大知识库类型枚举与 metadata 内容模型（FR-KB-001~006，迭代 0.3）。

本文件是六大知识库「内容模型」的单一事实源（存 knowledge_item.metadata JSONB，
03-数据库设计 §3.1：metadata 随 kb_type 自适应，FR-KB-020）：

| kb_type        | 需求    | metadata 模型      | 方案依据（内容模型/维度设计节）                    |
|----------------|---------|--------------------|----------------------------------------------------|
| product        | FR-KB-001 | ProductMeta      | SR-KB-001 §4/§6.1（参数/SKU/价格/认证/竞品）        |
| customer       | FR-KB-002 | CustomerMeta     | SR-KB-002 §6.1（画像标签/分级/PII 脱敏边界）        |
| skill          | FR-KB-003 | SkillMeta        | SR-KB-003 §6.1（场景标签/变量槽位）                 |
| process        | FR-KB-004 | ProcessMeta      | SR-KB-004 §4（节点链/红线条目结构化字段）           |
| negative_case  | FR-KB-005 | NegativeCaseMeta | SR-KB-005 §6.1/§6.2（reason_type 分类树/对照对）    |
| training       | FR-KB-006 | TrainingMeta     | SR-KB-006 §4/§6（课程包/题型/能力维度/场景定义）     |

设计约定：
- 每个 metadata 模型携带 Literal 判别字段 ``kb_type``，六个模型组成 discriminated
  union（见本文件 ``KbMetadata``），供 item.KnowledgeItemCreate 按库自动校验；
- 全部模型 extra="forbid"（fail-closed：未知字段一律拒绝，收敛脏数据入口）；
- 会进入向量库标量列（VectorRecord 五个过滤维度，VarChar(64)）的字段在建模层
  即约束长度上限 FILTER_VALUE_MAX_LENGTH，超长在入库前拦截；
- 价格体系（含底线价）仅 PG 存储不入向量正文（SR-KB-001 §2 决策 2 / R-03），
  「仅授权可见」以 PriceFloor.authorized_only 标记字段显式表达；
- 客户库明文 PII 永不入 metadata（NFR-SEC-01，SR-KB-002 §2 D3）：以
  PII_FORBIDDEN_METADATA_KEYS 禁入清单 + before 校验器给出字段级中文错误。

只读引用（不改其行为）：services/kb/core/vectorstore/base.py 的
FILTER_VALUE_MAX_LENGTH（标量维度 VarChar(64) 上限，03-数据库设计 §4.2）。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.kb.core.vectorstore.base import FILTER_VALUE_MAX_LENGTH

# ── 六大知识库枚举（取值与 vectorstore.base.KB_TYPES / 03-数据库设计 §3.1 CHECK 一致）──


class KBType(StrEnum):
    """六大知识库类型（FR-KB-001~006），取值即 Milvus 分区名与 PG kb_type 枚举。"""

    PRODUCT = "product"            # 产品知识库（FR-KB-001）
    CUSTOMER = "customer"          # 客户知识库（FR-KB-002）
    SKILL = "skill"                # 销售技能库（FR-KB-003）
    PROCESS = "process"            # 流程知识库（FR-KB-004）
    NEGATIVE_CASE = "negative_case"  # 负面案例库（FR-KB-005）
    TRAINING = "training"          # 新人带教素材库（FR-KB-006）


# ============================================================================
# 产品知识库（FR-KB-001，SR-KB-001 §4 字段表 / §6.1 内容模型）
# ============================================================================

#: SKU 格式（SR-KB-001 §7 配置 kb.product.sku_pattern 的代码内同规则，3-32 位字母数字连字符）
SKU_PATTERN: str = r"^[A-Za-z0-9\-]{3,32}$"
#: 价格币种白名单（SR-KB-001 §7 kb.product.price.currencies，ISO 4217）
PRICE_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "CNY")


class PriceTier(BaseModel):
    """价格阶梯档：MOQ 起订量 + 该档单价。"""

    model_config = ConfigDict(extra="forbid")

    moq: int = Field(gt=0, description="该档起订量")
    unit_price: Decimal = Field(gt=0, description="该档单价（正数，币种见 PriceBook.currency）")


class PriceFloor(BaseModel):
    """价格底线（SR-KB-001 §8：价格体系属商业秘密，底线价仅授权角色可见）。

    ``authorized_only`` 为固定 True 的标记字段：任何读取方据此判定该值只可
    出现在授权链路（get_product_card / 后台 RBAC 价格列），永不进入向量正文。
    """

    model_config = ConfigDict(extra="forbid")

    value: Decimal = Field(gt=0, description="底线单价（授权可见，不入向量正文）")
    authorized_only: Literal[True] = True  # 标记字段：仅授权可见（默认且唯一取值）


class PriceBook(BaseModel):
    """价格体系：币种 + MOQ 阶梯 + 有效期 + 底线价（仅 PG 存储，不重建向量）。"""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USD", description="ISO 4217 币种")
    tiers: list[PriceTier] = Field(min_length=1, description="MOQ 阶梯，至少一档")
    valid_until: date | None = Field(default=None, description="价格有效期")
    floor_price: PriceFloor | None = Field(default=None, description="底线价（仅授权可见）")

    @field_validator("currency")
    @classmethod
    def _currency_in_whitelist(cls, value: str) -> str:
        """币种必须在白名单内（SR-KB-001 §7）。"""
        upper = value.upper()
        if upper not in PRICE_CURRENCIES:
            raise ValueError(f"币种必须为 {PRICE_CURRENCIES} 之一，收到：{value!r}")
        return upper

    @model_validator(mode="after")
    def _price_ladder_non_increasing(self) -> "PriceBook":
        """阶梯价规则：MOQ 递增时单价必须非递增（外贸阶梯价惯例，违例拒绝入库）。"""
        tiers = sorted(self.tiers, key=lambda t: t.moq)
        for lower, upper in zip(tiers, tiers[1:]):
            if upper.unit_price > lower.unit_price:
                raise ValueError(
                    f"价格阶梯异常：MOQ {upper.moq} 档单价高于 MOQ {lower.moq} 档"
                )
        return self


class SellingPoint(BaseModel):
    """卖点话术：标签 + 话术 + 适用场景（逐条分块，块级打 scene 标量）。"""

    model_config = ConfigDict(extra="forbid")

    tag: str = Field(min_length=1, max_length=64)
    script: str = Field(min_length=1, description="卖点话术正文")
    scenes: list[str] = Field(default_factory=list, description="该卖点适用场景标签")


class Cert(BaseModel):
    """认证证书：名称/编号/签发方/有效期；入库时自动标记过期（不阻断）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    cert_no: str = Field(default="", max_length=128, description="证书编号（真实编号占位符化）")
    issuer: str = Field(default="", max_length=128)
    valid_until: date | None = None
    expired: bool = False  # 入库时计算：valid_until 早于当日即 True（检索层对过期块降权）

    @model_validator(mode="after")
    def _mark_expired(self) -> "Cert":
        """过期标记：valid_until < 今日 → expired=True（SR-KB-001 §6.1 校验钩子 ③）。"""
        if self.valid_until is not None and self.valid_until < date.today():
            self.expired = True
        return self


class Competitor(BaseModel):
    """竞品对比条目：竞品方 + 竞品型号 + 对比维度键值（按竞品独立成块）。"""

    model_config = ConfigDict(extra="forbid")

    competitor: str = Field(min_length=1, max_length=FILTER_VALUE_MAX_LENGTH)
    model: str = Field(default="", max_length=64)
    dims: dict[str, str] = Field(default_factory=dict, description="对比维度，如 {价格: 高 15%}")


class ProductMeta(BaseModel):
    """产品条目 metadata 内容模型（kb_type='product'，SR-KB-001 §4 字段表）。"""

    model_config = ConfigDict(extra="forbid")

    kb_type: Literal[KBType.PRODUCT] = KBType.PRODUCT  # discriminated union 判别字段

    category_path: list[str] = Field(min_length=1, description="品类路径，如 [AV支架, 电视挂架]")
    sku: str = Field(description="主 SKU（入库统一大写归一化）")
    sku_aliases: list[str] = Field(default_factory=list, description="SKU 别名（查询改写归一化用）")
    params: dict[str, str] = Field(default_factory=dict, description="产品参数键值对（参数表整块入 chunk）")
    price: PriceBook | None = Field(default=None, description="价格体系（仅 PG 存储，不入向量正文）")
    selling_points: list[SellingPoint] = Field(default_factory=list)
    scenes: list[str] = Field(default_factory=list, description="应用场景标签（条目级）")
    certs: list[Cert] = Field(default_factory=list)
    competitors: list[Competitor] = Field(default_factory=list)
    moq: int = Field(default=1, gt=0)
    lead_time_days: int = Field(default=30, gt=0)
    hs_code: str = Field(default="", max_length=16)

    @field_validator("sku")
    @classmethod
    def _check_sku(cls, value: str) -> str:
        """SKU 格式校验 + 统一大写归一化（SR-KB-001 §6.1）。"""
        if not re.fullmatch(SKU_PATTERN, value):
            raise ValueError(f"SKU 格式非法（3-32 位字母数字连字符），收到：{value!r}")
        return value.upper()

    @field_validator("category_path")
    @classmethod
    def _check_category_path(cls, value: list[str]) -> list[str]:
        """品类路径每级非空且 ≤64 字符（末级品类映射为检索维度，长度须落在标量上限内）。"""
        for seg in value:
            if not seg or not seg.strip():
                raise ValueError("品类路径各级不能为空")
            if len(seg) > FILTER_VALUE_MAX_LENGTH:
                raise ValueError(f"品类路径单级超长（上限 {FILTER_VALUE_MAX_LENGTH} 字符）：{seg[:32]!r}…")
        return value

    @model_validator(mode="after")
    def _normalize_aliases(self) -> "ProductMeta":
        """SKU 别名归一化：统一大写、去空、与主 SKU 去重（保证别名表干净，SR-KB-001 §6.3）。"""
        self.sku_aliases = [
            alias for alias in dict.fromkeys(a.upper() for a in self.sku_aliases if a.strip())
            if alias != self.sku
        ]
        return self


# ============================================================================
# 客户知识库（FR-KB-002，SR-KB-002 §6.1 画像标签模型 / §2 D3 PII 脱敏边界）
# ============================================================================

#: 客户知识条目四类内容（SR-KB-002 §6.1 CustomerKBCategory）
CUSTOMER_CATEGORIES: tuple[str, ...] = ("profile", "comm_history", "win_loss_review", "grade_standard")
#: 客户分级枚举（03-数据库设计 §3.2 customer.grade，口径 OI-06 待确认）
CUSTOMER_GRADES: tuple[str, ...] = ("A", "B", "C", "D")
#: 明文 PII 禁入清单：这些键出现在客户 metadata 任何层级均为违规（NFR-SEC-01）。
#: 语义：邮箱/电话/WhatsApp/微信等联系方式、全名、证件号、支付/银行账号、详细地址、生日。
PII_FORBIDDEN_METADATA_KEYS: frozenset[str] = frozenset({
    "email", "e_mail", "phone", "mobile", "tel", "whatsapp", "wechat", "contact",
    "full_name", "name", "contact_name", "id_number", "passport_no",
    "payment_account", "bank_account", "card_no",
    "address", "address_detail", "street", "birth", "birthday",
})
#: HMAC-SHA256 指纹格式（64 位十六进制，SR-KB-002 §6.2 pii_fingerprint）
_PII_DIGEST_PATTERN: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


def assert_no_pii_keys(payload: Mapping[str, object]) -> None:
    """校验 payload 的顶层键不命中明文 PII 禁入清单，命中即抛中文错误。

    供 CustomerMeta 入库前校验与批量导入（FR-KB-021）行预处理共用，
    保证「明文 PII 永不进入知识元数据」在建模层可断言。
    """
    hits = sorted(set(payload) & PII_FORBIDDEN_METADATA_KEYS)
    if hits:
        raise ValueError(
            f"客户 metadata 禁入明文 PII 字段（NFR-SEC-01）：{hits}；"
            "联系方式请以 pii_digests 指纹等值检索，地址仅保留国家/省层级"
        )


class CustomerProfileTags(BaseModel):
    """画像标签：脱敏后的结构化检索锚点（不含任何明文 PII，SR-KB-002 §2 D3②）。"""

    model_config = ConfigDict(extra="forbid")

    country: str = Field(default="", max_length=64, description="国家（地址仅保留国家/省层级）")
    industry: str = Field(default="", max_length=64, description="行业")
    customer_type: str = Field(default="", max_length=64, description="如：经销商/商超/电商卖家")
    purchase_scale: str = Field(default="", max_length=64, description="如：月采购 5k+ 件")
    risk_flags: list[str] = Field(default_factory=list, description="风险标记，如：长期拖账")


class CustomerMeta(BaseModel):
    """客户条目 metadata 内容模型（kb_type='customer'，SR-KB-002 §6.1）。"""

    model_config = ConfigDict(extra="forbid")

    kb_type: Literal[KBType.CUSTOMER] = KBType.CUSTOMER

    customer_ref: str | None = Field(
        default=None,
        max_length=FILTER_VALUE_MAX_LENGTH,
        description="关联 customer.uid（分级标准类为空）",
    )
    category: Literal["profile", "comm_history", "win_loss_review", "grade_standard"]
    grade: str | None = Field(default=None, description="条目形成时的客户分级快照（A/B/C/D）")
    tags: CustomerProfileTags = Field(default_factory=CustomerProfileTags)
    deal_snapshot: dict[str, str] = Field(
        default_factory=dict, description="成单/丢单摘要 {outcome, amount_band, at}"
    )
    pii_digests: list[str] = Field(
        default_factory=list, description="HMAC-SHA256 指纹，供密文等值检索（非明文 PII）"
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_pii_keys(cls, data: object) -> object:
        """PII 禁入清单前置校验：给出字段级中文错误（早于 extra=forbid 的通用报错）。"""
        if isinstance(data, Mapping):
            assert_no_pii_keys(data)
        return data

    @field_validator("grade")
    @classmethod
    def _check_grade(cls, value: str | None) -> str | None:
        """分级快照必须命中 A/B/C/D（03-数据库设计 §3.2）。"""
        if value is not None and value not in CUSTOMER_GRADES:
            raise ValueError(f"客户分级必须为 {CUSTOMER_GRADES} 之一，收到：{value!r}")
        return value

    @field_validator("pii_digests")
    @classmethod
    def _check_digest_format(cls, value: list[str]) -> list[str]:
        """指纹必须为 64 位十六进制（HMAC-SHA256 hexdigest），脏指纹拦截不入索引。"""
        for digest in value:
            if not _PII_DIGEST_PATTERN.fullmatch(digest):
                raise ValueError(f"pii_digests 必须为 64 位十六进制 HMAC 指纹，收到：{digest!r}")
        return value

    @model_validator(mode="after")
    def _check_ref_by_category(self) -> "CustomerMeta":
        """关联主键规则：分级标准类全局文档 customer_ref 必须为空；其余三类必须携带。"""
        if self.category == "grade_standard":
            if self.customer_ref:
                raise ValueError("分级标准类为全局文档，不得关联具体 customer_ref")
            return self
        if not self.customer_ref:
            raise ValueError(f"category={self.category!r} 的条目必须携带 customer_ref（客户 ID 关联检索主键）")
        try:
            import uuid as _uuid

            _uuid.UUID(self.customer_ref)
        except ValueError as exc:
            raise ValueError(f"customer_ref 必须为合法 UUID（customer.uid），收到：{self.customer_ref!r}") from exc
        return self


# ============================================================================
# 销售技能库（FR-KB-003，SR-KB-003 §6.1 结构化模型与场景标签体系）
# ============================================================================

#: 四类技能内容（SRS §5.2 原文四要素）
SKILL_KINDS: tuple[str, ...] = ("talk_track", "negotiation", "success_case", "failure_case")
#: 适用阶段（外贸全流程节点，与流程库 P01~P11 主链对齐）
SKILL_STAGES: tuple[str, ...] = (
    "inquiry", "quote", "bargain", "sample", "order", "logistics", "after_sale",
)
#: 槽位取值来源（SR-KB-003 §6.3 SlotSpec.source）
SLOT_SOURCES: tuple[str, ...] = ("product_kb", "customer_kb", "config", "free")
#: 结构化来源白名单：价格/交期类槽位仅允许产品库结构化字段或配置（R-03 防价格幻觉）
STRUCTURED_SLOT_SOURCES: frozenset[str] = frozenset({"product_kb", "config"})
#: 价格/交期类槽位名（SR-KB-003 §7 strategy.skill.render.strict_slots 默认值）——强制结构化来源
PRICE_SLOT_NAMES: tuple[str, ...] = ("price_range", "lead_time")
#: 槽位名词法（话术正文 {占位符} 引用，SR-KB-003 §6.3 SLOT_PATTERN）
_SLOT_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z_][a-z0-9_]*$")

#: 两级场景标签种子词表（SR-KB-003 §6.1 scene_taxonomy.json；quote/bargain 逐字取自方案，
#: 其余阶段为待甲方运营确认的种子值——受控词表，禁止自由标签，变更走版本化配置）。
SCENE_TAXONOMY: dict[str, dict[str, object]] = {
    "inquiry": {"label": "询盘", "scenes": ["首询响应", "询盘澄清", "背景调查"]},
    "quote": {"label": "报价", "scenes": ["首次报价", "批量询价", "竞品比价"]},
    "bargain": {"label": "议价", "scenes": ["砍价应对", "让步阶梯", "赠品策略", "底线坚守"]},
    "sample": {"label": "样品", "scenes": ["样品申请", "样品寄送", "样品费用"]},
    "order": {"label": "订单", "scenes": ["订单确认", "付款安排", "生产跟进"]},
    "logistics": {"label": "物流", "scenes": ["发货安排", "物流跟踪", "清关协助"]},
    "after_sale": {"label": "售后", "scenes": ["售后安抚", "投诉处理", "退换货"]},
}
#: 一级场景中文标签集合（负面案例库 scene_tag 与培训库 scene_tag 共用词表，SR-KB-005 §4）
SCENE_VOCABULARY: frozenset[str] = frozenset(
    str(node["label"]) for node in SCENE_TAXONOMY.values()
)
#: 全部合法场景标签（一级中文标签 ∪ 二级细分场景）
ALL_SCENE_TAGS: frozenset[str] = SCENE_VOCABULARY | frozenset(
    scene for node in SCENE_TAXONOMY.values() for scene in node["scenes"]  # type: ignore[union-attr]
)


def validate_scene_tags(tags: tuple[str, ...] | list[str]) -> None:
    """校验场景标签全部在受控词表内（SR-KB-003 决策 2：禁止自由标签）。"""
    unknown = [tag for tag in tags if tag not in ALL_SCENE_TAGS]
    if unknown:
        raise ValueError(f"场景标签不在受控词表内：{unknown}（词表见 SCENE_TAXONOMY，变更走版本化配置）")


class SlotSpec(BaseModel):
    """变量槽位声明：话术模板中 {占位符} 的取值来源与约束（SR-KB-003 §6.3）。

    价格/交期类槽位（PRICE_SLOT_NAMES）强制结构化来源（product_kb/config），
    禁止 LLM 自由生成——对应风险 R-03「价格幻觉」。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="槽位名，如 product / price_range / moq / lead_time")
    type: Literal["string", "number", "range", "enum"] = "string"
    required: bool = Field(default=False, description="必填槽位缺失时 strict 渲染失败")
    source: Literal["product_kb", "customer_kb", "config", "free"] = "free"
    enum_values: list[str] = Field(default_factory=list, description="type=enum 时的受控取值")
    default: str | None = Field(default=None, description="非必填槽位缺省值")

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """槽位名必须匹配 {占位符} 词法（小写字母/下划线起头）。"""
        if not _SLOT_NAME_PATTERN.fullmatch(value):
            raise ValueError(f"槽位名非法（须匹配 ^[a-z_][a-z0-9_]*$），收到：{value!r}")
        return value

    @model_validator(mode="after")
    def _check_slot_rules(self) -> "SlotSpec":
        """枚举槽位必须声明受控取值；价格/交期槽位强制结构化来源（R-03）。"""
        if self.type == "enum" and not self.enum_values:
            raise ValueError(f"enum 槽位 {self.name!r} 必须声明 enum_values 受控取值")
        if self.name in PRICE_SLOT_NAMES and self.source not in STRUCTURED_SLOT_SOURCES:
            raise ValueError(
                f"价格/交期槽位 {self.name!r} 必须为结构化来源 {sorted(STRUCTURED_SLOT_SOURCES)}"
                "（禁止自由文本来源，R-03 价格防幻觉）"
            )
        return self


class SkillMeta(BaseModel):
    """销售技能条目 metadata 内容模型（kb_type='skill'，SR-KB-003 §6.1）。"""

    model_config = ConfigDict(extra="forbid")

    kb_type: Literal[KBType.SKILL] = KBType.SKILL

    skill_kind: Literal["talk_track", "negotiation", "success_case", "failure_case"]
    stage: Literal["inquiry", "quote", "bargain", "sample", "order", "logistics", "after_sale"]
    scene_tags: list[str] = Field(
        min_length=1, description="场景标签（scene_tags[0] 为主标签，写入 Milvus scene_tag）"
    )
    customer_types: list[str] = Field(default_factory=list, description="适用客户类型（C-01~，SRS §6.3）")
    customer_grades: list[str] = Field(default_factory=list, description="适用客户分级 A/B/C/D")
    difficulty: int = Field(default=2, ge=1, le=3, description="使用难度（对应培训分级）")
    language: list[str] = Field(default_factory=lambda: ["en"])
    slots: list[SlotSpec] = Field(default_factory=list, description="变量槽位（仅 talk_track 携带）")
    outcome: Literal["win", "lose", "neutral"] | None = Field(default=None, description="案例类条目使用结果")

    @field_validator("scene_tags")
    @classmethod
    def _check_scene_tags(cls, value: list[str]) -> list[str]:
        """标签受控词表校验 + 单标签长度上限（主标签写入 VarChar(64) 标量）。"""
        for tag in value:
            if len(tag) > FILTER_VALUE_MAX_LENGTH:
                raise ValueError(f"场景标签超长（上限 {FILTER_VALUE_MAX_LENGTH} 字符）：{tag[:32]!r}…")
        validate_scene_tags(tuple(value))
        return value

    @field_validator("customer_grades")
    @classmethod
    def _check_grades(cls, value: list[str]) -> list[str]:
        """适用分级必须命中 A/B/C/D。"""
        bad = [grade for grade in value if grade not in CUSTOMER_GRADES]
        if bad:
            raise ValueError(f"适用客户分级必须为 {CUSTOMER_GRADES} 之一，收到：{bad}")
        return value

    @model_validator(mode="after")
    def _check_kind_rules(self) -> "SkillMeta":
        """话术模板必须声明 ≥1 个槽位；outcome 仅案例类条目携带。"""
        if self.skill_kind == "talk_track" and not self.slots:
            raise ValueError("话术模板（talk_track）必须声明至少 1 个变量槽位")
        if self.outcome is not None and self.skill_kind not in ("success_case", "failure_case"):
            raise ValueError(f"outcome 仅案例类条目（success_case/failure_case）可携带，当前 skill_kind={self.skill_kind!r}")
        names = [slot.name for slot in self.slots]
        if len(names) != len(set(names)):
            raise ValueError(f"槽位名重复：{sorted({n for n in names if names.count(n) > 1})}")
        return self


# ============================================================================
# 流程知识库（FR-KB-004，SR-KB-004 §4 节点链 / 红线条目结构化契约）
# ============================================================================

#: 流程域内容三分类（SR-KB-004 §2 决策 1）
PROCESS_CATEGORIES: tuple[str, ...] = ("sop", "platform_rule", "domain_knowledge")
#: 拦截动作类型白名单：与风控引擎 FR-AG-005 的动作白名单一一对齐（SR-KB-004 §6 domain.py）
PROCESS_ACTION_TYPES: tuple[str, ...] = (
    "price_change", "order_op", "payment_op",
    "mass_send", "offsite_trade", "contact_bomb", "other",
)
#: 红线风险级别（SR-KB-004 §4 metadata 契约）
PROCESS_RISK_LEVELS: tuple[str, ...] = ("high", "medium")
#: 拦截规则匹配类型（SR-KB-004 §4 intercept_rule.match_type）
INTERCEPT_MATCH_TYPES: tuple[str, ...] = ("keyword", "regex", "action")
#: 节点编号词法（SR-KB-004 §6 chunk_mapper：P\d{2}）
_NODE_CODE_PATTERN: re.Pattern[str] = re.compile(r"^P\d{2}$")

#: 预置主链 P01~P11（SR-KB-004 §4：process_node_dim 权威定义的代码侧种子；
#: sort_no 决定前后置校验的 DAG 方向，P05 样品节点可跳过）。
PRESET_PROCESS_NODES: dict[str, dict[str, object]] = {
    "P01": {"name": "询盘接收", "stage": "pre_sale", "sort_no": 1},
    "P02": {"name": "需求确认", "stage": "pre_sale", "sort_no": 2},
    "P03": {"name": "报价", "stage": "pre_sale", "sort_no": 3},
    "P04": {"name": "议价谈判", "stage": "pre_sale", "sort_no": 4},
    "P05": {"name": "样品", "stage": "pre_sale", "sort_no": 5, "allow_skip": True},
    "P06": {"name": "订单签订", "stage": "deal", "sort_no": 6},
    "P07": {"name": "收款支付", "stage": "deal", "sort_no": 7},
    "P08": {"name": "生产验货", "stage": "fulfill", "sort_no": 8},
    "P09": {"name": "报关出运", "stage": "fulfill", "sort_no": 9},
    "P10": {"name": "物流跟踪", "stage": "fulfill", "sort_no": 10},
    "P11": {"name": "交付售后", "stage": "after_sale", "sort_no": 11},
}
#: 主链顺序（前置/后置推导依据）
PRESET_MAIN_CHAIN: tuple[str, ...] = tuple(PRESET_PROCESS_NODES)


class InterceptRule(BaseModel):
    """机器可执行拦截规则（SR-KB-004 §4 intercept_rule；供风控引擎 FR-AG-005 消费）。"""

    model_config = ConfigDict(extra="forbid")

    match_type: Literal["keyword", "regex", "action"]
    patterns: list[str] = Field(default_factory=list, description="关键词/正则模式串")
    case_sensitive: bool = Field(default=False, description="匹配是否区分大小写")

    @field_validator("patterns")
    @classmethod
    def _check_patterns(cls, value: list[str]) -> list[str]:
        """模式串非空且长度受限（拦截热词表规模可控）。"""
        for pattern in value:
            if not pattern.strip():
                raise ValueError("intercept_rule.patterns 不允许空串")
            if len(pattern) > 128:
                raise ValueError(f"拦截模式超长（上限 128 字符）：{pattern[:32]!r}…")
        return value

    @model_validator(mode="after")
    def _check_by_match_type(self) -> "InterceptRule":
        """keyword/regex 必须携带 ≥1 模式；regex 模式必须可编译（坏正则入库即拦截）。"""
        if self.match_type in ("keyword", "regex") and not self.patterns:
            raise ValueError(f"match_type={self.match_type!r} 必须携带至少 1 个 pattern")
        if self.match_type == "regex":
            for pattern in self.patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"拦截正则不可编译：{pattern!r}（{exc}）") from exc
        return self


class ProcessMeta(BaseModel):
    """流程条目 metadata 内容模型（kb_type='process'，SR-KB-004 §4 metadata 契约）。

    红线条目（is_redline=True）强制携带 action_type / risk_level / intercept_rule
    三个机器可执行字段（「流程节点可追踪」与风控拦截清单的结构化前提）。
    """

    model_config = ConfigDict(extra="forbid")

    kb_type: Literal[KBType.PROCESS] = KBType.PROCESS

    category: Literal["sop", "platform_rule", "domain_knowledge"]
    process_nodes: list[str] = Field(
        default_factory=list, description="关联节点编号（P01~P11；platform_rule 为适用节点范围）"
    )
    prev_nodes: list[str] = Field(
        default_factory=list, description="前置节点编号（节点链 DAG，供双向追踪）"
    )
    next_nodes: list[str] = Field(
        default_factory=list, description="后置节点编号（节点链 DAG，供双向追踪）"
    )
    is_redline: bool = Field(default=False, description="红线标记（红线条目必填下列三字段）")
    action_type: Literal[
        "price_change", "order_op", "payment_op",
        "mass_send", "offsite_trade", "contact_bomb", "other"
    ] | None = Field(default=None, description="拦截动作类型（与 FR-AG-005 白名单对齐）")
    risk_level: Literal["high", "medium"] | None = Field(default=None)
    intercept_rule: InterceptRule | None = Field(default=None, description="机器可执行拦截规则")
    advice: str = Field(default="", max_length=500, description="命中红线的建议话术/处置建议")
    rule_source: str = Field(default="", max_length=200, description="规则出处（如阿里国际站规则中心版本）")
    effective_at: date | None = Field(default=None, description="规则生效日期")

    @field_validator("process_nodes", "prev_nodes", "next_nodes")
    @classmethod
    def _check_node_codes(cls, value: list[str]) -> list[str]:
        r"""节点编号必须匹配 P\d{2} 且在预置主链内（deprecated 外节点不参与过滤）。"""
        for code in value:
            if not _NODE_CODE_PATTERN.fullmatch(code):
                raise ValueError(f"流程节点编号须匹配 P\\d{{2}}（如 P03），收到：{code!r}")
            if code not in PRESET_PROCESS_NODES:
                raise ValueError(f"流程节点 {code!r} 不在预置主链 {PRESET_MAIN_CHAIN} 内")
        if len(value) != len(set(value)):
            raise ValueError(f"节点编号重复：{value}")
        return value

    @model_validator(mode="after")
    def _check_chain_direction(self) -> "ProcessMeta":
        """前置节点 sort_no 均小于当前首个关联节点、后置均大于（DAG 方向保证，防环）。

        比较基准取 process_nodes[0]（主关联节点）；无关联节点（跨节点通识）时跳过。
        """
        if not self.process_nodes:
            return self
        base = int(PRESET_PROCESS_NODES[self.process_nodes[0]]["sort_no"])  # type: ignore[arg-type]
        for code in self.prev_nodes:
            if int(PRESET_PROCESS_NODES[code]["sort_no"]) >= base:  # type: ignore[arg-type]
                raise ValueError(f"前置节点 {code} 的顺序号必须小于主节点 {self.process_nodes[0]}（DAG 方向）")
        for code in self.next_nodes:
            if int(PRESET_PROCESS_NODES[code]["sort_no"]) <= base:  # type: ignore[arg-type]
                raise ValueError(f"后置节点 {code} 的顺序号必须大于主节点 {self.process_nodes[0]}（DAG 方向）")
        return self

    @model_validator(mode="after")
    def _check_redline_fields(self) -> "ProcessMeta":
        """红线条目强校验：action_type / risk_level / intercept_rule 三字段缺一不可。"""
        if self.is_redline:
            missing = [
                name for name, value in (
                    ("action_type", self.action_type),
                    ("risk_level", self.risk_level),
                    ("intercept_rule", self.intercept_rule),
                ) if value is None
            ]
            if missing:
                raise ValueError(f"红线条目（is_redline=true）必须携带结构化字段：{missing}")
        return self


# ============================================================================
# 负面案例库（FR-KB-005，SR-KB-005 §6.1 分类树 / §6.2 结构化模型）
# ============================================================================

#: 案例四大类（SRS §5.2 原文四要素）
CASE_CATEGORIES: tuple[str, ...] = ("lost_order", "complaint", "wrong_script", "platform_penalty")
#: reason_type 两级分类树（SR-KB-005 §6.1 逐字对齐：一级=案例大类，二级=原因类型；
#: 代码常量为默认版，app_config(scope='kb') 可版本化覆盖）
NEGATIVE_CASE_REASON_TREE: dict[str, dict[str, object]] = {
    "lost_order": {"label": "丢单原因分析", "children": {
        "price_handling": "议价/报价失误", "response_speed": "响应不及时",
        "info_error": "产品信息/参数答复错误", "delivery_promise": "交期承诺失误",
        "trust_building": "信任建立不足", "followup_gap": "跟进断档/节奏失误",
        "competitor": "竞品对比失利", "service_attitude": "服务态度问题"}},
    "complaint": {"label": "客户投诉处理", "children": {
        "quality": "产品质量投诉", "delay": "交期延误投诉",
        "logistics_damage": "物流破损/丢失", "after_sales": "售后响应不当",
        "payment_dispute": "付款/退款争议", "attitude": "沟通态度投诉"}},
    "wrong_script": {"label": "常见错误话术", "children": {
        "over_promise": "过度承诺（交期/质量/认证）", "price_leak": "报价口径错误/底价泄露",
        "sensitive_word": "敏感词/违规表述", "impolite": "不礼貌/文化冲突表述",
        "misleading": "误导性/虚假宣传", "info_leak": "客户信息或商业机密泄露"}},
    "platform_penalty": {"label": "平台处罚案例", "children": {
        "bulk_spam": "批量群发/垃圾消息", "offsite_induce": "诱导站外交易",
        "contact_bomb": "联系方式轰炸", "auto_behavior": "机械式自动化行为",
        "ip_login_risk": "IP/登录环境风险", "listing_violation": "商品信息违规"}},
}


def validate_reason_path(path: str) -> bool:
    """校验 '一级.二级' 路径存在于分类树（SR-KB-005 §6.1）。"""
    top, _, leaf = path.partition(".")
    node = NEGATIVE_CASE_REASON_TREE.get(top)
    if not isinstance(node, dict):
        return False
    children = node.get("children")
    return isinstance(children, dict) and leaf in children


#: 丢单阶段（SR-KB-005 §6.2 LostOrderDetail.stage）
_LOST_ORDER_STAGES: tuple[str, ...] = ("询盘", "报价", "样品", "订单")
#: 脱敏客户标识格式（SR-KB-005 §6.2：CUST-****-0123）
_MASKED_CUSTOMER_REF_PATTERN: re.Pattern[str] = re.compile(r"^CUST-\*+-\d+$")
#: 证据占位符格式（SR-KB-005 §8：真实凭证一律占位符 <OSS-OBJ-KEY>）
_EVIDENCE_PLACEHOLDER_PATTERN: re.Pattern[str] = re.compile(r"^<.+>$")
#: 案例发生时间（脱敏口径，可模糊到月：YYYY / YYYY-MM / YYYY-MM-DD）
_OCCURRED_AT_PATTERN: re.Pattern[str] = re.compile(r"^\d{4}(-\d{2})?(-\d{2})?$")


class ScriptPair(BaseModel):
    """错误话术-正确话术对照对：本库核心资产（P1 演练 rubric 与 P2 规避校验共用）。"""

    model_config = ConfigDict(extra="forbid")

    pair_uid: str = Field(min_length=1, max_length=64)
    wrong_script: str = Field(min_length=1, description="错误话术原文（脱敏后；独立向量子块来源）")
    correct_script: str = Field(min_length=1, description="修复后的正确话术（重写建议）")
    scene_tag: str = Field(min_length=1, description="场景标签（与技能库共用词表）")
    risk_level: Literal["high", "medium", "low"] = "medium"
    reason_leaf: str = Field(description="必为 wrong_script.* 叶子，如 wrong_script.over_promise")
    trigger_keywords: list[str] = Field(default_factory=list, description="快筛关键词（ScriptGuard 第一通道）")

    @field_validator("scene_tag")
    @classmethod
    def _check_scene(cls, value: str) -> str:
        """场景标签必须在共用受控词表内（SR-KB-005 §4：询盘/报价/议价/样品/订单/物流/售后）。"""
        if value not in SCENE_VOCABULARY:
            raise ValueError(f"场景标签必须在共用词表 {sorted(SCENE_VOCABULARY)} 内，收到：{value!r}")
        return value

    @field_validator("reason_leaf")
    @classmethod
    def _check_reason_leaf(cls, value: str) -> str:
        """对照对原因叶子必须为 wrong_script.* 且路径存在于分类树。"""
        if not value.startswith("wrong_script.") or not validate_reason_path(value):
            raise ValueError(f"对照对 reason_leaf 必须为分类树上 wrong_script.* 叶子，收到：{value!r}")
        return value


class LostOrderDetail(BaseModel):
    """丢单原因分析结构化字段（SR-KB-005 §6.2）。"""

    model_config = ConfigDict(extra="forbid")

    customer_ref: str = Field(description="脱敏客户标识，如 CUST-****-0123")
    stage: str = Field(description="丢单阶段")
    order_amount_usd: Decimal | None = Field(default=None, ge=0)
    root_cause: str = Field(min_length=1)
    lesson: str = Field(default="", description="教训沉淀（P1 复盘引用）")

    @field_validator("customer_ref")
    @classmethod
    def _check_masked_ref(cls, value: str) -> str:
        """客户标识强制脱敏格式（明文客户名/编号拒绝入库，NFR-SEC-01）。"""
        if not _MASKED_CUSTOMER_REF_PATTERN.fullmatch(value):
            raise ValueError(f"丢单案例 customer_ref 须为脱敏格式（CUST-****-数字），收到：{value!r}")
        return value

    @field_validator("stage")
    @classmethod
    def _check_stage(cls, value: str) -> str:
        if value not in _LOST_ORDER_STAGES:
            raise ValueError(f"丢单阶段必须为 {_LOST_ORDER_STAGES} 之一，收到：{value!r}")
        return value


class ComplaintDetail(BaseModel):
    """客户投诉处理结构化字段（SR-KB-005 §6.2）。"""

    model_config = ConfigDict(extra="forbid")

    complaint_type: str = Field(description="complaint.* 叶子")
    severity: Literal["high", "medium", "low"]
    handling_sop_ref: str | None = Field(default=None, description="关联流程库 SOP 条目 uid（FR-KB-004）")
    resolution: str = Field(min_length=1)
    result: Literal["resolved", "compensated", "lost_customer"]

    @field_validator("complaint_type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if not value.startswith("complaint.") or not validate_reason_path(value):
            raise ValueError(f"complaint_type 必须为分类树上 complaint.* 叶子，收到：{value!r}")
        return value


class PenaltyDetail(BaseModel):
    """平台处罚案例结构化字段（与 FR-AG-005 风控规则联动，SR-KB-005 §6.2）。"""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(default="alibaba", max_length=32)
    violation_type: str = Field(description="platform_penalty.* 叶子")
    penalty_level: Literal["warning", "limit", "suspend", "ban"]
    occurred_at: str = Field(description="处罚发生时间（脱敏口径，可模糊到月）")
    root_cause: str = Field(min_length=1)
    prevention_rule_id: str | None = Field(
        default=None, description="对应 FR-AG-005 拦截清单规则 ID（案例→规则闭环）"
    )
    evidence_ref: str = Field(description="证据 OSS 对象键（真实凭证一律占位符）")

    @field_validator("violation_type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if not value.startswith("platform_penalty.") or not validate_reason_path(value):
            raise ValueError(f"violation_type 必须为分类树上 platform_penalty.* 叶子，收到：{value!r}")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _check_occurred(cls, value: str) -> str:
        if not _OCCURRED_AT_PATTERN.fullmatch(value):
            raise ValueError(f"occurred_at 须为 YYYY[-MM[-DD]]（可模糊到月），收到：{value!r}")
        return value

    @field_validator("evidence_ref")
    @classmethod
    def _check_evidence(cls, value: str) -> str:
        """证据引用强制占位符格式（<OSS-OBJ-KEY> 风格，真实凭证不入库）。"""
        if not _EVIDENCE_PLACEHOLDER_PATTERN.fullmatch(value):
            raise ValueError(f"evidence_ref 须为占位符格式（<OSS-OBJ-KEY>），收到：{value!r}")
        return value


class NegativeCaseMeta(BaseModel):
    """负面案例条目 metadata 内容模型（kb_type='negative_case'，SR-KB-005 §6.2）。"""

    model_config = ConfigDict(extra="forbid")

    kb_type: Literal[KBType.NEGATIVE_CASE] = KBType.NEGATIVE_CASE

    case_category: Literal["lost_order", "complaint", "wrong_script", "platform_penalty"]
    reason_type: str = Field(description="分类树路径 一级.二级，如 lost_order.price_handling")
    scene_tag: str | None = Field(default=None, description="场景标签（与技能库共用词表）")
    occurred_at: str = Field(description="案例发生时间（脱敏口径，可模糊到月）")
    lost_order: LostOrderDetail | None = None
    complaint: ComplaintDetail | None = None
    penalty: PenaltyDetail | None = None
    script_pairs: list[ScriptPair] = Field(
        default_factory=list, description="wrong_script 类必填 ≥1 对；可挂任意案例（丢单常含致败话术）"
    )

    @field_validator("reason_type")
    @classmethod
    def _check_reason_type(cls, value: str) -> str:
        """分类树路径合法性 + 长度上限（写入 Milvus reason_type 标量，VarChar(64)）。"""
        if not validate_reason_path(value):
            raise ValueError(f"非法原因类型：{value!r}（路径须存在于 NEGATIVE_CASE_REASON_TREE）")
        if len(value) > FILTER_VALUE_MAX_LENGTH:
            raise ValueError(f"reason_type 超长（上限 {FILTER_VALUE_MAX_LENGTH} 字符）：{value!r}")
        return value

    @field_validator("scene_tag")
    @classmethod
    def _check_scene(cls, value: str | None) -> str | None:
        if value is not None and value not in SCENE_VOCABULARY:
            raise ValueError(f"场景标签必须在共用词表 {sorted(SCENE_VOCABULARY)} 内，收到：{value!r}")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _check_occurred(cls, value: str) -> str:
        if not _OCCURRED_AT_PATTERN.fullmatch(value):
            raise ValueError(f"occurred_at 须为 YYYY[-MM[-DD]]（可模糊到月），收到：{value!r}")
        return value

    @model_validator(mode="after")
    def _check_category_consistency(self) -> "NegativeCaseMeta":
        """分类一致性与必填互斥校验（SR-KB-005 §6.2 _check，错误对应 HTTP 40001）。"""
        if self.reason_type.split(".", 1)[0] != self.case_category:
            raise ValueError(
                f"reason_type 一级须等于 case_category（{self.reason_type!r} vs {self.case_category!r}）"
            )
        required_detail = {
            "lost_order": ("lost_order", self.lost_order, "丢单案例须含 lost_order 字段"),
            "complaint": ("complaint", self.complaint, "投诉案例须含 complaint 字段"),
            "platform_penalty": ("penalty", self.penalty, "平台处罚案例须含 penalty 字段"),
        }
        rule = required_detail.get(self.case_category)
        if rule is not None and rule[1] is None:
            raise ValueError(rule[2])
        if self.case_category == "wrong_script" and not self.script_pairs:
            raise ValueError("错误话术类案例须至少 1 组对照对（script_pairs）")
        return self


# ============================================================================
# 新人带教素材库（FR-KB-006，SR-KB-006 §4 扩展表 → metadata 投影 / SRS §6.3）
# ============================================================================

#: 素材三类（SRS §5.2：培训材料、考试题库、模拟场景；课程包为结构化引用头）
TRAINING_MATERIAL_KINDS: tuple[str, ...] = ("lesson", "question", "scenario")
#: 题型（SR-KB-006 §4 question_bank_item.question_type CHECK）
QUESTION_TYPES: tuple[str, ...] = ("single", "multi", "judge", "short_answer", "case")
#: 能力维度（SR-KB-006 §6 DIMENSIONS，对齐 FR-TR-004 雷达四维）
ABILITY_DIMENSIONS: tuple[str, ...] = ("product", "communication", "negotiation", "platform")
#: 模拟场景流程节点（SR-KB-006 §4 sim_scenario.stage_node CHECK；注意与技能库
#: after_sale（SR-KB-003 STAGES）一词之差，各自保持文档口径）
SIM_STAGE_NODES: tuple[str, ...] = (
    "inquiry", "quote", "negotiation", "sample", "order", "logistics", "after_sales",
)
#: 模拟客户类型编号（SRS §6.3 最低配置 C-01~C-06，C-07+ 为甲方扩展）
CUSTOMER_TYPE_PATTERN: re.Pattern[str] = re.compile(r"^C-\d{2,}$")


class AbilityDims(BaseModel):
    """能力维度权重（和为 1；雷达四维的素材侧映射，SR-KB-006 §6）。"""

    model_config = ConfigDict(extra="forbid")

    product: float = Field(default=0.0, ge=0.0, le=1.0, description="产品知识")
    communication: float = Field(default=0.0, ge=0.0, le=1.0, description="沟通技巧")
    negotiation: float = Field(default=0.0, ge=0.0, le=1.0, description="议价能力")
    platform: float = Field(default=0.0, ge=0.0, le=1.0, description="平台规则")

    @model_validator(mode="after")
    def _sum_is_one_or_zero(self) -> "AbilityDims":
        """权重约定：要么全零（未声明），要么四维和为 1（±1e-6 容差）。"""
        total = self.product + self.communication + self.negotiation + self.platform
        if total > 0.0 and abs(total - 1.0) > 1e-6:
            raise ValueError(f"能力维度权重之和必须为 1（当前 {total:.6f}；全零表示未声明）")
        return self


class AssessmentPoint(BaseModel):
    """模拟场景考核点（SR-KB-006 §4 sim_scenario.assessment_points 元素）。"""

    model_config = ConfigDict(extra="forbid")

    dim: Literal["product", "communication", "negotiation", "platform"]
    desc: str = Field(min_length=1, description="考核点描述")
    weight: float = Field(default=1.0, gt=0.0, le=1.0, description="考核权重")


class TrainingMeta(BaseModel):
    """带教素材条目 metadata 内容模型（kb_type='training'，SR-KB-006 §4 / SRS §6.3）。

    素材三类（lesson/question/scenario）共用一模型按 kind 条件必填；
    课程包以 course_code 引用（training_course.code 为导入幂等键）。
    注意：answer/rubric 永不进入 metadata（永不向量化防答案泄漏，SR-KB-006 §4）。
    """

    model_config = ConfigDict(extra="forbid")

    kb_type: Literal[KBType.TRAINING] = KBType.TRAINING

    material_kind: Literal["lesson", "question", "scenario"]
    course_code: str | None = Field(default=None, max_length=64, description="课程包编码引用（课时必填）")
    course_title: str = Field(default="", max_length=200, description="课程包标题（展示用冗余）")
    seq: int | None = Field(default=None, ge=1, description="课时顺序（lesson 用，学习路径按此展开）")
    scene_tag: str = Field(default="", description="场景标签（与销售技能库 FR-KB-003 联动）")
    question_type: Literal["single", "multi", "judge", "short_answer", "case"] | None = Field(
        default=None, description="题型（question 必填）"
    )
    knowledge_points: list[str] = Field(default_factory=list, description="知识点（组卷过滤维度）")
    ability_dims: AbilityDims = Field(default_factory=AbilityDims, description="能力维度映射（雷达回填依据）")
    difficulty: int = Field(default=3, ge=1, le=5, description="难度（题目 1-5）")
    est_minutes: int = Field(default=10, ge=1, le=600, description="预计学习分钟数（lesson）")
    scenario_key: str | None = Field(default=None, max_length=64, description="场景稳定标识（scenario 必填）")
    customer_type: str | None = Field(default=None, description="模拟客户类型（C-01~，SRS §6.3 类型库）")
    stage_node: Literal[
        "inquiry", "quote", "negotiation", "sample", "order", "logistics", "after_sales"
    ] | None = Field(default=None, description="场景所处流程节点（scenario 必填）")
    opening_context: str | None = Field(default=None, description="开场情境（AI 客户第一句 + 背景设定）")
    hidden_needs: list[str] = Field(default_factory=list, description="客户隐藏诉求（复盘对照）")
    assessment_points: list[AssessmentPoint] = Field(default_factory=list, description="考核点")

    @field_validator("scene_tag")
    @classmethod
    def _check_scene(cls, value: str) -> str:
        """场景标签受控（空串=未打标；非空须在共用词表内）。"""
        if value and value not in ALL_SCENE_TAGS:
            raise ValueError(f"场景标签不在受控词表内：{value!r}（与技能库共用 SCENE_TAXONOMY 词表）")
        if len(value) > FILTER_VALUE_MAX_LENGTH:
            raise ValueError(f"场景标签超长（上限 {FILTER_VALUE_MAX_LENGTH} 字符）：{value!r}")
        return value

    @field_validator("customer_type")
    @classmethod
    def _check_customer_type(cls, value: str | None) -> str | None:
        """模拟客户类型对齐 SRS §6.3 类型库编号（C-01~C-06 最低配置，C-07+ 扩展）。"""
        if value is not None and not CUSTOMER_TYPE_PATTERN.fullmatch(value):
            raise ValueError(f"模拟客户类型须匹配 C-数字（如 C-01，SRS §6.3 类型库），收到：{value!r}")
        return value

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> "TrainingMeta":
        """按素材类型的条件必填：question→题型；scenario→场景四要素；lesson→课程包+顺序。"""
        if self.material_kind == "question":
            if self.question_type is None:
                raise ValueError("题目素材（question）必须声明 question_type 题型")
        elif self.material_kind == "scenario":
            missing = [
                name for name, value in (
                    ("scenario_key", self.scenario_key),
                    ("customer_type", self.customer_type),
                    ("stage_node", self.stage_node),
                    ("opening_context", self.opening_context),
                ) if not value
            ]
            if missing:
                raise ValueError(f"模拟场景素材（scenario）必须携带 {missing}（对齐 SRS §6.3 场景定义）")
        elif self.material_kind == "lesson":
            if not self.course_code:
                raise ValueError("课时素材（lesson）必须携带 course_code 课程包引用（结构化课程包）")
            if self.seq is None:
                raise ValueError("课时素材（lesson）必须携带 seq 课时顺序")
        return self


# ============================================================================
# discriminated union：六库 metadata 按 kb_type 判别（item.py 消费）
# ============================================================================

#: 六库 metadata 判别联合（判别字段 kb_type，Literal 各库取值互斥）
KbMetadata = Annotated[
    Union[
        ProductMeta,
        CustomerMeta,
        SkillMeta,
        ProcessMeta,
        NegativeCaseMeta,
        TrainingMeta,
    ],
    Field(discriminator="kb_type"),
]


__all__ = [
    # 枚举与联合
    "KBType",
    "KbMetadata",
    # 产品库（FR-KB-001）
    "SKU_PATTERN",
    "PRICE_CURRENCIES",
    "PriceTier",
    "PriceFloor",
    "PriceBook",
    "SellingPoint",
    "Cert",
    "Competitor",
    "ProductMeta",
    # 客户库（FR-KB-002）
    "CUSTOMER_CATEGORIES",
    "CUSTOMER_GRADES",
    "PII_FORBIDDEN_METADATA_KEYS",
    "assert_no_pii_keys",
    "CustomerProfileTags",
    "CustomerMeta",
    # 技能库（FR-KB-003）
    "SKILL_KINDS",
    "SKILL_STAGES",
    "SLOT_SOURCES",
    "STRUCTURED_SLOT_SOURCES",
    "PRICE_SLOT_NAMES",
    "SCENE_TAXONOMY",
    "SCENE_VOCABULARY",
    "ALL_SCENE_TAGS",
    "validate_scene_tags",
    "SlotSpec",
    "SkillMeta",
    # 流程库（FR-KB-004）
    "PROCESS_CATEGORIES",
    "PROCESS_ACTION_TYPES",
    "PROCESS_RISK_LEVELS",
    "INTERCEPT_MATCH_TYPES",
    "PRESET_PROCESS_NODES",
    "PRESET_MAIN_CHAIN",
    "InterceptRule",
    "ProcessMeta",
    # 负面案例库（FR-KB-005）
    "CASE_CATEGORIES",
    "NEGATIVE_CASE_REASON_TREE",
    "validate_reason_path",
    "ScriptPair",
    "LostOrderDetail",
    "ComplaintDetail",
    "PenaltyDetail",
    "NegativeCaseMeta",
    # 带教素材库（FR-KB-006）
    "TRAINING_MATERIAL_KINDS",
    "QUESTION_TYPES",
    "ABILITY_DIMENSIONS",
    "SIM_STAGE_NODES",
    "CUSTOMER_TYPE_PATTERN",
    "AbilityDims",
    "AssessmentPoint",
    "TrainingMeta",
]
