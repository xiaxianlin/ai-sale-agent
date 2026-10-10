"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-06 数据不出域——LLM 出域网关骨架（平台基座实现）。

依据：docs/07-实现方案/NFR/SR-NFR-SEC-06-数据不出域.md §6.2（LLMEgressGateway）
与 docs/02-设计文档/06-安全与合规设计.md §6（LLM 边界：发送内容限「已脱敏消息文本 +
检索后的知识片段」，客户 PII 组装 Prompt 前二次过滤）。

send 管线（本层骨架）：
① 内容过滤管线——可插拔 ContentFilter 协议 + 注册机制：PII 过滤器接口仅在本层定义协议，
   真实现由 services 层 cleaning.masking 注入（复用 NFR-SEC-01 MaskingEngine，决策 D3）；
   内置可配敏感词表过滤器（warn 级替换 [REDACTED] 计数 / block 级剔除整块）；
② 过滤后内容快照留存——SnapshotStore 内存存储接口（生产形态为 SHA-256 + AES-256-GCM
   密文入 egress_snapshot 表，KMS 信封加密随服务层落地，决策 D4）；
③ 经 EgressGuard.assert_egress 断言目标合法（fail-closed：白名单外 / OI-09 未确认即拒，
   决策 D1，无「先发后审」路径）；
④ 出域 HTTP 发出——HTTP 客户端注入（协议 LlmHttpClient），测试用桩，生产对接 DeepSeek-V3。

偏差说明：SR §6.2 的 async complete() / 超时重试降级随服务层落地；本骨架为同步 send()，
管线顺序与拦截语义与 SR 一致（①过滤 → ②快照 → ③断言 → ④发出）。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from foundation.net.egress import (
    EgressCallRecord,
    EgressGuard,
    EgressViolation,
    content_digest,
)

EGRESS_SENSITIVE_BLOCKED = "EGRESS-1004"
"""敏感词 block 级命中且整段剔除后无有效内容（SR §5 错误码）。"""

REDACTED_MARK = "[REDACTED]"
"""warn 级敏感词替换标记（SR §6.2 c）。"""


# --------------------------------------------------------------------------- 内容过滤协议（D3）


@dataclass(frozen=True)
class FilterOutcome:
    """单个过滤器的处理结果：过滤后文本 + 命中计数 + 命中词 + 是否整块剔除。"""

    text: str
    hits: int = 0
    hit_words: tuple[str, ...] = ()
    blocked: bool = False


class ContentFilter(Protocol):
    """可插拔内容过滤器协议（send 前置清洗管线的统一接口）。

    PII 过滤器的真实现由 services 层 cleaning.masking 注入（NFR-SEC-01 MaskingEngine，
    同一规则集与版本，规则分叉视为违规）；本层仅提供协议与注册机制。
    """

    name: str
    """过滤器名称（进入 filter_hits 汇总的键）。"""

    def apply(self, text: str) -> FilterOutcome: ...


def _compile_words(words: tuple[str, ...]) -> re.Pattern[str] | None:
    """词表编译为大小写不敏感的交替正则；长词优先，避免短词截断长词。"""
    if not words:
        return None
    ordered = sorted(words, key=len, reverse=True)
    return re.compile("|".join(re.escape(w) for w in ordered), re.IGNORECASE)


class SensitiveWordFilter:
    """内置敏感词表过滤器（可配词表，SR §7 egress.llm.sensitive_words）。

    - warn 级：命中替换为 [REDACTED] 并计数（不阻断）；
    - block 级：命中即判定该块整块剔除（blocked=True），由网关丢弃该块；
      全部块被剔除时网关抛 EGRESS-1004（SR §5 / §9 test_scrub_sensitive_words）。
    """

    name = "sensitive_words"

    def __init__(self, warn_words: Iterable[str] = (), block_words: Iterable[str] = ()) -> None:
        self._warn_words: tuple[str, ...] = tuple(w for w in dict.fromkeys(warn_words) if w)
        self._block_words: tuple[str, ...] = tuple(w for w in dict.fromkeys(block_words) if w)
        self._warn_re = _compile_words(self._warn_words)
        self._block_re = _compile_words(self._block_words)

    @property
    def warn_words(self) -> tuple[str, ...]:
        return self._warn_words

    @property
    def block_words(self) -> tuple[str, ...]:
        return self._block_words

    @classmethod
    def from_config(cls, config: Mapping[str, Iterable[str]] | None) -> SensitiveWordFilter:
        """从配置构造（形如 {"warn": [...], "block": [...]}，SR §7）。"""
        cfg = config or {}
        return cls(cfg.get("warn", ()), cfg.get("block", ()))

    def apply(self, text: str) -> FilterOutcome:
        if self._block_re is not None:
            block_matches = self._block_re.findall(text)
            if block_matches:
                return FilterOutcome(
                    text="",
                    hits=len(block_matches),
                    hit_words=tuple(sorted({m.lower() for m in block_matches})),
                    blocked=True,
                )
        hits, cleaned, hit_words = 0, text, ()
        if self._warn_re is not None:
            warn_matches = self._warn_re.findall(text)
            hits = len(warn_matches)
            hit_words = tuple(sorted({m.lower() for m in warn_matches}))
            cleaned = self._warn_re.sub(REDACTED_MARK, text)
        return FilterOutcome(text=cleaned, hits=hits, hit_words=hit_words, blocked=False)


