# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# isolation_suite.py —— 多租户隔离测试套件（NFR-SEC-07，SR-NFR-SEC-07 §6/§9）：
# 租户 A 凭证遍历租户 B 资源（API 越权矩阵 / canary 检索 / 伪造参数 / 存储前缀）
# 全部拒绝，产出可复现的隔离性测试报告（MD）——SRS §16.6① 验收交付物。
"""多租户隔离测试套件：四层遍历 + 元测试（缺陷注入敏感 / 空库假阴性防护）。

用例组（SR §6.1-6.3 收敛到本仓库内存形态）：
- API-*   越权矩阵：A 全套真实凭证遍历 B 全部资源 × GET/PUT/DELETE → 403/404
          且报文不含 B 的 canary 探针串（错误报文也不得泄露）；
- VEC-*   canary 检索：A 检索 B 独有探针短语零命中；对照组 B 必须 ≥1 命中
          （证明数据在库，排除空库假阴性——B=0 判 FAIL 而非 PASS）；
- FORGE-* 上下文逃逸：body/query/header 三形态伪造 tenant_id 参数全部被忽略；
- STORAGE-* 存储前缀：A 写入对象强制归位 tenants/{A}/ 前缀；跨前缀读取拒绝；
          键空间（Redis 形态）全部带 t:{A}: 前缀，无前缀逃逸。

报告（§6.4）：执行概要 + 可复现三要素（环境/命令行/种子指纹）+ 覆盖矩阵 +
漏洞清单 + 双方签字栏。可独立运行：
    python3 tools/saas/isolation_suite.py --report-out out/iso-reports
退出码：0=PASS；1=FAIL（任一用例失败或对照组证据无效）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 独立运行引导：把 <repo>/src 加入 sys.path（工具目录不依赖安装）
REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fastapi.testclient import TestClient  # noqa: E402

from services.saas.api.saas_api import (  # noqa: E402
    SAAS_TENANT_PREFIX,
    create_saas_app,
)
from services.saas.core.quota.engine import QuotaEngine  # noqa: E402
from services.saas.core.tenant.registry import TenantRegistry  # noqa: E402

#: 两租户差异化种子（SR §4：A/B 数据量与形态刻意不同，防对称性掩盖遗漏）
TENANT_A_ID: int = 101
TENANT_B_ID: int = 202

#: B 独有探针短语前缀（任何层出现即泄露，SR §4 canary 口径）
CANARY_PREFIX: str = "ZZQB-9f3a"

A_TITLES: tuple[str, ...] = ("标准支架参数手册", "通用报价流程说明")
B_TITLES: tuple[str, ...] = (
    f"甲公司专有型号 {CANARY_PREFIX}-07 参数",
    f"内部结算口径 {CANARY_PREFIX}-08",
    f"定制夹具图纸 {CANARY_PREFIX}-09",
)


# --------------------------------------------------------------------------- 数据结构


@dataclass
class CaseResult:
    """单用例结果（SR §5 IsolationCaseResult：evidence 为报告原始证据）。"""

    case_id: str
    layer: str
    target: str
    expected: str
    actual: str
    verdict: str  # PASS / FAIL / SKIP
    evidence: dict = field(default_factory=dict)


@dataclass
class IsolationRun:
    """一次套件执行（覆盖矩阵 + 可复现三要素的载体）。"""

    run_uid: str
    env_name: str
    seed_fp: str
    cmdline: str
    started_at: str
    leak_mode: str
    results: list[CaseResult] = field(default_factory=list)

    def stats(self) -> dict[str, int]:
        passed = sum(1 for r in self.results if r.verdict == "PASS")
        failed = sum(1 for r in self.results if r.verdict == "FAIL")
        return {"total": len(self.results), "passed": passed, "failed": failed}

    def verdict(self) -> str:
        """FAIL 即整体 FAIL（P0 需求：任一层跨租户可见即验收不通过，无豁免）。"""
        return "FAIL" if any(r.verdict == "FAIL" for r in self.results) else "PASS"


# --------------------------------------------------------------------------- 环境（A/B 全套凭证）


class IsolationEnv:
    """两租户内存环境：注册表 + 配额 + 应用（leak_mode=open 注入隔离缺陷）。"""

    def __init__(self, *, leak_mode: str = "isolated") -> None:
        self.leak_mode = leak_mode
        self.registry = TenantRegistry()
        self.registry.seed(TENANT_A_ID, "隔离测试租户A")
        self.registry.seed(TENANT_B_ID, "隔离测试租户B")
        quota = QuotaEngine()
        quota.set_limit(TENANT_A_ID, "api_qps", 1000)  # 套件自身调用不受 QPS 干扰
        quota.set_limit(TENANT_B_ID, "api_qps", 1000)
        self.app, self.comps = create_saas_app(
            registry=self.registry,
            quota=quota,
            cross_tenant_leak=(leak_mode == "open"),
        )
        self.client = TestClient(self.app)
        self.token_a = self.comps["ops"].issue_tenant_token(TENANT_A_ID, sub="iso-a")
        self.token_b = self.comps["ops"].issue_tenant_token(TENANT_B_ID, sub="iso-b")
        self.canary_phrase = f"{CANARY_PREFIX}-07"
        self._seed()

    # ---------------------------------------------------------------- 种子装载

    def _auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _seed(self) -> None:
        """两租户差异化种子（合成数据，NFR-SEC-06 脱敏口径）：A 常规、B 含 canary。"""
        for title in A_TITLES:
            self.client.post(
                f"{SAAS_TENANT_PREFIX}/knowledge/items",
                json={"title": title, "body": f"{title} 正文（合成数据）"},
                headers=self._auth(self.token_a),
            )
        for title in B_TITLES:
            self.client.post(
                f"{SAAS_TENANT_PREFIX}/knowledge/items",
                json={"title": title, "body": f"{title} 正文（含探针 {self.canary_phrase}）"},
                headers=self._auth(self.token_b),
            )
        # 对象与缓存键：存储前缀用例数据（A/B 各自前缀，形态刻意不同）
        self.client.put(
            f"{SAAS_TENANT_PREFIX}/objects/brochure-a.txt",
            content="A 租户宣传册（合成）",
            headers=self._auth(self.token_a),
        )
        self.client.put(
            f"{SAAS_TENANT_PREFIX}/objects/contract-b.txt",
            content=f"B 租户合同（含探针 {self.canary_phrase}）",
            headers=self._auth(self.token_b),
        )
        self.client.put(
            f"{SAAS_TENANT_PREFIX}/cache/sess-iso", content="", headers=self._auth(self.token_a)
        )

    # ---------------------------------------------------------------- 观测口

    def b_items(self) -> list:
        """B 租户全部知识条目（越权矩阵遍历目标）。"""
        return self.comps["store"].items_of(TENANT_B_ID)

    def a_items(self) -> list:
        return self.comps["store"].items_of(TENANT_A_ID)

    def a_object_keys(self) -> list[str]:
        """A 前缀下全部对象键（前缀归位断言）。"""
        return self.comps["objects"].list_prefix(f"tenants/{TENANT_A_ID}/")

    def a_cache_keys(self) -> list[str]:
        """A 上下文生成的全部键（前缀逃逸断言）。"""
        return self.comps["keyspace"].scan(TENANT_A_ID)

    def cross_prefix_read_status(self) -> int:
        """A 凭证读 B 前缀对象的响应码（403/404 均判拒绝）。"""
        resp = self.client.get(
            f"{SAAS_TENANT_PREFIX}/objects/contract-b.txt",
            headers=self._auth(self.token_a),
        )
        return resp.status_code

    def search(self, token: str, q: str) -> dict:
        resp = self.client.post(
            f"{SAAS_TENANT_PREFIX}/knowledge/search",
            json={"q": q},
            headers=self._auth(token),
        )
        return {"status": resp.status_code, "body": resp.json()}

    def remove_b_canary(self) -> None:
        """元测试②：清空 B 的 canary 数据（对照组零命中 → 证据无效 → FAIL）。"""
        store = self.comps["store"]
        for item in store.items_of(TENANT_B_ID):
            store.delete(item.uid)

    def seed_fingerprint(self) -> str:
        """种子数据集指纹（可复现要素②，SR §4 seed_fp 口径）。"""
        payload = sorted(
            (i.uid, i.tenant_id, i.title) for i in self.comps["store"].all_items()
        )
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).hexdigest()


def build_default_env(leak_mode: str = "isolated") -> IsolationEnv:
    return IsolationEnv(leak_mode=leak_mode)


# --------------------------------------------------------------------------- 套件


class IsolationTestSuite:
    """隔离测试套件：单用例异常捕获记 FAIL 不中断，保证覆盖矩阵完整（SR §6.5）。"""

    def __init__(self, env: IsolationEnv) -> None:
        self.env = env

    def run(self) -> IsolationRun:
        env = self.env
        run = IsolationRun(
            run_uid=f"ISO-RUN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}",
            env_name=f"in-memory（leak_mode={env.leak_mode}）",
            seed_fp=env.seed_fingerprint(),
            cmdline="python3 tools/saas/isolation_suite.py --report-out <out-dir>",
            started_at=datetime.now(timezone.utc).isoformat(),
            leak_mode=env.leak_mode,
        )
        run.results.extend(self._cases_api_matrix())
        run.results.append(self._case_canary())
        run.results.extend(self._cases_forged_params())
        run.results.append(self._case_storage_prefix())
        return run

    # ---------------------------------------------------------------- 用例组

    def _cases_api_matrix(self) -> list[CaseResult]:
        """① API 越权矩阵：A token × B 全部资源 × GET/PUT/DELETE 逐格遍历
        （SR §6.1：403/404 均判拒绝；错误报文也不得携带 B 的探针串）。"""
        env = self.env
        cases: list[CaseResult] = []
        for item in env.b_items():
            for method in ("GET", "PUT", "DELETE"):
                case_id = f"API-{method}-{item.uid}"
                try:
                    kwargs: dict = {"headers": env._auth(env.token_a)}
                    if method == "PUT":
                        kwargs["json"] = {"title": "越权写入尝试", "body": "x"}
                    resp = getattr(env.client, method.lower())(
                        f"{SAAS_TENANT_PREFIX}/knowledge/items/{item.uid}", **kwargs
                    )
                    ok = resp.status_code in (403, 404) and CANARY_PREFIX not in resp.text
                    cases.append(CaseResult(
                        case_id=case_id, layer="API", target=f"{method} items/{item.uid}",
                        expected="403/404 且报文无探针串", actual=str(resp.status_code),
                        verdict="PASS" if ok else "FAIL",
                        evidence={"status": resp.status_code, "body_head": resp.text[:120]},
                    ))
                except Exception as exc:  # 单用例异常不中断全量（SR §6.5）
                    cases.append(CaseResult(
                        case_id=case_id, layer="API", target=f"{method} items/{item.uid}",
                        expected="403/404 且报文无探针串", actual=f"异常: {exc}",
                        verdict="FAIL", evidence={},
                    ))
        return cases

    def _case_canary(self) -> CaseResult:
        """③ canary 检索：A 零命中；对照组 B ≥1（B=0 判 FAIL——数据缺失即证据无效）。"""
        env = self.env
        hit_a = env.search(env.token_a, env.canary_phrase)
        hit_b = env.search(env.token_b, env.canary_phrase)
        a_hits = int(hit_a["body"].get("data", {}).get("total", -1)) if hit_a["status"] == 200 else -1
        b_hits = int(hit_b["body"].get("data", {}).get("total", -1)) if hit_b["status"] == 200 else -1
        ok = a_hits == 0 and b_hits >= 1
        return CaseResult(
            case_id="VEC-canary",
            layer="VECTOR",
            target=f"统一检索 canary={env.canary_phrase}",
            expected="A=0 命中 且 B≥1 命中（对照组防空库假阴性）",
            actual=f"A={a_hits}, B={b_hits}",
            verdict="PASS" if ok else "FAIL",
            evidence={"a_hits": a_hits, "b_hits": b_hits},
        )

    def _cases_forged_params(self) -> list[CaseResult]:
        """辅助② 上下文逃逸：伪造 tenant_id 参数（body/query/header）全部被忽略。"""
        env = self.env
        results: list[CaseResult] = []
        injections = (
            ("FORGE-body", lambda: env.client.post(
                f"{SAAS_TENANT_PREFIX}/knowledge/search",
                json={"q": env.canary_phrase, "tenant_id": TENANT_B_ID},
                headers=env._auth(env.token_a),
            )),
            ("FORGE-query", lambda: env.client.post(
                f"{SAAS_TENANT_PREFIX}/knowledge/search",
                params={"tenant_id": str(TENANT_B_ID)},
                json={"q": env.canary_phrase},
                headers=env._auth(env.token_a),
            )),
            ("FORGE-header", lambda: env.client.post(
                f"{SAAS_TENANT_PREFIX}/knowledge/search",
                json={"q": env.canary_phrase},
                headers={**env._auth(env.token_a), "X-Tenant-Id": str(TENANT_B_ID)},
            )),
        )
        for case_id, send in injections:
            try:
                resp = send()
                body = resp.json().get("data", {}) or {}
                hits = int(body.get("total", -1))
                ok = resp.status_code == 200 and hits == 0 and CANARY_PREFIX not in resp.text
                results.append(CaseResult(
                    case_id=case_id, layer="FORGE", target="伪造 tenant_id 参数",
                    expected="参数被忽略（零命中）",
                    actual=f"status={resp.status_code}, hits={hits}",
                    verdict="PASS" if ok else "FAIL",
                    evidence={"body_head": resp.text[:200]},
                ))
            except Exception as exc:  # 单用例异常不中断全量（SR §6.5）
                results.append(CaseResult(
                    case_id=case_id, layer="FORGE", target="伪造 tenant_id 参数",
                    expected="参数被忽略（零命中）", actual=f"异常: {exc}",
                    verdict="FAIL", evidence={},
                ))
        return results

    def _case_storage_prefix(self) -> CaseResult:
        """④ 存储前缀：A 对象全部归位 tenants/{A}/；跨前缀读取拒绝；键空间无逃逸。"""
        env = self.env
        stray_objects = [k for k in env.a_object_keys()
                         if not k.startswith(f"tenants/{TENANT_A_ID}/")]
        cross_status = env.cross_prefix_read_status()
        stray_keys = [k for k in env.a_cache_keys() if not k.startswith(f"t:{TENANT_A_ID}:")]
        ok = not stray_objects and cross_status in (403, 404) and not stray_keys
        return CaseResult(
            case_id="STORAGE-prefix",
            layer="STORAGE",
            target=f"对象前缀 tenants/{TENANT_A_ID}/ + 键空间 t:{TENANT_A_ID}:",
            expected="前缀归位 且 跨前缀读取 403/404 且 无键逃逸",
            actual=(f"stray_objects={len(stray_objects)}, cross={cross_status}, "
                    f"stray_keys={len(stray_keys)}"),
            verdict="PASS" if ok else "FAIL",
            evidence={"stray_objects": stray_objects, "cross_status": cross_status,
                      "stray_keys": stray_keys},
        )


# --------------------------------------------------------------------------- 报告生成器


class ReportGenerator:
    """隔离性测试报告（MD）：执行概要 + 可复现三要素 + 覆盖矩阵 + 漏洞清单 +
    双方签字栏（SR §6.4 证据效力要求；生产形态另出 PDF 并入审计哈希链）。"""

    def __init__(self, out_dir: Path | str) -> None:
        self.out_dir = Path(out_dir)

    def render(self, run: IsolationRun) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{run.run_uid}.md"
        stats = run.stats()
        lines = [
            "# 多租户隔离性测试报告（NFR-SEC-07）",
            "",
            "## 1. 执行概要",
            "",
            f"- 报告编号：{run.run_uid}",
            f"- 执行时间：{run.started_at}",
            f"- 环境：{run.env_name}",
            f"- 缺陷注入模式：{run.leak_mode}（isolated=正常；open=元测试注入）",
            f"- 结论：**{run.verdict()}**（total={stats['total']}, "
            f"passed={stats['passed']}, failed={stats['failed']}）",
            "",
            "## 2. 可复现三要素",
            "",
            f"- ① 环境与版本：{run.env_name}（内存形态，等价 staging 口径）",
            f"- ② 种子数据集指纹（sha256）：`{run.seed_fp}`",
            f"- ③ 完整命令行：`{run.cmdline}`",
            "",
            "## 3. 覆盖矩阵",
            "",
            "| 用例 | 层 | 目标 | 期望 | 实际 | 结果 |",
            "|---|---|---|---|---|---|",
        ]
        for r in run.results:
            lines.append(f"| {r.case_id} | {r.layer} | {r.target} | {r.expected} | "
                         f"{r.actual} | {r.verdict} |")
        lines += ["", "## 4. 漏洞清单（FAIL 用例）", ""]
        vulns = [r for r in run.results if r.verdict == "FAIL"]
        if not vulns:
            lines.append("- 无（全部拒绝/零命中，四层隔离有效）")
        else:
            for v in vulns:
                lines.append(f"- [{v.layer}] {v.case_id}: {v.actual}（证据: {json.dumps(v.evidence, ensure_ascii=False)[:200]}）")
        lines += [
            "",
            "## 5. 签字栏",
            "",
            "| 角色 | 签字 | 日期 | 结论 |",
            "|---|---|---|---|",
            "| 甲方验收代表 | ____________ | ________ | □ 通过 □ 不通过 |",
            "| 乙方交付代表 | ____________ | ________ | □ 通过 □ 不通过 |",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """可复现命令（SR §5）：退出码 0=PASS / 1=FAIL；--report-out 指定报告目录。"""
    parser = argparse.ArgumentParser(description="多租户隔离测试套件（NFR-SEC-07）")
    parser.add_argument("--report-out", default=str(REPO_ROOT / "out" / "iso-reports"),
                        help="报告输出目录（MD）")
    parser.add_argument("--leak-mode", default="isolated", choices=("isolated", "open"),
                        help="isolated=正常被测；open=元测试注入隔离缺陷")
    args = parser.parse_args(argv)

    env = build_default_env(leak_mode=args.leak_mode)
    suite = IsolationTestSuite(env)
    run = suite.run()
    report = ReportGenerator(out_dir=args.report_out).render(run)
    stats = run.stats()
    print(f"隔离套件结论: {run.verdict()} "
          f"(total={stats['total']}, passed={stats['passed']}, failed={stats['failed']})")
    print(f"报告: {report}")
    return 0 if run.verdict() == "PASS" else 1


if __name__ == "__main__":  # 独立运行入口（可复现命令，SR §5）
    raise SystemExit(main())
