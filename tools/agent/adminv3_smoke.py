#!/usr/bin/env python3
# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 + 移动端端到端冒烟（FR-SC-005/006；P4 第二批交付门禁；demo 口径置顶）。

链路（SR-SC-005 §5/§6、SR-SC-006 §5/§6 的进程内端到端预演）：
  ① 28 账号（4 主 + 24 子）注册心跳 → 状态墙汇聚（健康三态 + WS 帧语义）；
  ② 批量重启 28 账号，其中 3 个账号注入容器超时失败 → 部分失败不回滚（partial）；
  ③ 失败清单出列 → 仅重试失败项 → 重试成功（故障恢复后 done）；
  ④ ROI 报表：询盘/回复率/45s 达标/介入率/成交额五口径预聚合对账；
  ⑤ 移动 BFF：双端免登换发 → 一屏聚合四卡片；
  ⑥ 红线演练：高危审批无令牌/伪造令牌确认一律拦截且下游零调用（SRS §7.4），
     校验码换令牌后确认放行（一次）。

输出：中文冒烟报告（Markdown，默认 out/agent/adminv3_smoke_report.md）+ 退出码
0（全链通过）/ 1（任一环节失败）。

重要口径：本冒烟为**内存链路 demo 口径**（容器操作/钉钉/微信推送为 Protocol+Fake、
校验码经捕获器不下发）；生产验收必须以真实编排（Docker Compose 28 容器）与真实
推送平台复测。用法（仓库根）：
  python3 tools/agent/adminv3_smoke.py [--report FILE]
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import sysconfig
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

# 仓库根自举：直接以文件路径执行时 sys.path[0] 为脚本目录，补齐仓库根才能导入 services 包
_REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPO_ROOT / "src"))


def _restore_stdlib_platform() -> None:
    """仓库根包名 platform 与标准库 platform 冲突的自愈（同 tools/precheck/phase0_kpi.py）。"""
    cached = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法定位标准库 platform 模块（运行环境损坏）")
    module = ModuleType("platform")
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(module, name, value)
    sys.modules["platform"] = module


_restore_stdlib_platform()

from fastapi.testclient import TestClient  # noqa: E402

from services.agent.api.adminv3_api import create_adminv3_app  # noqa: E402
from services.agent.core.adminv3.batch import BatchOps  # noqa: E402
from services.agent.core.adminv3.roi import InquiryFact, RoiStats  # noqa: E402
from services.agent.core.adminv3.wall import ClusterWall, Heartbeat, InstanceMeta  # noqa: E402
from services.agent.core.mobile.bff import (  # noqa: E402
    Binding,
    BindingStore,
    DingTalkWorkFake,
    FakeApprovalSink,
    FakeConversationSink,
    FakeHomeProvider,
    IdentityStub,
    MobileBff,
    MpTemplateFake,
    PushRouter,
    SmsFake,
    VerifyGate,
    WxSubscribeFake,
)

NOW = datetime(2026, 10, 11, 10, 0, 0)
DAY = date(2026, 10, 11)
FAIL_ACCOUNTS = (7, 19, 23)                        # 3 个故意失败账号（§9 冒烟剧本）
ROLES = {"supervisor:1": ("supervisor",), "sales:1": ("sales",)}


