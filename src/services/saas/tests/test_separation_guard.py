# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""通用能力剥离守护单元测试（FR-SAAS-007 §6.1-6.3；TDD 阶段①②，五类场景齐备）。

覆盖：依赖方向规则（foundation 禁止 import services / 产品侧禁止引用 tenant-content，
含动态引用）、出厂种子审计（归属 tenant_id=0 / 夹带登记 / 甲方关键词零命中）、
模板三级加载链（租户覆盖 > 平台默认 > 代码内置，缺失 fail-fast、覆盖变更即时
失效、TTL 过期回源）。
验收口径映射：SRS §16.6「剥离验证：出厂产品不含甲方专属知识/话术」。

实现方案：docs/07-实现方案/SAAS/SR-SAAS-007-通用能力剥离.md §6.1/§6.2/§6.3/§9
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.saas.core.separation.guard import (
    BUILTIN_TEMPLATES,
    DEFAULT_DOMAIN_TENANT_ID,
    SeedRow,
    TemplateKeyUnknownError,
    TemplateLoader,
    TemplateMissingError,
    audit_factory_seeds,
    builtin_seed_rows,
    scan_dependency_direction,
)

T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- 依赖方向（D1/D2）


def test_dependency_direction_clean_repo(tmp_path: Path) -> None:
    """合规样例：foundation 文件只 import 标准库与自身 → 零违例。"""
    pkg = tmp_path / "foundation" / "security"
    pkg.mkdir(parents=True)
    (pkg / "context.py").write_text(
        "# 知识产权归甲方所有（COM-IP-01）\nfrom contextvars import ContextVar\n", encoding="utf-8"
    )
    assert scan_dependency_direction(tmp_path) == []


def test_dependency_direction_detects_foundation_imports_services(tmp_path: Path) -> None:
    """[D1] foundation 反向依赖 services → 检出并给出 文件:行（分层倒置阻断）。"""
    pkg = tmp_path / "foundation" / "bad"
    pkg.mkdir(parents=True)
    target = pkg / "leak.py"
    target.write_text(
        "# 知识产权归甲方所有（COM-IP-01）\nimport services.saas.api\n", encoding="utf-8"
    )
    violations = scan_dependency_direction(tmp_path)
    assert len(violations) == 1
    assert violations[0].rule == "D1-foundation-imports-services"
    assert violations[0].file.endswith("leak.py") and violations[0].line == 2


def test_content_side_dynamic_reference_detected(tmp_path: Path) -> None:
    """[D1-dyn] 产品侧动态引用 tenant-content（importlib 字符串）同样拦截（SR §6.1 ②）。"""
    pkg = tmp_path / "foundation" / "x"
    pkg.mkdir(parents=True)
    (pkg / "dyn.py").write_text(
        '# 知识产权归甲方所有（COM-IP-01）\nimport importlib\nimportlib.import_module("tenant_content.secrets")\n',
        encoding="utf-8",
    )
    rules = [v.rule for v in scan_dependency_direction(tmp_path)]
    assert "D1-dynamic-tenant-content" in rules


# --------------------------------------------------------------------------- 出厂种子审计（§6.2）


def test_builtin_seeds_clean_and_default_domain() -> None:
    """内置出厂种子：全部挂默认域 tenant_id=0 且对登记清单零违例（出厂即合规）。"""
    seeds = builtin_seed_rows()
    assert seeds, "出厂种子不得为空"
    assert all(row.tenant_id == DEFAULT_DOMAIN_TENANT_ID == 0 for row in seeds)
    registered = set(BUILTIN_TEMPLATES)
    assert audit_factory_seeds(seeds, registered, keywords=("模型X-900", "甲公司")) == []


