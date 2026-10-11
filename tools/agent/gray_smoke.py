# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""灰度上线端到端冒烟 CLI（FR-SC-001/002/004/007 串联预跑；demo 口径置顶）。

七步链路（SRS §16.5 Phase 4 验收项的内存链路演示口径）：
  ① 集群注册：28 实例绑定落注册表（account↔container↔cookie↔proxy_ip 一一对应），
     编排器批量供给全部 running（内存容器编排模拟，SRS §16.5 单点故障演练等价形态）；
  ② 错峰窗口：28 账号窗口起点黄金比例错开——两两互异（无两账号同窗口，SR §9）+
     槽位配额（同槽在线 ≤ 10）；
  ③ 内容防同质：同文案第二账号命中 → 强制重写（钩子协议注入确定性改写器）→
     ok:rewritten 过检（FR-SC-004-③ 链路自证）；
  ④ 灰度分组：前 10 账号 pilot 打标、余 18 对照（FR-SC-007 §6-1）；
  ⑤ 门禁判定：注入达标指标（封号 0 / 可用率 86% / 会话量 16.2 / 存活率 100%）
     → 三硬指标全过（SRS §16.3 口径）；
  ⑥ 放量：pilot → wave2（18 账号转入、分批错峰重启每批 ≤5）；
  ⑦ 决策记录落盘：全链决策台账（时间/动作/依据/审批人）写入中文 Markdown 报告。

**demo 口径声明**：本冒烟运行于内存合成数据（无真实账号/平台/容器/Milvus），指标为
注入达标值；生产验收必须按 SRS §16.3/§16.5 以 14 天真实观察窗运行数据复测，防口径
粉饰。退出码：0 = 七步全过；1 = 任一断言失败或运行异常。

用法（仓库根）：
  python3 tools/agent/gray_smoke.py [--report out/agent/gray_smoke_report.md]
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# 仓库根自举：直接以文件路径执行时补齐 tools 包与 src（services 包）导入路径
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.agent.core.cluster.callpool import ClusterRetrievalPool, PoolConfig  # noqa: E402
from services.agent.core.cluster.gray import (  # noqa: E402
    GateMetrics,
    GrayReleaseManager,
)
from services.agent.core.cluster.orchestrator import (  # noqa: E402
    ClusterConfig,
    ClusterOrchestrator,
    FakeContainerOps,
)
from services.agent.core.cluster.registry import InstanceRegistry  # noqa: E402
from services.agent.core.engine import InMemoryAlerts  # noqa: E402
from services.agent.core.riskv2.stagger import StaggerScheduler  # noqa: E402
from services.agent.core.riskv2.uniqueness import ContentUniqueness  # noqa: E402

TAG = "[灰度冒烟]"
DEFAULT_REPORT = Path("out") / "agent" / "gray_smoke_report.md"

NOW = datetime(2026, 10, 12, 9, 0, 0)
DAY = date(2026, 10, 13)
OBSERVE_DAYS = 14
PILOT_SIZE = 10
INSTANCE_COUNT = 28
CANARY_TEXT = "您好！感谢咨询，我们的 ST-3200 支架现货供应，价格优惠，欢迎下单。"
CANARY_VARIANT = "承蒙垂询！ST-3200 支架备货充足，本期报价实惠，期待合作下单。"


class DeterministicRewriter:
    """确定性改写钩子（demo）：首轮返回既定变体（生产 = LLM 改写器注入位）。"""

    def rewrite(self, text: str, account_id: int, attempt: int) -> str:
        return CANARY_VARIANT if attempt == 1 else text


def build_cluster() -> tuple[InstanceRegistry, ClusterOrchestrator, FakeContainerOps, dict]:
    """步骤①集群注册与批量供给：28 实例绑定 → 分轮供给全部 running。"""
    registry = InstanceRegistry()
    for seq in range(1, INSTANCE_COUNT + 1):
        registry.register(seq=seq, account_id=7000 + seq,
                          proxy_ip=f"10.8.{seq // 256}.{seq % 256}",
                          host_node=f"runner-{((seq - 1) // 3) + 1:02d}",
                          gray_batch=1)
    ops = FakeContainerOps()
    orch = ClusterOrchestrator(registry, ops, alerts=InMemoryAlerts(),
                               config=ClusterConfig(batch_max_per_round=5,
                                                    rollout_gap_s=0.0))
    report = orch.provision(gray_batch=1)
    assert report["created"] == INSTANCE_COUNT, "批量供给未全量创建"
    assert registry.by_status() == {"running": INSTANCE_COUNT}, "实例未全部 running"
    return registry, orch, ops, report


def check_stagger(accounts: list[int]) -> dict:
    """步骤②错峰窗口：起点两两互异（无重叠）+ 槽位配额不超。"""
    scheduler = StaggerScheduler(accounts)
    starts = [scheduler.window_for(a, day=DAY)[0] for a in accounts]
    assert len(set(starts)) == len(accounts), "存在两账号同窗口起点（错峰失效）"
    allocation = scheduler.allocate_slots(day=DAY)
    load: dict[int, int] = {}
    for slots in allocation.values():
        for s in slots:
            load[s] = load.get(s, 0) + 1
    assert load and max(load.values()) <= 10, "同槽在线超配额"
    assert all(len(v) >= 1 for v in allocation.values()), "存在账号被饿死（0 槽）"
    return {"distinct_starts": len(set(starts)), "max_slot_load": max(load.values()),
            "slots": len(load)}


