# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""通用能力剥离守护（FR-SAAS-007 §6.1-6.3，SR-SAAS-007 §4/§5/§6）。

- 依赖方向规则（SR §6.1 D1）：foundation 禁止反向 import services；产品侧
  禁止静态/动态引用 tenant-content（AST + 字符串双通道）；
- 出厂种子审计（SR §6.2）：归属（tenant_id 只允许 0=平台默认域）/ 夹带
  （键必须登记于产品默认集）/ 甲方关键词逐词扫描（清单参数注入，COM-IP-06
  不入产品包）；
- 模板三级加载链（SR §6.3）：租户覆盖 > 平台默认域（tenant_id=0）> 代码内置，
  三级全缺失 fail-fast；覆盖键必须登记于产品默认集（禁止私增）。
剥离不改变任何资产产权归属（COM-IP-01）：仅为部署形态与产品化边界要求。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

#: 平台默认域标识（出厂种子全部挂该域，禁止改作租户 id，SR §7）
DEFAULT_DOMAIN_TENANT_ID: int = 0

#: 出厂种子统一表（app_config 默认域行；scope 扩展 'prompt'，SR §4）
SEED_TABLE: str = "app_config"

#: 依赖方向违例规则名（SR §6.1 D1/D1-dyn）
_RULE_FOUNDATION_TO_SERVICES = "D1-foundation-imports-services"
_RULE_STATIC_TENANT_CONTENT = "D1-tenant-content"
_RULE_DYNAMIC_TENANT_CONTENT = "D1-dynamic-tenant-content"

#: 内容侧标识（目录/模块名两种形态；产品侧引用即违例）
_TENANT_CONTENT_TOKENS: tuple[str, ...] = ("tenant_content", "tenant-content")

#: 扫描器自身豁免（SR §6.3 白名单同源口径：守护工具/扫描器必须引用内容侧标识
#: 才能实施扫描，引用本身不构成依赖；豁免按文件路径后缀判定）
_SCAN_SELF_EXEMPT: tuple[str, ...] = (
    "core/separation/guard.py",
    "tools/saas/zero_code_check.py",
    "tools/saas/isolation_suite.py",
)


def _is_self_exempt(relative_path: str) -> bool:
    """内容侧规则豁免：扫描器自身文件与测试目录（用例需构造违例样例，
    引用内容侧标识是扫描/测试的前提而非依赖；SR §6.3 白名单同源口径）。"""
    if any(relative_path.endswith(suffix) or suffix in relative_path
           for suffix in _SCAN_SELF_EXEMPT):
        return True
    return "/tests/" in relative_path.replace("\\", "/")

_DYNAMIC_REF_RE = re.compile(r"(importlib|__import__)")


@dataclass(frozen=True)
class Violation:
    """剥离守护违例（规则/文件/行/说明——报告与 CI 阻断的统一载体）。"""

    rule: str
    file: str
    line: int
    detail: str


@dataclass(frozen=True)
class SeedRow:
    """出厂种子行（SEED_MANIFEST 条目 + 行内容；审计权威，SR §4）。"""

    scope: str
    key: str
    body: str
    table: str = SEED_TABLE
    tenant_id: int = DEFAULT_DOMAIN_TENANT_ID


# --------------------------------------------------------------------------- 依赖方向扫描


def _iter_imports(py: Path):
    """AST 解析一个 .py 的 import/from 语句，产出 (行号, 顶层模块名, 原语句)。"""
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0], f"import {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module.split(".")[0], f"from {node.module} import …"


def _foundation_roots(root: Path) -> list[Path]:
    """foundation 基座目录候选（src 布局与平铺布局都识别）。"""
    return [p for p in (root / "src" / "foundation", root / "foundation") if p.is_dir()]


def _product_roots(root: Path) -> list[Path]:
    """产品侧目录候选（foundation + services；tools 由调用方按需追加）。"""
    candidates = [root / "src" / "foundation", root / "foundation",
                  root / "src" / "services", root / "services"]
    return [p for p in candidates if p.is_dir()]


