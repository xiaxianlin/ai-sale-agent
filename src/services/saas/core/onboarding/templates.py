# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""配置默认模板库（FR-SAAS-002，SR-SAAS-002 §6.3）。

默认值集合（新租户开箱可用）+ 差异项清单（kind=required 必填）。
「单一来源」决策（SR §2 决策②）：默认值以既有配置默认为模板源入库发布，
避免两套默认漂移；复制即生成租户私有 app_config 行，后续调整走运营后台
配置接口免发版（NFR-MAI-02）。

kind 语义：default 开箱默认 / required 差异项必填（向导第④步强制显式确认）/
advanced 高级可后调。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 差异项必填键（SR §6.3：分级规则 / 风控日操作上限 / A 级跟进节奏）
REQUIRED_KEYS: tuple[str, ...] = (
    "strategy.grade.thresholds",
    "risk.max_ops_per_day",
    "strategy.followup.cadence.A",
)


@dataclass(frozen=True)
class TemplateEntry:
    """模板条目（tenant_config_template 行的进程内形态）。"""

    scope: str
    cfg_key: str
    default_value: Any
    kind: str = "default"           # default | required | advanced
    description: str = ""
    range_hint: str = ""


@dataclass(frozen=True)
class AppConfigRow:
    """租户级配置行（app_config 行的进程内形态，updated_by=onboarding）。"""

    tenant_id: int
    scope: str
    cfg_key: str
    cfg_value: Any
    updated_by: str = "onboarding"


#: 内置模板库（demo 16 键含 3 差异项；数值口径以 SR-SC-003 §7 / SR-AG-005 §7 为模板源）
DEFAULT_TEMPLATES: tuple[TemplateEntry, ...] = (
    TemplateEntry("strategy", "strategy.grade.thresholds", {"A": 80, "B": 60}, "required",
                  "客户分级规则（A/B 线，按询盘质量分）", "0-100 分数对"),
    TemplateEntry("risk", "risk.max_ops_per_day", 200, "required",
                  "单账号风控日操作上限", "10-1000"),
    TemplateEntry("strategy", "strategy.followup.cadence.A", "1h", "required",
                  "A 级客户跟进节奏", "15m-24h"),
    TemplateEntry("strategy", "strategy.followup.cadence.B", "4h", "default",
                  "B 级客户跟进节奏", "1h-72h"),
    TemplateEntry("strategy", "strategy.reply.tone", "professional", "default",
                  "回复语气基调", "professional|friendly"),
    TemplateEntry("strategy", "strategy.quote.allow_discount_pct", 5, "advanced",
                  "AI 报价可让利幅度（%）", "0-15"),
    TemplateEntry("kb", "kb.top_k", 5, "default", "检索召回条数（FR-KB-030）", "1-20"),
    TemplateEntry("kb", "kb.rerank", True, "default", "是否二阶段重排", "bool"),
    TemplateEntry("kb", "kb.score_threshold", 0.35, "advanced",
                  "命中分数阈值（低于转人工）", "0-1"),
    TemplateEntry("agent", "agent.reply_timeout_sec", 45, "default",
                  "回复可用性截止（中位数口径 §16.3）", "20-120"),
    TemplateEntry("agent", "agent.low_confidence_handoff", True, "default",
                  "低置信度兜底转人工（FR-AG-009）", "bool"),
    TemplateEntry("agent", "agent.max_turns_per_session", 20, "advanced",
                  "单会话最大轮数", "5-100"),
    TemplateEntry("site", "site.widget.position", "right", "default",
                  "独立站 SDK 气泡位置（FR-SITE-001）", "left|right"),
    TemplateEntry("site", "site.widget.greeting", "Hi, may I help you?", "default",
                  "SDK 招呼语（可被白标覆盖）", "≤200 字符"),
    TemplateEntry("saas", "saas.onboarding.kb_retry_max", 3, "advanced",
                  "初始化单文件失败重试上限", "0-5"),
    TemplateEntry("saas", "saas.onboarding.progress_cache_sec", 60, "advanced",
                  "进度缓存 TTL（防轮询读放大）", "10-600"),
)


class TemplateService:
    """默认模板库：validate_required / copy_to_tenant / defaults_for_form / copied_rows。"""

    def __init__(self, entries: tuple[TemplateEntry, ...] = DEFAULT_TEMPLATES) -> None:
        self.entries: tuple[TemplateEntry, ...] = entries
        self._copied: dict[int, list[AppConfigRow]] = {}  # tenant_id → 复制行（demo 账本）

    @property
    def required_keys(self) -> tuple[str, ...]:
        """差异项必填键清单。"""
        return tuple(e.cfg_key for e in self.entries if e.kind == "required")

    def validate_required(self, payload: dict) -> None:
        """差异项必填校验：缺失即 40001 并列完整清单（向导第④步强制显式确认）。"""
        from services.saas.core.onboarding.wizard import OnboardingError  # 防循环局部导入

        missing = [k for k in self.required_keys if k not in payload]
        if missing:
            raise OnboardingError(40001, f"差异项必填：{missing}")

    def copy_to_tenant(self, tenant_id: int, overrides: dict) -> list[AppConfigRow]:
        """模板复制生成租户级 app_config 行：required 键取覆盖值，其余取模板默认。"""
        rows: list[AppConfigRow] = []
        for t in self.entries:
            value = overrides.get(t.cfg_key, t.default_value)
            rows.append(AppConfigRow(tenant_id=tenant_id, scope=t.scope,
                                     cfg_key=t.cfg_key, cfg_value=value))
        self._copied[tenant_id] = rows
        return list(rows)

    def copied_rows(self, tenant_id: int) -> list[AppConfigRow]:
        """已复制行查询（测试/演练断言用）。"""
        return list(self._copied.get(tenant_id, []))

    def defaults_for_form(self) -> list[dict]:
        """模板预填视图（GET /templates/defaults）：向导第④步表单回填。"""
        return [{"scope": t.scope, "cfg_key": t.cfg_key,
                 "default_value": t.default_value, "kind": t.kind,
                 "description": t.description, "range_hint": t.range_hint}
                for t in self.entries]
