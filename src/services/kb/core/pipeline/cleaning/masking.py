# 知识产权归甲方所有（COM-IP-01）
"""统一脱敏引擎 MaskingEngine（NFR-SEC-01 唯一权威实现，SR-NFR-SEC-01 §6.1 / §6.2）。

- 检测层：版本化规则集（MaskRule 正则，先注册者优先、长匹配优先、重叠消解）
  + 三级白名单过滤之词面级（显式白名单 + 公司名后缀词典，决策 D4 防误杀）；
- 替换层四类动作（决策 D2，对齐 06-安全与合规设计 §2 表）：
    vault_mask     邮箱 / 电话 / WhatsApp —— 部分遮蔽 + 原文可逆加密入保险库（占位接口 Vault）；
    partial_mask   地址 —— 不可逆遮蔽（仅留国家 / 省层级，无法切分则全遮蔽 + needs_review）；
    delete         身份证 / 支付卡 —— 完全删除，不入保险库（06 §2 硬规则：不留存即不可还原）；
    whitelist_keep 白名单命中 —— 保留原文并留痕；
- 双模式：构造时传入 vault 为可逆模式（原文入保险库，授权后可还原）；
  不传 vault 为不可逆模式（vault_mask 规则降级为纯遮蔽——更严格方向，绝非降级明文）；
- 价格策略：演练库（training/negative_case/skill）遮蔽、产品库保留（06 §2，按 kb_type 分派）；
- 规则版本化：RuleSet.version 单调递增由配置层保证，每条 Finding 携带版本（决策 D3）。

生产执行点：E1 清洗入库前（pipeline 阶段三）；E2 案例启用闸门 / E3 出口动态脱敏
不在本模块范围（见 SR-NFR-SEC-01 §6.3）。

依据文档：docs/07-实现方案/NFR/SR-NFR-SEC-01-数据脱敏.md §6.1、§6.2；
         docs/07-实现方案/KB/SR-KB-011-数据清洗.md §6.5；06-安全与合规设计 §2。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol, Sequence

from .base import CleanAction, STAGE_MASK, now_iso

# 允许的替换动作枚举（决策 D2；whitelist_keep 为运行期判定结果，不作为规则静态动作）
ALLOWED_ACTIONS: tuple[str, ...] = ("partial_mask", "vault_mask", "delete")

# 价格脱敏策略默认值：演练库遮蔽、产品库保留（SR-KB-011 §7 clean.mask.price_policy）
DEFAULT_PRICE_POLICY: dict[str, str] = {
    "product": "keep",
    "training": "mask",
    "negative_case": "mask",
    "customer": "keep",
    "skill": "mask",
    "process": "keep",
}

# 公司名后缀词典（三级白名单第二级，SR-NFR-SEC-01 §6.2）：命中 → 保留并留痕
COMPANY_SUFFIXES: tuple[str, ...] = (
    "co., ltd",
    "inc",
    "ltd",
    "gmbh",
    "limited",
    "有限公司",
    "集团",
    "公司",
    "贸易行",
    "厂",
)

# 地址检测正则：须含街道要素（路/街/巷/道）且匹配区间内含数字（门牌/楼栋），降低误报；
# 省/市前缀可选——无省层级时无法按「仅留国家/省」切分 → 全遮蔽 + needs_review（§6.5 ④）。
# 已知边界：省/市前缀可能吞并紧邻的普通汉字（如「从上海市区发货」），属正则方案固有局限，
# 由 06 §2 抽样复核（review_sample_ratio）兜底；检测为增强项时不改变规则语义。
_ADDR_PATTERN = (
    r"(?:中国)?"
    r"(?=[\u4e00-\u9fa5A-Za-z0-9#\-]{0,90}\d)"
    r"(?:[\u4e00-\u9fa5]{2,8}(?:省|自治区))?"
    r"(?:[\u4e00-\u9fa5]{2,8}(?:市|盟|州))?"
    r"[\u4e00-\u9fa5A-Za-z0-9#\-]{2,60}?"
    r"(?:大道|路|街|巷|道)"
    r"(?:\d{1,5}(?:号院|号))?"
    r"(?:[\u4e00-\u9fa5A-Za-z0-9]{1,8}?(?:栋|幢|座|单元))?"
    r"(?:\d{1,5}(?:室|层|楼))?"
)

# 价格检测正则（须带币种标记，避免误杀普通数字；仅演练库启用遮蔽）
_PRICE_PATTERN = (
    r"(?<![A-Za-z0-9])"
    r"(?:(?:USD|EUR|CNY|RMB|US\$)\s*\d{1,12}(?:[.,]\d{1,4})?"
    r"|[¥￥€$]\s*\d{1,12}(?:[.,]\d{1,4})?"
    r"|\d{1,12}(?:[.,]\d{1,4})?\s*(?:USD|EUR|CNY|RMB|美元|欧元|人民币|元))"
    r"(?![A-Za-z0-9])"
)
PRICE_MASKED = "[价格已遮蔽]"

# 模块级正则缓存（规则版本化序列化存 pattern 字符串，加载时预编译校验）
_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def passes_luhn(raw: str) -> bool:
    """Luhn（模 10）校验：支付卡号合法性复核，未通过则不视为卡号（减误杀，§6.5）。"""
    digits = re.sub(r"\D", "", raw)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def content_digest(salt: str, raw: str) -> str:
    """留痕摘要：HMAC-SHA256(盐, 原文) 前 32 hex——可比对不泄露（决策 D5，审计落库用）。"""
    return hmac.new(salt.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class MaskRule:
    """单条脱敏规则：检测（正则 + 可选捕获组 / Luhn 复核）→ 分类动作（§6.1）。"""

    rule_id: str
    pattern: str
    action: str  # partial_mask | vault_mask | delete
    capture: int | None = None  # 命中分组号（None=整体匹配）；WhatsApp 上下文规则只取号码分组
    luhn: bool = False  # True 时命中串须通过 Luhn 校验方视为命中（支付卡减误报）

    def compiled(self) -> re.Pattern[str]:
        """预编译（带缓存）：坏正则在规则集构建期即失败，拒绝发布（§6.1）。"""
        if self.pattern not in _PATTERN_CACHE:
            _PATTERN_CACHE[self.pattern] = re.compile(self.pattern)
        return _PATTERN_CACHE[self.pattern]

    def validate(self) -> None:
        """规则合法性校验：动作枚举 + 正则可编译（坏正则统一转 ValueError，便于加载层捕获）。"""
        if self.action not in ALLOWED_ACTIONS:
            raise ValueError(f"非法动作 {self.action!r}：允许值 {ALLOWED_ACTIONS}")
        try:
            self.compiled()
        except re.error as exc:
            raise ValueError(f"规则 {self.rule_id!r} 正则非法：{exc}") from exc


class RuleSet:
    """规则集版本化容器：整体存 app_config(scope='mask')，版本单调递增（决策 D3）。

    whitelist 为三级白名单合并结果（人工维护 + 客户档案公司名 + 后缀词典命中项），
    词面级精确命中在此生效；后缀词典级在引擎 detect 内生效（§6.2）。
    """

    def __init__(
        self,
        version: str,
        rules: Sequence[MaskRule],
        whitelist: Iterable[str] = (),
        price_policy: Mapping[str, str] | None = None,
    ) -> None:
        if not version:
            raise ValueError("规则集版本不能为空（决策 D3 版本化）")
        for rule in rules:
            rule.validate()  # 坏规则拒绝发布（MASK-1001 语义在加载层抛出）
        self.version = version
        self.rules: tuple[MaskRule, ...] = tuple(rules)
        self.whitelist: frozenset[str] = frozenset(w.lower() for w in whitelist)
        self.price_policy: dict[str, str] = dict(price_policy or DEFAULT_PRICE_POLICY)

    def rule(self, rule_id: str) -> MaskRule | None:
        """按 rule_id 取规则（规则版本化排查用）。"""
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        return None


def default_ruleset(version: str = "mask.rules@v1", whitelist: Iterable[str] = ()) -> RuleSet:
    """内置默认规则集（06-安全 §2 五类 + 支付卡；注册顺序 = 重叠消解优先级）。

    顺序要点：身份证先于支付卡（同为长数字串，身份证优先整体删除）；
    WhatsApp 上下文规则先于通用电话（命中同一号码时前者胜出且保留标签）；
    邮箱先于电话（邮箱局部数字段不落入电话规则）。
    """
    rules = (
        MaskRule("mask.idnum", r"(?<!\d)\d{17}[\dXx](?!\d)", "delete"),
        MaskRule("mask.pay", r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)", "delete", luhn=True),
        MaskRule(
            "mask.whatsapp",
            r"(?:WhatsApp|Whatsapp|whatsapp|WHATSAPP)\s*(?:号码|号|[:：])?\s*"
            r"(\+?\d[\d\s\-]{5,17}\d)",
            "vault_mask",
            capture=1,
        ),
        MaskRule("mask.email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", "vault_mask"),
        MaskRule("mask.phone", r"(?<!\d)(?:\+?\d{1,4}[\s\-]?)?\d{7,13}(?!\d)", "vault_mask"),
        MaskRule("mask.addr", _ADDR_PATTERN, "partial_mask"),
    )
    return RuleSet(version, rules, whitelist)


@dataclass
class Finding:
    """一次检测命中：span 为替换区间（字符偏移），raw 为命中敏感原文（仅存在于内存）。"""

    rule_id: str
    span: tuple[int, int]
    raw: str
    action: str  # vault_mask | partial_mask | delete | whitelist_keep（运行期判定）
    ruleset_version: str  # 动作发生时的规则集版本（决策 D3）
    vault_id: int | None = None  # mask 阶段回填（vault_mask 时指向 mask_vault.id）


@dataclass
class MaskResult:
    """mask() 结果：脱敏后文本 + 命中清单 + 人工复核计数（遗留清单 → FR-KB-024）。"""

    text: str
    findings: list[Finding] = field(default_factory=list)
    needs_review: int = 0  # 无法按层级切分的地址等 → 抽样人工复核清单
    vault_refs: int = 0  # 入保险库条数（可逆脱敏计数）


class Vault(Protocol):
    """保险库协议（可逆 vault 占位接口，SR-KB-011 §6.5 / SR-NFR-SEC-01 §2）。

    生产实现为 AES-256-GCM + KMS 信封加密（密钥不落应用），AAD 绑定 block_index
    防错位还原；reveal 仅授权角色经审计调用（RBAC / 审计在生产 API 层实施）。
    """

    def seal(self, plaintext: str, block_index: int, rule_id: str, source_file: str) -> int:
        """原文加密入库，返回 vault_id。"""
        ...

    def reveal(self, vault_id: int) -> str:
        """按 vault_id 还原原文（授权 + 审计后）。"""
        ...


class InMemoryVault:
    """进程内保险库占位实现：单元测试 / 单机演示用（生产替换为 AES-256-GCM + KMS 实现）。"""

    def __init__(self) -> None:
        self._store: dict[int, str] = {}
        self._seq = 0
        # 封存台账（vault_id, 原文, block_index, rule_id, source_file）——测试断言与审计演示用
        self.sealed: list[tuple[int, str, int, str, str]] = []

    def __len__(self) -> int:
        return len(self._store)

    def seal(self, plaintext: str, block_index: int, rule_id: str, source_file: str) -> int:
        self._seq += 1
        self._store[self._seq] = plaintext
        self.sealed.append((self._seq, plaintext, block_index, rule_id, source_file))
        return self._seq

    def reveal(self, vault_id: int) -> str:
        if vault_id not in self._store:
            raise KeyError(f"vault:{vault_id} 不存在")
        return self._store[vault_id]


def _partial_show(rule_id: str, raw: str) -> str:
    """部分遮蔽展示式（SR-KB-011 §6.5）：邮箱 j***@x.com；电话 / WhatsApp 留国码 +86 ****。"""
    if rule_id == "mask.email":
        local, _, domain = raw.partition("@")
        return f"{local[:1]}***@{domain}"
    if rule_id in ("mask.phone", "mask.whatsapp"):
        m = re.match(r"(\+\d{1,4})[\s\-]?", raw)  # 有国码 → 留国码；无国码 → 全遮蔽
        return f"{m.group(1)} ****" if m else "****"
    return "****"


_PROVINCE_PREFIX = re.compile(r"^(中国)?[\u4e00-\u9fa5]{2,8}?(?:省|自治区)")


def _mask_address(raw: str) -> tuple[str, int]:
    """地址遮蔽：仅留国家 / 省层级（06 §2）；无法切分省层级 → 全遮蔽并计入 needs_review。"""
    m = _PROVINCE_PREFIX.match(raw)
    if m:
        return f"{m.group(0)}[地址已遮蔽]", 0
    return "[地址已遮蔽]", 1


class MaskingEngine:
    """统一脱敏引擎（NFR-SEC-01 唯一权威实现）：检测层 → 白名单过滤 → 替换层 → 留痕。

    E1 清洗入库（pipeline 阶段三）、E2 案例闸门、E3 导出出口全部经由此引擎，
    任何旁路路径视为违规（SR-NFR-SEC-01 §2 执行点矩阵）。
    """

    def __init__(self, ruleset: RuleSet, vault: Vault | None = None) -> None:
        self.ruleset = ruleset
        self.vault = vault  # None → 不可逆模式（vault_mask 规则降级为纯遮蔽，非明文降级）

    # ── 检测层 ──────────────────────────────────────────────────────────────
    def detect(self, text: str) -> list[Finding]:
        """全规则正则扫描 → 排序（起点升序 / 长匹配优先）→ 重叠消解 → 白名单过滤。

        重叠消解保证同一段文本只被一条规则处理：先注册的规则优先
        （身份证 > 支付卡 > WhatsApp > 邮箱 > 电话 > 地址）。
        """
        hits: list[Finding] = []
        for rule in self.ruleset.rules:
            for m in rule.compiled().finditer(text):
                if rule.capture is not None:
                    raw = m.group(rule.capture) or ""
                    if not raw:  # 可选捕获组未命中 → 跳过
                        continue
                    span = m.span(rule.capture)
                else:
                    raw, span = m.group(0), m.span(0)
                if rule.luhn and not passes_luhn(raw):
                    continue  # Luhn 不通过 → 疑似订单号等业务编号，不误杀
                hits.append(Finding(rule.rule_id, span, raw, rule.action, self.ruleset.version))
        hits.sort(key=lambda f: (f.span[0], -(f.span[1] - f.span[0])))
        accepted: list[Finding] = []
        for f in hits:
            if any(f.span[0] < a.span[1] and a.span[0] < f.span[1] for a in accepted):
                continue  # 与已接受命中重叠 → 丢弃
            # 三级白名单之词面级：显式白名单 + 公司名后缀词典（决策 D4，防误杀）
            low = f.raw.lower()
            if low in self.ruleset.whitelist or any(low.endswith(sfx) for sfx in COMPANY_SUFFIXES):
                f.action = "whitelist_keep"
            accepted.append(f)
        return accepted

    def scan_only(self, text: str, kb_type: str = "product") -> list[Finding]:
        """只检测不改写（E2 闸门 / 验证扫描器共用）：whitelist_keep 不计为残留（§6.2）。"""
        return [f for f in self.detect(text) if f.action != "whitelist_keep"]

    # ── 替换层 ──────────────────────────────────────────────────────────────
    def mask(
        self,
        text: str,
        *,
        block_index: int = 0,
        source_file: str = "",
        kb_type: str = "product",
        actions: list[CleanAction] | None = None,
    ) -> MaskResult:
        """按 action 四类分派替换（决策 D2），逐条留痕（不落明文，占位指针指向保险库）。

        价格策略在 PII 替换后按 kb_type 追加执行（演练库遮蔽 / 产品库保留）。
        """
        findings = self.detect(text)
        needs_review = 0
        vault_refs = 0
        actions = actions if actions is not None else []

        def _record(rule_id: str, before: str, after: str) -> None:
            actions.append(
                CleanAction(
                    seq=len(actions) + 1,
                    stage=STAGE_MASK,
                    rule_id=rule_id,
                    before=before,
                    after=after,
                    ts=now_iso(),
                )
            )

        parts: list[str] = []
        offset = 0
        for f in findings:  # 从左到右替换，span 基于原文、输出随伸缩拼接
            s, e = f.span
            parts.append(text[offset:s])
            if f.action == "whitelist_keep":
                shown = f.raw  # 公司名等业务必需字段保留（防误杀，06 §2）
                _record("whitelist.keep", f.raw, shown)
            elif f.action == "vault_mask":
                shown = _partial_show(f.rule_id, f.raw)
                if self.vault is not None:  # 可逆模式：原文入保险库，留痕只放占位指针
                    f.vault_id = self.vault.seal(f.raw, block_index, f.rule_id, source_file)
                    vault_refs += 1
                    _record(f.rule_id, f"(vault:{f.vault_id})", shown)
                else:  # 不可逆模式：降级为纯遮蔽（更严格方向，非明文降级）
                    _record(f.rule_id, f"(masked:{f.rule_id})", shown)
            elif f.action == "partial_mask":
                if f.rule_id == "mask.addr":
                    shown, review = _mask_address(f.raw)
                    needs_review += review
                else:
                    shown = _partial_show(f.rule_id, f.raw)
                _record(f.rule_id, f"(masked:{f.rule_id})", shown)
            else:  # delete：身份证 / 支付卡完全删除，不入保险库（06 §2 硬规则）
                shown = f"[{f.rule_id}已删除]"
                _record(f.rule_id, f"(masked:{f.rule_id})", shown)
            parts.append(shown)
            offset = e
        parts.append(text[offset:])
        out = "".join(parts)

        # 价格策略：演练库遮蔽、产品库保留（06 §2；SR-KB-011 §7 price_policy）
        if self.ruleset.price_policy.get(kb_type) == "mask":
            price_re = re.compile(_PRICE_PATTERN)

            def _mask_price(m: re.Match[str]) -> str:
                _record("mask.price", m.group(0), PRICE_MASKED)  # 价格非客户 PII，diff 保留原文便于核对
                return PRICE_MASKED

            out = price_re.sub(_mask_price, out)

        return MaskResult(out, findings, needs_review, vault_refs)