def scan_dependency_direction(root: Path) -> list[Violation]:
    """依赖方向扫描（SR §6.1）：
    ① [D1] foundation 任何文件 import services（分层倒置，阻断）；
    ② [D1] 产品侧静态 import tenant_content；
    ③ [D1-dyn] 产品侧动态字符串引用 tenant_content（importlib/__import__ 兜底）。
    """
    violations: list[Violation] = []
    for base in _foundation_roots(root):
        for py in sorted(base.rglob("*.py")):
            for lineno, top, stmt in _iter_imports(py):
                if top == "services":
                    violations.append(
                        Violation(
                            _RULE_FOUNDATION_TO_SERVICES, str(py.relative_to(root)), lineno,
                            f"foundation 反向依赖 services：{stmt}（分层倒置，禁止）",
                        )
                    )
    for base in _product_roots(root):
        for py in sorted(base.rglob("*.py")):
            relative = str(py.relative_to(root))
            if _is_self_exempt(relative):
                continue  # 扫描器自身豁免（引用内容侧标识是扫描的前提，非依赖）
            text = py.read_text(encoding="utf-8", errors="replace")
            for lineno, top, stmt in _iter_imports(py):
                if top in _TENANT_CONTENT_TOKENS:
                    violations.append(
                        Violation(_RULE_STATIC_TENANT_CONTENT, relative, lineno,
                                  f"产品侧引用内容侧：{stmt}")
                    )
            for line_no, line in enumerate(text.splitlines(), start=1):
                if _DYNAMIC_REF_RE.search(line) and any(tok in line for tok in _TENANT_CONTENT_TOKENS):
                    violations.append(
                        Violation(_RULE_DYNAMIC_TENANT_CONTENT, relative, line_no,
                                  "疑似动态引用租户内容侧（importlib/__import__）")
                    )
    return violations


# --------------------------------------------------------------------------- 出厂内置模板与种子


def _builtin_templates() -> dict[tuple[str, str], str]:
    """代码内置模板（出厂种子的同源生成源，SR §6.3 ③；全部为通用骨架，
    零甲方业务信息——甲方优化版本一律挂租户覆盖行，不进产品）。"""
    return {
        ("prompt", "reply.system"): (
            "你是外贸销售助理。仅依据「引用知识」回复；引用缺失时如实说明并转人工；"
            "禁止编造价格、交期与认证信息。"
        ),
        ("prompt", "followup.round1"): (
            "基于上轮询盘要点生成首轮跟进：确认需求、补充一个产品价值点、"
            "给出一个明确下一步动作。语气专业、简洁。"
        ),
        ("strategy", "followup.default"): (
            "首次跟进 24h 内；未回复按 3/7/14 天梯度重试；三轮未回复降频转 nurture 池。"
        ),
        ("risk", "sensitive.base"): (
            "平台基础敏感维度：价格承诺、交期承诺、合同条款、付款方式、质检口径。"
        ),
        ("training", "personas.base"): (
            "模拟客户基础类型：询价型、比价型、认证关注型、物流关注型、售后关注型。"
        ),
    }


BUILTIN_TEMPLATES: dict[tuple[str, str], str] = _builtin_templates()


def builtin_seed_rows() -> list[SeedRow]:
    """出厂种子 = 内置模板同源展开（tenant_id 全 0；启动期可校验两套默认不漂移）。"""
    return [
        SeedRow(scope=scope, key=key, body=body)
        for (scope, key), body in sorted(BUILTIN_TEMPLATES.items())
    ]