def test_seed_ownership_violation_detected() -> None:
    """[seed-tenant] 种子行挂 tenant_id=1（甲方数据夹带进出厂包）→ 违例阻断。"""
    seeds = [
        SeedRow(scope="prompt", key="reply.system", body="通用骨架", tenant_id=1),
    ]
    violations = audit_factory_seeds(seeds, {("prompt", "reply.system")})
    assert [v.rule for v in violations] == ["seed-tenant"]


def test_seed_unlisted_detected() -> None:
    """[seed-unlisted] 未登记于产品默认集的键 → 夹带违例（禁止私增种子）。"""
    seeds = [SeedRow(scope="prompt", key="hidden.backdoor", body="x")]
    violations = audit_factory_seeds(seeds, set())
    assert [v.rule for v in violations] == ["seed-unlisted"]


def test_seed_keyword_hit_detected() -> None:
    """[keyword-hit] 种子正文命中甲方关键词 → 违例并列出 键:词（COM-IP-06 参数注入）。"""
    seeds = [SeedRow(scope="prompt", key="reply.system", body="欢迎选购 甲公司 的 模型X-900")]
    violations = audit_factory_seeds(seeds, {("prompt", "reply.system")}, keywords=("模型X-900",))
    assert len(violations) == 1
    assert violations[0].rule == "keyword-hit"
    assert "模型X-900" in violations[0].detail


# --------------------------------------------------------------------------- 模板三级加载链（§6.3）


class CountingDefaults:
    """带访问计数的默认域桩（TTL 回源用例观测「是否重新读默认域」）。"""

    def __init__(self, body: str) -> None:
        self.body = body
        self.reads = 0

    def get(self, key: tuple[str, str]) -> str | None:
        self.reads += 1
        return self.body


def test_template_chain_priority_override_over_default_over_builtin() -> None:
    """加载链优先级：租户覆盖 > 平台默认域 > 代码内置（SR §6.3 顺序固定）。"""
    loader = TemplateLoader(clock=lambda: T0)
    scope, key = "prompt", "reply.system"  # 内置键
    assert loader.load(2, scope, key) == BUILTIN_TEMPLATES[(scope, key)]  # ③ 内置兜底
    loader = TemplateLoader(
        defaults={(scope, key): "平台默认文"}, clock=lambda: T0
    )
    assert loader.load(2, scope, key) == "平台默认文"  # ② 默认域
    loader.put_override(2, scope, key, "租户覆盖文")
    assert loader.load(2, scope, key) == "租户覆盖文"  # ① 覆盖行最优先


def test_template_missing_fail_fast_and_unknown_override_key() -> None:
    """异常路径：三级全缺失 → TemplateMissingError（fail-fast，禁止静默降级空 Prompt）；
    覆盖键不在产品默认集内禁止私增 → TemplateKeyUnknownError（SR §5）。"""
    loader = TemplateLoader(clock=lambda: T0)
    with pytest.raises(TemplateMissingError):
        loader.load(2, "prompt", "no.such.key")
    with pytest.raises(TemplateKeyUnknownError):
        loader.put_override(2, "prompt", "no.such.key", "x")


def test_override_invalidation_and_ttl_expiry() -> None:
    """缓存：TTL 内命中缓存不回源；TTL 过期回源默认域（时钟注入验证）；
    覆盖变更即时生效（缓存失效）已由加载链用例覆盖。"""
    clock_now = T0
    defaults = CountingDefaults("平台默认文")
    loader = TemplateLoader(defaults=defaults, clock=lambda: clock_now, cache_ttl_sec=10)
    scope, key = "prompt", "reply.system"
    assert loader.load(2, scope, key) == "平台默认文"
    assert defaults.reads == 1
    clock_now = T0 + timedelta(seconds=5)
    assert loader.load(2, scope, key) == "平台默认文"
    assert defaults.reads == 1  # TTL 内命中缓存，不回源
    clock_now = T0 + timedelta(seconds=11)
    assert loader.load(2, scope, key) == "平台默认文"
    assert defaults.reads == 2  # TTL 过期后重新读默认域（回源）