class Smoke:
    """端到端冒烟执行器：逐步记录（步骤/结论/证据），任一步失败整体不通过。"""

    def __init__(self) -> None:
        self.now = NOW
        self.steps: list[dict[str, Any]] = []
        self._container_faults: set[int] = set(FAIL_ACCOUNTS)   # 容器故障注入（可恢复）

        # ---- 装配：状态墙 + 批量（注入容器 Fake）+ ROI + V3 API ----
        self.wall = ClusterWall(hb_timeout_s=45.0, clock=lambda: self.now)
        self.calls: list[tuple[str, int]] = []

        def instance_ctl(action: str, account_id: int) -> None:
            """容器操作 Fake：故障集合内账号抛超时（重试时故障已恢复则放行）。"""
            self.calls.append((action, account_id))
            if account_id in self._container_faults:
                raise RuntimeError(f"容器操作超时 acct={account_id}")

        self.batch = BatchOps(instance_ctl=instance_ctl,
                              config_writer=lambda k, v, a: None, clock=lambda: self.now)
        self.roi = RoiStats()
        self.client = TestClient(create_adminv3_app(
            wall=self.wall, batch_ops=self.batch, roi=self.roi))

        # ---- 装配：移动 BFF（钉钉/微信推送 Fake + 校验码捕获）----
        self.codes: list[tuple[str, str]] = []
        self.bindings = BindingStore()
        self.bindings.add(Binding("supervisor:1", "dingtalk", "dd-001", "王主管"))
        self.push = PushRouter(DingTalkWorkFake(), WxSubscribeFake(), MpTemplateFake(),
                               SmsFake(), clock=lambda: self.now)
        self.verify = VerifyGate(
            code_sender=lambda uid, code: self.codes.append((uid, code)),
            clock=lambda: self.now)
        self.approvals = FakeApprovalSink()
        self.bff = MobileBff(
            bindings=self.bindings, identity=IdentityStub(dingtalk_users={"auth-ok": "dd-001"}),
            roles_of=ROLES.get, home=FakeHomeProvider(), approvals=self.approvals,
            conversations=FakeConversationSink(), push=self.push, verify=self.verify,
            clock=lambda: self.now)

    # ---------------------------------------------------------------- 步骤登记

    def record(self, name: str, ok: bool, evidence: str) -> None:
        self.steps.append({"name": name, "ok": ok, "evidence": evidence})

    # ---------------------------------------------------------------- 冒烟链路

    def run(self) -> bool:
        """执行六段链路；返回全链是否通过。"""
        return self._step_wall() and self._step_batch() and self._step_retry() \
            and self._step_roi() and self._step_mobile() and self._step_redline()

    def _register_28(self) -> None:
        for i in range(1, 29):
            self.wall.register(InstanceMeta(
                account_id=i, account_name=f"acct-{i:02d}",
                account_type="main" if i <= 4 else "sub",
                container_name=f"agent-{i:02d}", proxy_ip=f"<PROXY-IP-{i:02d}>"))

    def _step_wall(self) -> bool:
        """① 28 账号心跳 → 状态墙（三态 + full/delta 帧 + 失联告警）。"""
        try:
            self._register_28()
            for i in range(1, 28):
                self.wall.heartbeat(i, Heartbeat(ts=NOW - timedelta(seconds=10),
                                                 sessions_today=i * 3, ops_today=i))
            self.wall.heartbeat(28, Heartbeat(ts=NOW - timedelta(seconds=10)))
            first = self.wall.aggregate(now=NOW)
            full = self.wall.frame(None)
            self.wall.heartbeat(28, Heartbeat(ts=NOW - timedelta(seconds=1), sessions_today=999))
            second = self.wall.aggregate(now=NOW + timedelta(seconds=5))
            delta = self.wall.frame(first["ver"])
            ok = (
                len(self.wall.cards()) == 28
                and sum(1 for c in self.wall.cards() if c["account_type"] == "main") == 4
                and self.wall.health_of(1) == "ok"
                and full["type"] == "full" and len(full["snapshot"]) == 28
                and second["ver"] == first["ver"] + 1 and set(second["changes"]) == {28}
                and delta["type"] == "delta" and set(delta["changes"]) == {28}
            )
            self.record("① 28 账号心跳 → 状态墙", ok,
                        f"卡片 28（主 {4}/子 {24}）；全量帧 {len(full['snapshot'])} 卡；"
                        f"变化版本 {first['ver']}→{second['ver']}，delta 仅含账号 28")
            return ok
        except Exception as exc:  # noqa: BLE001——冒烟兜底：任何异常记失败不中断报告
            self.record("① 28 账号心跳 → 状态墙", False, f"异常：{exc}")
            return False

    def _step_batch(self) -> bool:
        """② 批量重启 28 账号（3 账号注入容器超时）→ partial 不回滚。"""
        try:
            resp = self.client.post(
                "/api/v1/agent/v3/batch",
                json={"action": "restart", "account_ids": list(range(1, 29))},
                headers={"X-Idempotency-Key": "smoke-batch-1"})
            task = resp.json()
            failed = task.get("failed_account_ids", [])
            ok = (resp.status_code == 200 and task["status"] == "partial"
                  and failed == list(FAIL_ACCOUNTS)
                  and task["total"] == 28
                  and all(item["state"] == "ok" for item in task["items"]
                          if item["account_id"] not in FAIL_ACCOUNTS)
                  and all(item["error_msg"] for item in task["items"] if item["state"] == "fail"))
            self._partial_uid = task["uid"]
            self.record("② 批量重启 28 账号（3 个故意失败）", ok,
                        f"任务 {task['uid']} 状态 partial；失败清单 {failed}；"
                        f"其余 {28 - len(failed)} 项保持 ok 不回滚")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.record("② 批量重启 28 账号（3 个故意失败）", False, f"异常：{exc}")
            return False

    def _step_retry(self) -> bool:
        """③ 故障恢复 → 仅重试失败项 → done；幂等键重复提交不重复执行。"""
        try:
            dup = self.client.post(
                "/api/v1/agent/v3/batch",
                json={"action": "restart", "account_ids": list(range(1, 29))},
                headers={"X-Idempotency-Key": "smoke-batch-1"})
            self._container_faults.clear()                 # 运维介入：容器故障恢复
            retry = self.batch.retry_fails(self._partial_uid, actor="admin:smoke")
            ok = (dup.json().get("uid") == self._partial_uid
                  and retry.account_ids == FAIL_ACCOUNTS
                  and retry.status == "done"
                  and self.batch.task(self._partial_uid).status == "partial")
            self.record("③ 失败清单重试（故障恢复后）", ok,
                        f"同幂等键重复提交返回原任务 {dup.json().get('uid')}；"
                        f"重试仅含 {list(retry.account_ids)} → done；原任务留痕不变")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.record("③ 失败清单重试（故障恢复后）", False, f"异常：{exc}")
            return False

    def _seed_roi(self) -> None:
        """灌入两日 ROI 账本（账号 1 十询盘 + 补录；账号 2 四询盘），预聚合。"""
        for i in range(10):
            self.roi.record(InquiryFact(
                account_id=1, conversation_id=f"c1-{i}", stat_date=DAY,
                replied=i < 8, reply_sec=30.0 if i < 7 else 60.0,
                human_touched=i < 2, gmv_confirmed=50.0 if i == 0 else 0.0))
        self.roi.record_gmv_manual(1, DAY, 450.0)
        for i in range(4):
            self.roi.record(InquiryFact(
                account_id=2, conversation_id=f"c2-{i}", stat_date=DAY,
                replied=i < 3, reply_sec=20.0, gmv_confirmed=100.0))
        self.roi.rollup_day(DAY)

    def _step_roi(self) -> bool:
        """④ ROI 报表（HTTP 只读视图）：口径对账 + 账号排名。"""
        try:
            self._seed_roi()
            overview = self.client.get(
                "/api/v1/agent/v3/roi",
                params={"date_from": DAY, "date_to": DAY}).json()
            account = self.client.get(
                "/api/v1/agent/v3/roi/1",
                params={"date_from": DAY, "date_to": DAY}).json()
            totals = overview["totals"]
            ok = (totals["inquiry_cnt"] == 14 and totals["gmv_total"] == 900.0
                  and account["reply_rate"] == 0.8 and account["reply_within45s"] == 7
                  and account["human_touch_rate"] == 0.2 and account["gmv_total"] == 500.0
                  and overview["ranking"][0]["account_id"] == 1)
            self.record("④ ROI 报表（预聚合对账）", ok,
                        f"整体：询盘 {totals['inquiry_cnt']}、成交 {totals['gmv_total']:.0f} 美元；"
                        f"账号 1：回复率 {account['reply_rate']}、45s 达标 {account['reply_within45s']}、"
                        f"介入率 {account['human_touch_rate']}、成交 {account['gmv_total']:.0f} 美元；"
                        f"排名首位账号 {overview['ranking'][0]['account_id']}")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.record("④ ROI 报表（预聚合对账）", False, f"异常：{exc}")
            return False

    def _step_mobile(self) -> bool:
        """⑤ 移动 BFF：钉钉免登换发 → 一屏聚合四卡片（下游 Fake 口径）。"""
        try:
            login = self.bff.login("dingtalk", "auth-ok")
            payload = self.bff.home_summary(str(login["token"]))
            ok = (login["user"]["roles"] == ("supervisor",)
                  and set(payload) == {"todo_alerts", "todo_approvals", "taking_over", "today"}
                  and payload["todo_alerts"] == 3 and payload["todo_approvals"] == 1
                  and payload["taking_over"] == 2 and payload["today"]["inquiries"] == 137)
            self.record("⑤ 移动 BFF 免登 + 一屏聚合", ok,
                        f"主管免登角色 {list(login['user']['roles'])}；四卡片 "
                        f"告警 {payload['todo_alerts']}/审批 {payload['todo_approvals']}"
                        f"/接管 {payload['taking_over']}/今日询盘 {payload['today']['inquiries']}")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.record("⑤ 移动 BFF 免登 + 一屏聚合", False, f"异常：{exc}")
            return False

    def _step_redline(self) -> bool:
        """⑥ 红线演练：无令牌/伪造令牌确认拦截（下游零调用）；正确校验码后放行一次。"""
        try:
            token = str(self.bff.login("dingtalk", "auth-ok")["token"])
            blocked_none = blocked_fake = False
            try:
                self.bff.confirm_approval(token, "sa-1", "", idem_key="smoke-k1")
            except Exception:  # noqa: BLE001——预期 40102 拦截
                blocked_none = True
            try:
                self.bff.confirm_approval(token, "sa-1", "faked-token", idem_key="smoke-k2")
            except Exception:  # noqa: BLE001
                blocked_fake = True
            zero_call = self.approvals.executed == 0
            blocked_calls = self.approvals.executed               # 拦截期实测下游调用数
            self.bff.request_challenge(token)
            code = self.codes[-1][1]
            vt = str(self.bff.issue_verify_token(token, code)["verify_token"])
            result = self.bff.confirm_approval(token, "sa-1", vt, note="冒烟确认",
                                               idem_key="smoke-k3")
            replay_blocked = False
            try:
                self.bff.confirm_approval(token, "sa-1", vt, idem_key="smoke-k4")
            except Exception:  # noqa: BLE001——令牌单次消费：重放拒绝
                replay_blocked = True
            ok = (blocked_none and blocked_fake and zero_call
                  and result.get("ok") is True and self.approvals.executed == 1
                  and replay_blocked)
            self.record("⑥ 高危审批二次校验红线", ok,
                        f"无令牌/伪造令牌确认拦截={blocked_none}/{blocked_fake}"
                        f"（拦截期下游调用 {blocked_calls} 次，零调用={zero_call}）；"
                        f"校验码换令牌后确认放行 1 次；令牌重放拒绝={replay_blocked}")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.record("⑥ 高危审批二次校验红线", False, f"异常：{exc}")
            return False

    # ---------------------------------------------------------------- 报告

    def render_report(self) -> str:
        """中文 Markdown 冒烟报告（demo 口径置顶，防口径粉饰）。"""
        passed = sum(1 for s in self.steps if s["ok"])
        total = len(self.steps)
        lines = [
            "# 管理后台 V3 + 移动端 端到端冒烟报告（adminv3_smoke）",
            "",
            f"- 生成时刻：{datetime.now().isoformat(timespec='seconds')}",
            f"- 冒烟结论：{'**通过**' if passed == total else '**不通过**'}"
            f"（{passed}/{total} 环节绿）",
            "- 口径声明：**内存链路 demo 口径**——容器操作/钉钉/微信推送为 Protocol+Fake，"
            "校验码经捕获器不下发；生产验收必须以真实编排（28 容器 Compose）与真实推送平台复测；",
            "- 追溯：FR-SC-005（管理后台 V3）/ FR-SC-006（移动端）→ SR-SC-005/SR-SC-006；"
            "红线依据 SRS §7.4（敏感操作 AI 只起草、人工二次确认）。",
            "",
            "## 冒烟链路",
            "",
            "| # | 环节 | 结论 | 证据 |",
            "|---|------|------|------|",
        ]
        for idx, step in enumerate(self.steps, 1):
            verdict = "通过" if step["ok"] else "**失败**"
            lines.append(f"| {idx} | {step['name']} | {verdict} | {step['evidence']} |")
        lines += [
            "",
            "## 关键断言摘要",
            "",
            "- 部分失败不回滚：28 项批量中 3 项失败，25 项保持 ok（SRS §16.5 批量正确性）；",
            "- 仅重试失败项：重试任务仅含失败账号，原任务留痕不变（审计链不可变）；",
            "- 幂等：同 X-Idempotency-Key 重复提交返回首次任务，不重复执行；",
            "- 红线：移动端 confirm 无令牌/伪造/重放一律 40102 且下游零调用——"
            "敏感操作必须人工二次校验（SRS §7.4）。",
            "",
        ]
        return "\n".join(lines)

    @property
    def all_green(self) -> bool:
        return all(step["ok"] for step in self.steps)


def main(argv: list[str] | None = None) -> int:
    """冒烟入口：执行链路 → 写报告 → 退出码 0/1。"""
    parser = argparse.ArgumentParser(description="管理后台 V3 + 移动端端到端冒烟（demo 口径）")
    parser.add_argument("--report", default="out/agent/adminv3_smoke_report.md",
                        help="报告输出路径（默认 out/agent/adminv3_smoke_report.md）")
    args = parser.parse_args(argv)

    smoke = Smoke()
    smoke.run()
    report = smoke.render_report()
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = _REPO_ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"报告已写入：{report_path}")
    return 0 if smoke.all_green else 1


if __name__ == "__main__":
    sys.exit(main())