def audit_factory_seeds(
    rows: Iterable[SeedRow],
    registered_keys: Iterable[tuple[str, str]],
    keywords: Iterable[str] = (),
) -> list[Violation]:
    """出厂数据包种子审计（SR §6.2，任何一条不满足即违例）：
    ① 归属：tenant_id 只允许 0（甲方数据 tenant_id=1 夹带即 [seed-tenant]）；
    ② 夹带：键必须登记于产品默认集（未登记 = [seed-unlisted]）；
    ③ 关键词：正文命中甲方关键词清单 → [keyword-hit]（清单经参数注入，COM-IP-06）。
    """
    registered = set(registered_keys)
    words = [w for w in keywords if w]
    violations: list[Violation] = []
    for row in rows:
        if row.tenant_id != DEFAULT_DOMAIN_TENANT_ID:
            violations.append(
                Violation("seed-tenant", f"{row.table}:{row.scope}/{row.key}", 0,
                          f"种子归属违规 tenant_id={row.tenant_id}（出厂只允许默认域 0）")
            )
        if (row.scope, row.key) not in registered:
            violations.append(
                Violation("seed-unlisted", f"{row.table}:{row.scope}/{row.key}", 0,
                          "种子未登记于产品默认集（疑似夹带）")
            )
        for word in words:
            if word in row.body:
                violations.append(
                    Violation("keyword-hit", f"{row.scope}/{row.key}", 0,
                              f"出厂种子命中甲方关键词: {row.scope}/{row.key}:{word}")
                )
    return violations


# --------------------------------------------------------------------------- 模板三级加载链


class TemplateMissingError(Exception):
    """模板三级全缺失（fail-fast：禁止静默降级为空 Prompt，SR §6.3）。"""


class TemplateKeyUnknownError(Exception):
    """覆盖键不在产品默认集内（禁止私增，SR §5 TEMPLATE_KEY_UNKNOWN/404）。"""


class TemplateLoader:
    """三级加载链：租户覆盖（tenant_id=N 行）> 平台默认（tenant_id=0 行）>
    代码内置（BUILTIN_TEMPLATES）。加载结果按 TTL 缓存；覆盖行变更即时失效。"""

    def __init__(
        self,
        *,
        defaults: Mapping[tuple[str, str], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        cache_ttl_sec: int = 300,
    ) -> None:
        self._defaults = defaults  # None 时默认域仅内置兜底（出厂种子同源）
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = cache_ttl_sec
        self._overrides: dict[tuple[int, str, str], str] = {}
        self._cache: dict[tuple[int, str, str], tuple[datetime, str]] = {}

    def _known_key(self, scope: str, key: str) -> bool:
        if (scope, key) in BUILTIN_TEMPLATES:
            return True
        if self._defaults is not None and self._defaults.get((scope, key)) is not None:
            return True
        return False

    def put_override(self, tenant_id: int, scope: str, key: str, body: str) -> None:
        """写入租户覆盖行（键必须在产品默认集内，SR §5；变更即时失效缓存）。"""
        if not self._known_key(scope, key):
            raise TemplateKeyUnknownError(f"模板键不在产品默认集内: {scope}/{key}")
        cache_key = (int(tenant_id), scope, key)
        self._overrides[cache_key] = body
        self._cache.pop(cache_key, None)  # 覆盖变更 → 失效对应键（SR §6.3 末段）

    def _default_body(self, scope: str, key: str) -> str | None:
        if self._defaults is None:
            return None
        return self._defaults.get((scope, key))

    def load(self, tenant_id: int, scope: str, key: str) -> str:
        """按三级链加载（缓存命中先回）；三级全缺失抛 TemplateMissingError。"""
        cache_key = (int(tenant_id), scope, key)
        hit = self._cache.get(cache_key)
        if hit is not None:
            cached_at, body = hit
            if (self._clock() - cached_at).total_seconds() < self._ttl:
                return body
            self._cache.pop(cache_key, None)  # TTL 过期回源
        override = self._overrides.get(cache_key)
        if override is not None:  # ① 租户覆盖（甲方优化版话术/参数）
            body = override
        else:  # ② 平台默认域（出厂种子行，对全部租户只读可见——域标识见上方查询常量）
            body = self._default_body(scope, key)
        if body is None:  # ③ 代码内置兜底（与出厂种子同源生成）
            body = BUILTIN_TEMPLATES.get((scope, key))
        if body is None:
            raise TemplateMissingError(f"模板缺失: {scope}/{key}")
        self._cache[cache_key] = (self._clock(), body)
        return body
