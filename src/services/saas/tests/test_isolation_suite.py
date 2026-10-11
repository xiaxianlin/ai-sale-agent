# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""隔离测试套件单元测试（NFR-SEC-07 §6.1-6.5/§9；TDD 阶段①②，五类场景齐备）。

覆盖：API 越权矩阵（A 遍历 B 全 403/404 且无 canary 泄露）、canary 检索零泄漏
（含对照组防空库假阴性）、伪造 tenant_id 参数无效（三种注入形态）、存储前缀
隔离（对象前缀 + 键空间前缀）、报告生成器（可复现三要素/覆盖矩阵/漏洞清单/
签字栏）、套件元测试（缺陷注入敏感 + 假阴性防护——套件能抓真漏洞）。
验收口径映射：SRS §16.6①「多租户隔离测试通过，出具隔离性测试报告」。

实现方案：docs/07-实现方案/NFR/SR-NFR-SEC-07-多租户隔离.md §6/§9
"""

from __future__ import annotations

from pathlib import Path

from tools.saas.isolation_suite import (
    CANARY_PREFIX,
    IsolationTestSuite,
    ReportGenerator,
    TENANT_A_ID,
    TENANT_B_ID,
    build_default_env,
)


def fresh_suite(leak_mode: str = "isolated"):
    """构建默认两租户环境（A=101 常规 / B=202 含 canary）并装配套件。"""
    env = build_default_env(leak_mode=leak_mode)
    return env, IsolationTestSuite(env)


def find_cases(run, layer_prefix: str):
    return [r for r in run.results if r.case_id.startswith(layer_prefix)]


def test_suite_all_pass_on_isolated_env() -> None:
    """全绿：隔离正常的默认环境 → verdict=PASS，四组用例（API/VECTOR/FORGE/STORAGE）齐备。"""
    env, suite = fresh_suite()
    run = suite.run()
    assert run.verdict() == "PASS"
    layers = {r.layer for r in run.results}
    assert {"API", "VECTOR", "FORGE", "STORAGE"} <= layers
    assert all(r.verdict == "PASS" for r in run.results)


def test_api_matrix_covers_all_b_resources() -> None:
    """API 越权矩阵完备：B 全部资源 uid × GET/PUT/DELETE 逐条遍历，全 403/404。"""
    env, suite = fresh_suite()
    run = suite.run()
    api_cases = find_cases(run, "API-")
    b_uids = [item.uid for item in env.b_items()]
    assert len(api_cases) == len(b_uids) * 3
    assert all(r.verdict == "PASS" for r in api_cases)
    assert all(r.actual in {"403", "404"} for r in api_cases)


def test_canary_control_group_zero_leakage() -> None:
    """canary 检索：A 零命中且 B 对照组 ≥1（证明数据在库，排除空库假阴性）。"""
    env, suite = fresh_suite()
    run = suite.run()
    vec = find_cases(run, "VEC-")
    assert len(vec) == 1 and vec[0].verdict == "PASS"
    assert vec[0].evidence["a_hits"] == 0
    assert vec[0].evidence["b_hits"] >= 1


def test_forged_tenant_id_param_ignored() -> None:
    """伪造 tenant_id 参数无效：body/query/header 三种注入形态全部被忽略。"""
    env, suite = fresh_suite()
    run = suite.run()
    forge_cases = find_cases(run, "FORGE-")
    assert len(forge_cases) == 3
    assert all(r.verdict == "PASS" for r in forge_cases)
    assert all(CANARY_PREFIX not in str(r.evidence.get("body_head", "")) for r in forge_cases)


def test_storage_prefix_isolation() -> None:
    """存储前缀隔离：A 写入对象全部落在 tenants/{A}/ 前缀；跨前缀读取被拒；
    键空间（Redis 形态）键全部带 t:{A}: 前缀，无前缀逃逸。"""
    env, suite = fresh_suite()
    run = suite.run()
    storage_cases = find_cases(run, "STORAGE-")
    assert storage_cases and all(r.verdict == "PASS" for r in storage_cases)
    assert all(k.startswith(f"tenants/{TENANT_A_ID}/") for k in env.a_object_keys())
    assert env.cross_prefix_read_status() in {403, 404}
    assert all(k.startswith(f"t:{TENANT_A_ID}:") for k in env.a_cache_keys())


def test_suite_detects_injected_leak() -> None:
    """元测试①缺陷注入敏感性（SR §9）：故意放开隔离（模拟 RLS 失效）→
    套件必须 FAIL——防止「永绿」套件。"""
    env, suite = fresh_suite(leak_mode="open")
    run = suite.run()
    assert run.verdict() == "FAIL"
    assert run.stats()["failed"] >= 1


def test_false_negative_guard_on_empty_b() -> None:
    """元测试②空库假阴性防护（SR §9）：B 的 canary 数据缺失时对照组零命中 →
    VEC 用例判 FAIL（数据缺失即证据无效），绝不静默 PASS。"""
    env, suite = fresh_suite()
    env.remove_b_canary()
    run = suite.run()
    vec = find_cases(run, "VEC-")
    assert len(vec) == 1 and vec[0].verdict == "FAIL"
    assert run.verdict() == "FAIL"


def test_report_generator_renders_deliverable(tmp_path: Path) -> None:
    """报告生成器：MD 含执行概要/可复现三要素（环境+命令行+种子指纹）/覆盖矩阵/
    漏洞清单（FAIL 场景）/双方签字栏（NFR-SEC-07 证据效力要求）。"""
    env, suite = fresh_suite(leak_mode="open")
    run = suite.run()
    report_path = ReportGenerator(out_dir=tmp_path).render(run)
    text = report_path.read_text(encoding="utf-8")
    assert report_path.suffix == ".md" and report_path.exists()
    for section in ("执行概要", "可复现三要素", "覆盖矩阵", "漏洞清单", "签字栏", "甲方验收代表"):
        assert section in text
    assert "FAIL" in text  # 漏洞清单随 FAIL 用例生成
    assert run.seed_fp in text  # 种子数据集指纹（可复现要素②）