# --------------------------------------------------------------------------- 快照留存接口（D4）


class ContentSnapshot(BaseModel):
    """出域内容快照（egress_snapshot 的进程内形态：SHA-256 + 过滤后内容 + 时间）。"""

    model_config = ConfigDict(frozen=True)

    sha256: str
    """过滤后出域明文的 SHA-256（与调用留痕 content_sha256 关联）。"""

    size: int
    created_at: datetime
    content: str
    """内存形态保留过滤后明文；生产形态为 AES-256-GCM 密文（KMS 信封加密，D4）。"""


class SnapshotStore(Protocol):
    """过滤后内容快照存储接口（内存实现见 InMemorySnapshotStore；生产为 PG + KMS）。"""

    def save(self, content: str) -> ContentSnapshot: ...

    def get(self, sha256: str) -> ContentSnapshot | None: ...

    def count(self) -> int: ...

    def iter_all(self) -> tuple[ContentSnapshot, ...]: ...


class InMemorySnapshotStore:
    """快照内存实现：按 sha256 去重留存（审计追溯与去重口径与 SR §4 一致）。"""

    def __init__(self) -> None:
        self._items: dict[str, ContentSnapshot] = {}
        self._lock = threading.Lock()

    def save(self, content: str) -> ContentSnapshot:
        snapshot = ContentSnapshot(
            sha256=content_digest(content),
            size=len(content.encode("utf-8")),
            created_at=datetime.now(timezone.utc),
            content=content,
        )
        with self._lock:
            self._items.setdefault(snapshot.sha256, snapshot)
        return snapshot

    def get(self, sha256: str) -> ContentSnapshot | None:
        with self._lock:
            return self._items.get(sha256)

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def iter_all(self) -> tuple[ContentSnapshot, ...]:
        with self._lock:
            return tuple(self._items.values())


# --------------------------------------------------------------------------- HTTP 客户端协议


class LlmHttpClient(Protocol):
    """LLM 出域 HTTP 客户端协议（依赖注入；生产对接 DeepSeek-V3，测试用桩）。

    同步形态为骨架约定；服务层异步/超时/重试/降级（02 选型 §5）由其适配实现承载。
    """

    def post(self, url: str, body: Mapping[str, Any]) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- 网关配置与结果


class LlmGatewayConfig(BaseModel):
    """LLM 出域网关配置（SR §7 egress.llm.* 的进程内子集，NFR-MAI-02 配置化）。"""

    model_config = ConfigDict(frozen=True)

    dest_code: str = "deepseek_api"
    """目的地编码（须已在 EgressGuard 注册且 enabled）。"""

    url: str = "https://api.deepseek.com"
    """LLM 出域目标 URL（host/port 须与注册表一致，路径不参与校验）。"""

    purpose: str = "llm_chat"
    """默认声明用途（send 未显式指定时使用；须在目的地 purposes 内）。"""

    sensitive_words: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: {"warn": (), "block": ()}
    )
    """敏感词表：warn 替换计数 / block 整块剔除（SR §7 egress.llm.sensitive_words）。"""

    @field_validator("sensitive_words")
    @classmethod
    def _check_sensitive_words(cls, value: Mapping[str, Iterable[str]]) -> dict[str, tuple[str, ...]]:
        unknown = set(value) - {"warn", "block"}
        if unknown:
            raise ValueError(f"sensitive_words 仅支持 warn/block 两级，收到 {sorted(unknown)}")
        return {level: tuple(value.get(level, ())) for level in ("warn", "block")}


class LlmEgressResult(BaseModel):
    """send 返回：响应文本 + 原始响应 + 快照摘要 + 留痕序号 + 过滤命中汇总。"""

    model_config = ConfigDict(frozen=True)

    text: str
    response: dict[str, Any]
    content_sha256: str
    log_seq: int
    filter_hits: dict[str, int] = Field(default_factory=dict)
    """各过滤器命中计数（键为过滤器 name，供 field_digest / 月度抽检）。"""


# --------------------------------------------------------------------------- LLM 出域网关


