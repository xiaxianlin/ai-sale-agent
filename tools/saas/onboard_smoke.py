#!/usr/bin/env python3
# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""新租户纯配置接入端到端冒烟（FR-SAAS-002/006；P5 交付门禁；demo 口径置顶）。

链路（SR-SAAS-002 §6.5 七步接入清单 + SR-SAAS-006 白标链路的进程内端到端预演）：
  ① 创建向导 → ② 四步配置全 API（企业信息/素材/账号/策略，差异项显式覆盖）；
  ③ 激活幂等（同 Idempotency-Key 重放同结果）；
  ④ 知识初始化长任务：1 文件瞬时抖动重试后成功、1 文件确定性失败 → 向导 failed
     → retry 仅补跑失败文件 → 重跑全绿 → active；
  ⑤ 白标：PUT 品牌 → ETag/304 协商缓存（免发版生效）；
  ⑥ 自定义域名：注册 → TXT/CNAME 验证 → 证书签发 → active → Host 路由解析；
  ⑦ 零代码证据：drill-report 七步全 ok + 素材全在本租户 OSS 前缀（隔离冒烟）。

输出：中文冒烟报告（Markdown，默认 out/saas/onboard_smoke_report.md）+ 退出码
0（全链通过）/ 1（任一环节失败）。

重要口径：本冒烟为**进程内 demo 口径**（P0 流水线/DNS/证书为 Protocol+Fake、
OSS 直传为登记口径）；生产验收必须以真实部署包（FR-SAAS-004）与真实解析流水线
复测。用法（仓库根）：
  python3 tools/saas/onboard_smoke.py [--report out/saas/onboard_smoke_report.md]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

# 仓库根自举：直接以文件路径执行时补齐 src 导入路径
_REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from services.saas.api.branding_api import create_branding_app  # noqa: E402
from services.saas.api.onboarding_api import create_onboarding_app  # noqa: E402
from services.saas.core.branding.service import BrandingService, brand_etag  # noqa: E402
from services.saas.core.onboarding.kb_init import DeterministicError, TransientError  # noqa: E402
from services.saas.core.onboarding.wizard import OnboardingWizard  # noqa: E402

STRATEGY = {"strategy.grade.thresholds": {"A": 80, "B": 60},
            "risk.max_ops_per_day": 200, "strategy.followup.cadence.A": "1h"}


class SmokeDns:
    """假 DNS（demo）：TXT/CNAME 恒通过（生产为递归解析实查）。"""

    def txt_matches(self, name: str, token: str) -> bool:
        return True

    def cname_points_to(self, domain: str, target: str) -> bool:
        return True


class SmokeCert:
    """假证书签发器（demo）：恒成功（生产 cert-manager / acme.sh）。"""

    def issue(self, domain: str) -> bool:
        return True