def check_uniqueness() -> dict:
    """步骤③内容防同质：首发过检 → 第二账号同文案强制重写过检（同质内容不上线）。"""
    guard = ContentUniqueness(rewriter=DeterministicRewriter(),
                              clock=lambda: NOW, rewrite_max_attempts=2)
    first = guard.guard(7001, "smoke-m-0001", CANARY_TEXT)
    assert first.allow and first.reason == "ok", "首发未过检"
    second = guard.guard(7002, "smoke-m-0002", CANARY_TEXT)
    assert second.allow and second.reason == "ok:rewritten", "第二账号同文案未被强制重写"
    assert second.text_out == CANARY_VARIANT and second.rewrites == 1, "重写产物/次数不符"
    assert guard.fingerprint_count() == 2, "指纹登记数不符"
    return {"first_reason": first.reason, "second_reason": second.reason,
            "rewrites": second.rewrites, "fingerprints": guard.fingerprint_count()}


def check_retrieval_pool() -> dict:
    """知识库调用池自证（FR-SC-002）：50 线程同指纹合并单次回源（并发削峰）。"""
    class _Backend:
        calls = 0

        def search(self, req: dict) -> dict:
            with threading.Lock():
                _Backend.calls += 1
            time.sleep(0.01)  # 回源时延：制造真实并发窗口（合并可见）
            return {"hits": [{"chunk_id": "c1", "score": 0.92, "text": "ST-3200 报价"}],
                    "latency_ms": 10.0}

    backend = _Backend()
    pool = ClusterRetrievalPool(backend, config=PoolConfig())
    req = {"query": "ST-3200 支架 价格", "kb_types": ["product"], "top_k": 5}
    results: list[dict] = []
    barrier = threading.Barrier(50)

    def worker(i: int) -> None:
        barrier.wait()
        results.append(pool.search(req, requester=f"runner-{i:02d}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert backend.calls == 1, "同指纹未合并为单次回源"
    assert all(r["hits"] == results[0]["hits"] for r in results), "并发结果竞态"
    return {"backend_calls": backend.calls, "merged": pool.stats["merges"]}


def run_gray_release(registry: InstanceRegistry) -> tuple[GrayReleaseManager, object, dict]:
    """步骤④⑤⑥⑦：pilot 分组 → 门禁（注入达标指标）→ 放量 → 决策台账。"""
    alerts = InMemoryAlerts()
    batches: list[list[int]] = []
    metrics = GateMetrics(ban_or_restricted=0, reply_usable_rate=0.86,
                          sessions_per_account_day=16.2, instance_alive_rate=1.0)
    manager = GrayReleaseManager(
        registry=registry,
        metrics=lambda plan: metrics,
        alerts=alerts,
        restart_hook=lambda ids: batches.append(list(ids)),
    )
    pilot = [7000 + s for s in range(1, PILOT_SIZE + 1)]
    plan = manager.create_plan(
        plan_no="GRAY-SMOKE-2026-001", target_version="strategy:v2",
        baseline_version="strategy:v1", pilot_account_ids=pilot,
        observe_window_days=OBSERVE_DAYS,
        target_config={"strategy.version": "v2"}, now=NOW)
    assert registry.group_counts() == {"pilot": PILOT_SIZE,
                                       "control": INSTANCE_COUNT - PILOT_SIZE}, "分组打标不符"
    assert manager.resolve_config("strategy.version", "pilot") == "v2", "试点组未命中覆盖键"
    manager.set_global_config("strategy.version", "v1")
    assert manager.resolve_config("strategy.version", "control") == "v1", "对照组未回落全局键"

    manager.start_observing(plan.plan_no, now=NOW)
    after_window = NOW + timedelta(days=OBSERVE_DAYS + 1)
    gate = manager.evaluate_gate(plan.plan_no, now=after_window)
    assert gate["passed"] is True, "门禁未通过（注入达标指标下应全绿）"
    assert plan.state == "gate_passed", "门禁通过后状态不符"

    rollout = manager.rollout(plan.plan_no, approver="王审核（demo）", now=after_window)
    assert rollout["to_group"] == "wave2" and rollout["accounts"] == 18, "放量结果不符"
    assert registry.group_counts()["wave2"] == 18, "wave2 分组计数不符"
    assert all(len(b) <= 5 for b in batches), "放量分批超出批次上限"
    return manager, plan, {"gate_checks": gate["checks"], "rollout": rollout,
                           "decision_count": len(manager.decisions)}


def write_report(path: Path, sections: list[tuple[str, str]], decisions) -> None:
    """中文 Markdown 报告落盘（决策台账表 + demo 口径声明置顶）。"""
    lines = [
        "# 灰度上线端到端冒烟报告（gray_smoke）",
        "",
        f"- 生成时刻：{datetime.now().isoformat(timespec='seconds')}",
        "- 演示计划：GRAY-SMOKE-2026-001（28 实例 / pilot 10 / 观察窗 14 天）",
        "",
        "> **demo 口径声明**：本报告为内存合成数据 + 注入达标指标的工程自证证据"
        "（无真实账号/平台/容器/Milvus）；生产验收必须按 SRS §16.3/§16.5 以真实运行"
        " 14 天观察窗数据复测。",
        "",
    ]
    for title, body in sections:
        lines += [f"## {title}", "", body, ""]
    lines += ["## 决策记录台账（§15.3 付款节点证据链）", "",
              "| 决策时间 | 动作 | 审批人 | 依据摘要 |", "|---|---|---|---|"]
    for d in decisions:
        summary = "; ".join(f"{k}={v}" for k, v in list(d.evidence.items())[:3])
        lines.append(f"| {d.decided_at.isoformat(timespec='seconds')} | {d.action} "
                     f"| {d.approver} | {summary} |")
    lines += ["", "---", "",
              "知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """七步冒烟主流程：任一断言失败即退出码 1（报告仍落盘以留存失败现场）。"""
    parser = argparse.ArgumentParser(description="灰度上线端到端冒烟（demo 口径）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告输出路径")
    args = parser.parse_args(argv)
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = _REPO_ROOT / report_path
    sections: list[tuple[str, str]] = []
    try:
        registry, _orch, ops, provision = build_cluster()
        sections.append((
            "① 集群注册与批量供给（FR-SC-001）",
            f"- 绑定清单：{registry.count()} 实例（account↔container↔cookie↔proxy_ip 一一对应）\n"
            f"- 供给结果：created={provision['created']}，batches={provision['batches']}，"
            f"by_status={registry.by_status()}\n"
            f"- 容器操作留痕：start × {len(ops.calls_of('start'))}（内存编排模拟）"))

        accounts = [7000 + s for s in range(1, INSTANCE_COUNT + 1)]
        stagger = check_stagger(accounts)
        sections.append((
            "② 操作时段错峰（FR-SC-004-②）",
            f"- 28 账号窗口起点两两互异：{stagger['distinct_starts']}/{INSTANCE_COUNT}"
            "（黄金比例低差异序列，无同窗口）\n"
            f"- 槽位配额：{stagger['slots']} 槽，最大同槽在线 {stagger['max_slot_load']}/10"))

        uniqueness = check_uniqueness()
        sections.append((
            "③ 内容防同质（FR-SC-004-③）",
            f"- 首发：{uniqueness['first_reason']}；第二账号同文案：{uniqueness['second_reason']}"
            f"（强制重写 {uniqueness['rewrites']} 次后过检，同质内容不上线）\n"
            f"- 指纹登记：{uniqueness['fingerprints']} 条（24h 滚动窗口口径）"))

        pool = check_retrieval_pool()
        sections.append((
            "④ 知识库集群调用池（FR-SC-002）",
            f"- 50 线程同指纹并发：回源 {pool['backend_calls']} 次、合并 {pool['merged']} 次"
            "（singleflight 削峰，结果零竞态）"))

        manager, plan, gray = run_gray_release(registry)
        checks_md = "\n".join(
            f"- {c['metric']}：value={c['value']} threshold={c['threshold']} "
            f"→ {'PASS' if c['passed'] else 'FAIL'}" for c in gray["gate_checks"])
        sections.append((
            "⑤ 灰度分组（FR-SC-007 §6-1）",
            "- 分组计数：pilot=10 / control=18（组级配置隔离：pilot→v2，control→v1）"))
        sections.append((
            "⑥ 放量门禁（FR-SC-007 §6-2/6-3）",
            f"{checks_md}\n- 判定：passed（观察窗恰满 {OBSERVE_DAYS}+1 天后判定，注入达标指标）\n"
            f"- 放量：pilot→{gray['rollout']['to_group']}（{gray['rollout']['accounts']} 账号，"
            f"{gray['rollout']['batches']} 批错峰重启，每批 ≤5）"))
        sections.append((
            "⑦ 决策记录（§15.3 付款节点证据链）",
            f"- 台账 {gray['decision_count']} 条（时间/动作/依据/审批人，只追加时序单调），"
            "明细见下表"))

        write_report(report_path, sections, manager.decisions)
        print(f"{TAG} 冒烟通过：七步全绿，报告已落盘 {report_path}")
        print(f"{TAG} 决策台账 {len(manager.decisions)} 条、末笔动作 "
              f"{manager.decisions[-1].action}（计划 {plan.plan_no} 状态 {plan.state}）")
        return 0
    except AssertionError as exc:
        write_report(report_path, sections + [("异常", f"- 冒烟断言失败：{exc}")], [])
        print(f"{TAG} 冒烟失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 —— demo 收口：异常也要落盘留痕
        write_report(report_path, sections + [("异常", f"- 运行异常：{exc!r}")], [])
        print(f"{TAG} 运行异常：{exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