class LLMEgressGateway:
    """LLM 唯一出域口（骨架）：过滤管线 → 快照留存 → EgressGuard 断言 → 注入客户端发出。

    拦截语义（fail-closed，D1）：
    - 敏感词 block 级全剔除 → EGRESS-1004（留痕 blocked 后抛 EgressViolation）；
    - 目标白名单外 / OI-09 未确认 / 用途越界 → 由 EgressGuard.assert_egress 抛
      EgressViolation（EGRESS-1001/1002/1003），HTTP 客户端不会被调用。
    """

    def __init__(
        self,
        guard: EgressGuard,
        http_client: LlmHttpClient,
        *,
        config: LlmGatewayConfig | None = None,
        snapshot_store: SnapshotStore | None = None,
        filters: Iterable[ContentFilter] = (),
    ) -> None:
        self._guard = guard
        self._http = http_client
        self.cfg: LlmGatewayConfig = config or LlmGatewayConfig()
        self.snapshots: SnapshotStore = snapshot_store or InMemorySnapshotStore()
        self._filters: list[ContentFilter] = list(filters)
        self._builtin = SensitiveWordFilter(
            warn_words=self.cfg.sensitive_words.get("warn", ()),
            block_words=self.cfg.sensitive_words.get("block", ()),
        )

    def register_filter(self, content_filter: ContentFilter) -> None:
        """注册外部内容过滤器（PII 过滤真实现由 services 层 cleaning.masking 注入，D3）。

        追加进管线且先于内置敏感词表执行（对齐 SR §6.2 清洗顺序 a→c：
        PII 二次过滤 → 身份脱替 → 敏感词表）。
        """
        self._filters.append(content_filter)

    @property
    def filters(self) -> tuple[ContentFilter, ...]:
        """当前管线（外部过滤器在前，内置敏感词表在后）。"""
        return (*self._filters, self._builtin)

    def send(
        self,
        content: str | Sequence[str],
        *,
        purpose: str | None = None,
        caller: str = "agent-core",
        trace_id: str = "",
    ) -> LlmEgressResult:
        """出域发送：①过滤 → ②快照 → ③断言 → ④发出（拦截即抛，绝不「先发后审」）。

        content 支持整段字符串或 Prompt 块列表（block 级敏感词按「整块剔除」处理，
        SR §6.2 c）；返回 LlmEgressResult（含留痕序号与过滤命中汇总）。
        """
        blocks: list[str] = [content] if isinstance(content, str) else [str(b) for b in content]
        purpose = purpose or self.cfg.purpose
        cleaned, hits, dropped = self._run_pipeline(blocks)
        original_text = "\n".join(blocks)
        if dropped and not cleaned:
            # 敏感词 block 级命中：整段剔除后无有效内容 → EGRESS-1004（SR §5）
            detail = "敏感词 block 级命中：整段剔除后无有效内容"
            self._guard.record_call(
                url=self.cfg.url,
                purpose=purpose,
                verdict="blocked",
                code=EGRESS_SENSITIVE_BLOCKED,
                detail=detail,
                content=original_text,
                caller=caller,
                trace_id=trace_id,
            )
            raise EgressViolation(EGRESS_SENSITIVE_BLOCKED, detail, self.cfg.url)
        if not cleaned:
            raise ValueError("出域内容为空，拒绝发出（fail-closed）")
        payload_text = "\n".join(cleaned)
        snapshot = self.snapshots.save(payload_text)  # ② 过滤后内容快照留存（D4）
        # ③ EgressGuard 断言目标合法（fail-closed；通过时落 allowed 留痕）
        record: EgressCallRecord = self._guard.assert_egress(
            self.cfg.url, purpose, content=payload_text, caller=caller, trace_id=trace_id
        )
        # ④ 发出（HTTP 客户端注入；上游异常原样传播，超时/降级语义由服务层实现承载）
        response = self._http.post(self.cfg.url, {"content": payload_text, "purpose": purpose})
        return LlmEgressResult(
            text=str(response.get("text", "")),
            response=dict(response),
            content_sha256=snapshot.sha256,
            log_seq=record.seq,
            filter_hits=hits,
        )

    def _run_pipeline(self, blocks: list[str]) -> tuple[list[str], dict[str, int], int]:
        """执行过滤管线：返回 (保留块, 各过滤器命中计数, 被剔除块数)。

        任一过滤器对某块返回 blocked=True 即整块剔除（后续过滤器不再执行该块）；
        过滤后为空白串的块同样不进入出域载荷。
        """
        kept: list[str] = []
        hits: dict[str, int] = {}
        dropped = 0
        for block in blocks:
            current = block
            block_dropped = False
            for content_filter in self.filters:
                outcome = content_filter.apply(current)
                hits[content_filter.name] = hits.get(content_filter.name, 0) + outcome.hits
                if outcome.blocked:
                    block_dropped = True
                    break
                current = outcome.text
            if block_dropped:
                dropped += 1
            elif current.strip():
                kept.append(current)
        return kept, hits, dropped