class SmokeRunner:
    """零代码接入冒烟执行器：逐步跑链路并记录（步骤/断言/明细）。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []
        self.wizard = OnboardingWizard()
        self.branding = BrandingService(dns=SmokeDns(), cert=SmokeCert())
        self.client = TestClient(create_onboarding_app(self.wizard))
        self.brand_client = TestClient(create_branding_app(self.branding))
        self.uid = ""
        self.tenant_id = 0
        self.task_uid = ""
        self.ok_count = 0

    def step(self, name: str, ok: bool, detail: str) -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail})
        self.ok_count += 1 if ok else 0

    # -- 链路步骤 -----------------------------------------------------------

    def run(self) -> bool:
        return all([self._create_and_configure(), self._activate_idempotent(),
                    self._kb_init_with_retry(), self._whitelabel_etag(),
                    self._custom_domain(), self._zero_code_evidence()])

    def _create_and_configure(self) -> bool:
        """①② 创建向导 + 四步配置全 API（零代码：仅数据写入）。"""
        resp = self.client.post("/api/v1/saas/onboarding", json={"plan": "standard"})
        data = resp.json()["data"]
        self.uid, self.tenant_id = data["uid"], data["tenant_id"]
        ok_create = resp.status_code == 200 and data["status"] == "configuring"
        ok_company = self.client.put(
            f"/api/v1/saas/onboarding/{self.uid}/company-info", json={
                "name": "冒烟演练租户", "brand": {
                    "logo": f"https://oss.example.com/tenants/{self.tenant_id}/brand/l.png",
                    "primary": "#1E63E9"},
                "timezone": "Asia/Shanghai", "language": "zh-CN"}).status_code == 200
        materials = [{"oss_key": f"t:{self.tenant_id}/raw/spec-{i}.pdf", "size": 4096 * (i + 1),
                      "format": "pdf"} for i in range(3)]
        mats_resp = self.client.post(
            f"/api/v1/saas/onboarding/{self.uid}/kb/materials", json={"materials": materials})
        est = mats_resp.json()["data"]["estimate"]
        ok_materials = mats_resp.status_code == 200 and est["chunks"] > 0
        acc_resp = self.client.put(f"/api/v1/saas/onboarding/{self.uid}/accounts", json={
            "accounts": [{"platform": "alibaba", "username": "smoke-account",
                          "credential": "cred-smoke-2026"}]})
        ok_accounts = (acc_resp.status_code == 200
                       and "cred-smoke-2026" not in acc_resp.text  # 凭证无明文回显
                       and acc_resp.json()["data"]["accounts"][0]["credential"].startswith("<enc:"))
        ok_strategy = self.client.put(
            f"/api/v1/saas/onboarding/{self.uid}/strategy", json=STRATEGY).status_code == 200
        ok = ok_create and ok_company and ok_materials and ok_accounts and ok_strategy
        self.step("四步配置（全 API）", ok,
                  f"uid={self.uid} tenant={self.tenant_id} 预估 chunks={est['chunks']}，"
                  f"凭证掩码回显={ok_accounts}")
        return ok

    def _activate_idempotent(self) -> bool:
        """③ 提交开通：幂等重放同结果。"""
        first = self.client.post(f"/api/v1/saas/onboarding/{self.uid}/activate",
                                 headers={"Idempotency-Key": "smoke-act-001"})
        replay = self.client.post(f"/api/v1/saas/onboarding/{self.uid}/activate",
                                  headers={"Idempotency-Key": "smoke-act-001"})
        self.task_uid = first.json()["data"]["kb_task_uid"]
        ok = (first.status_code == 200 and self.task_uid
              == replay.json()["data"]["kb_task_uid"])
        self.step("激活幂等", ok, f"kb_task={self.task_uid}（重放同键同结果）")
        return ok

    def _kb_init_with_retry(self) -> bool:
        """④ 初始化长任务：瞬时抖动重试 + 确定性失败 → failed → 仅补跑失败 → active。"""
        task = self.wizard.kb_init.task_of(self.uid)
        keys = [f.oss_key for f in task.files]
        flips = {keys[0]: 0}
        repaired = {"v": False}  # 模拟人工修复：替换素材后重试不再失败

        def pipeline(oss_key: str, fmt: str) -> None:
            if oss_key == keys[0]:  # 瞬时抖动 1 次（Embedding 抖动口径）
                flips[oss_key] += 1
                if flips[oss_key] == 1:
                    raise TransientError("embedding 抖动")
            if oss_key == keys[2] and not repaired["v"]:
                raise DeterministicError("格式不支持（演示确定性失败）")

        self.wizard.kb_init.run_task(self.task_uid, pipeline)
        partial_failed = self.wizard.store.get(self.uid).status == "failed"
        reset = self.wizard.retry_kb(self.uid)  # 仅补跑 failed 文件
        retried = reset == 1
        repaired["v"] = True  # 修复动作=替换 OSS 素材（仍是纯配置操作）
        final = self.wizard.kb_init.run_task(self.task_uid, pipeline)
        prog = self.wizard.kb_init.progress(self.task_uid)
        active = (self.wizard.store.get(self.uid).status == "active"
                  and prog["percent"] == 100.0 and final == "done")
        ok = partial_failed and retried and active and task.files[0].attempts == 2
        self.step("知识初始化（重试闭环）", ok,
                  f"瞬时抖动重试 attempts={task.files[0].attempts}，确定性失败→failed→"
                  f"补跑 {reset} 文件→active（percent={prog['percent']}）")
        return ok

    def _whitelabel_etag(self) -> bool:
        """⑤ 白标：PUT 品牌 → 公开读取 ETag → If-None-Match 304（免发版生效）。"""
        put = self.brand_client.put("/api/v1/saas/tenants/7/brand-config", json={
            "product_name": "冒烟白标品牌", "font_family": "system_cn",
            "colors": {"primary": "#1A73E8", "secondary": "#00A884", "accent": "#FF7043",
                       "bg_page": "#F7F8FA", "text_main": "#1F2329"}})
        version = put.json()["data"]["version"]
        etag = brand_etag(7, version)
        first = self.brand_client.get("/api/v1/public/brand-config", params={"tenant_id": 7})
        cached = self.brand_client.get("/api/v1/public/brand-config",
                                       params={"tenant_id": 7},
                                       headers={"If-None-Match": etag})
        ok = (put.status_code == 200 and first.headers.get("etag") == etag
              and cached.status_code == 304 and cached.content == b"")
        self.step("白标生效（ETag/304）", ok,
                  f"version={version} etag={etag} 304 零载荷={cached.status_code == 304}")
        return ok

    def _custom_domain(self) -> bool:
        """⑥ 自定义域名：注册 → 验证 → 证书签发 → active → 路由解析。"""
        reg = self.brand_client.post("/api/v1/saas/domains",
                                     json={"tenant_id": 7, "domain": "smoke.acme-example.com"})
        domain_id = reg.json()["data"]["id"]
        verify = self.brand_client.post(f"/api/v1/saas/domains/{domain_id}/verify")
        data = verify.json()["data"]
        ok = (data["state"] == "active" and data["cert_state"] == "issued"
              and self.branding.resolve_host("smoke.acme-example.com") == 7)
        self.step("自定义域名（注册-验证-证书-路由）", ok,
                  f"state={data['state']} cert={data['cert_state']} "
                  f"路由 Host→7={self.branding.resolve_host('smoke.acme-example.com')}")
        return ok

    def _zero_code_evidence(self) -> bool:
        """⑦ 零代码证据：drill-report 七步全 ok + 素材全在本租户前缀（隔离冒烟）。"""
        report = self.client.get(
            f"/api/v1/saas/onboarding/{self.uid}/drill-report").json()["data"]["steps"]
        all_ok = all(s["ok"] for s in report)
        task = self.wizard.kb_init.task_of(self.uid)
        prefix_ok = all(f.oss_key.startswith(f"t:{self.tenant_id}/raw/")
                        for f in task.files)
        ok = len(report) == 7 and all_ok and prefix_ok
        self.step("零代码证据（七步清单/隔离）", ok,
                  f"drill 七步={all_ok}，素材前缀 t:{self.tenant_id}/raw/ 全命中={prefix_ok}")
        return ok

    # -- 报告 ----------------------------------------------------------------

    def render_report(self) -> str:
        """渲染中文冒烟报告（Markdown）。"""
        passed = len(self.steps) == self.ok_count
        conclusion = "PASS（全链通过）" if passed else "FAIL（存在失败环节）"
        lines = [
            "# 新租户纯配置接入冒烟报告（onboard_smoke）",
            "",
            "| 属性 | 内容 |",
            "|---|---|",
            f"| 执行时间 | {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} |",
            f"| 结论 | **{conclusion}**（{self.ok_count}/{len(self.steps)} 环节通过） |",
            "| 口径 | 进程内 demo（P0 流水线/DNS/证书为 Fake）；生产以部署包复测 |",
            "| 依据 | SR-SAAS-002 §6.5 七步接入清单 + SR-SAAS-006 白标链路 |",
            "",
            "| # | 环节 | 结果 | 明细 |",
            "|---|---|---|---|",
        ]
        for i, s in enumerate(self.steps, start=1):
            mark = "PASS" if s["ok"] else "FAIL"
            lines.append(f"| {i} | {s['name']} | {mark} | {s['detail']} |")
        lines += ["", f"退出码：{'0' if passed else '1'}", ""]
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="新租户纯配置接入端到端冒烟（demo 口径）")
    parser.add_argument("--report", default=str(_REPO_ROOT / "out/saas/onboard_smoke_report.md"),
                        help="冒烟报告输出路径")
    args = parser.parse_args(argv)
    runner = SmokeRunner()
    runner.run()
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(runner.render_report(), encoding="utf-8")
    passed = len(runner.steps) == runner.ok_count
    print(f"[onboard_smoke] {'PASS' if passed else 'FAIL'}："
          f"{runner.ok_count}/{len(runner.steps)} 环节通过，报告 {report}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
