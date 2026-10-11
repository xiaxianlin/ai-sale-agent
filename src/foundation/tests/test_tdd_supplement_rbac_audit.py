"""知识产权归甲方所有（COM-IP-01）

TDD 补充测试（平台域）：SEC-02 RBAC + SEC-03 审计哈希链——五类场景缺口补齐。

TDD 新规口径：等价类 / 边界 / 异常 / 并发 / 降级 五类场景逐列审计后，
本文件只覆盖既有 test_rbac.py / test_audit.py 未覆盖的缺口：
- SEC-02：空权限声明 fail-closed、max_scope 未知角色、自继承成环、行副本防篡改、
  审计钩子故障降级（拒绝结论不变）、匿名越权留痕、ContextVar 线程隔离（并发）；
- SEC-03：单条链边界、首行篡改/删除边界、bool 字符串化、ip 参与哈希、
  并发同 event_id 幂等（竞态）、追加期间对账一致性（并发）。

KPI 映射：SEC-02 → NFR-SEC-02（40301 越权拒绝率 100% / 越权必留痕）；
SEC-03 → NFR-SEC-03（篡改告警 100% 检出 / 审计记录完整率）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

import pytest

from foundation.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditEventType,
    AuditWriter,
    InMemoryAuditStorage,
    canonical_payload,
)
from foundation.security import (
    DataScope,
    PermissionDeniedError,
    Principal,
    RoleCycleError,
    RoleDef,
    UnknownRoleError,
    clear_current_principal,
    get_current_principal,
    max_scope,
    principal_context,
    require_role,
    resolve_perms,
    scope_filter,
    set_current_principal,
    set_deny_auditor,
)


def make_principal(role: str, uid: str = "1001") -> Principal:
    """按矩阵构造单角色主体（与 test_rbac.make_principal 同构的本地桩）。"""
    return Principal(
        uid=uid,
        roles=(role,),
        perms=frozenset(resolve_perms(role)),
        scope=max_scope((role,)),
        tenant_id=1,
        privileged=role in {"kb_admin", "sys_admin", "tenant_admin", "platform_ops"},
    )


@pytest.fixture(autouse=True)
def _reset_security_state() -> Any:
    """用例后清理全局主体与越权审计钩子，避免用例间串扰。"""
    yield
    set_deny_auditor(None)
    clear_current_principal()


# ---------------------------------------------------------------- SEC-02 RBAC


class TestRbacSupplement:
    def test_require_role_empty_perms_fails_closed(self) -> None:
        """【异常】require_role() 空声明：任何角色（含 sys_admin）一律 40301 拒绝。

        KPI 映射：NFR-SEC-02 越权拒绝率 100%（声明缺失端点必须默认拒绝，fail-closed）。
        """
        @require_role()
        def handler() -> str:
            return "ok"

        with principal_context(make_principal("sys_admin")):
            with pytest.raises(PermissionDeniedError) as excinfo:
                handler()
        assert excinfo.value.code == 40301
        assert excinfo.value.required_perms == ()  # 声明为空集：无任何动作可放行

    def test_max_scope_unknown_role_raises(self) -> None:
        """【异常】max_scope 传入未声明角色 → UnknownRoleError（fail-closed，防配置漂移）。

        KPI 映射：NFR-SEC-02 角色矩阵一致性（未声明角色零放行）。
        """
        with pytest.raises(UnknownRoleError):
            max_scope(("sales", "ghost"))

    def test_role_self_cycle_detected(self) -> None:
        """【边界】自继承（a extends a）成环：resolve_perms 抛 RoleCycleError。

        KPI 映射：NFR-SEC-02 矩阵导入期自检（最短环也必须在启动期暴露）。
        """
        self_loop: dict[str, RoleDef] = {
            "a": RoleDef(name="A", perms=frozenset({"a:do"}), scope=DataScope.SELF, extends=("a",)),
        }
        with pytest.raises(RoleCycleError):
            resolve_perms("a", matrix=self_loop)

    def test_scope_filter_returns_row_copies(self) -> None:
        """【降级】过滤器返回行副本：业务侧改写返回行不污染原始数据（防共享可变态）。

        KPI 映射：NFR-SEC-02 数据范围隔离（过滤输出不可反向写穿）。
        """
        rows: list[dict[str, object]] = [
            {"tenant_id": 1, "owner_user_id": "1001", "v": "origin"},
        ]
        principal = make_principal("trainee", uid="1001")
        filtered = scope_filter(rows, principal)
        assert filtered and filtered[0] is not rows[0]
        filtered[0]["v"] = "tampered"
        assert rows[0]["v"] == "origin"  # 原始行未被改写

    def test_deny_auditor_failure_still_denies(self) -> None:
        """【降级】越权审计钩子自身抛异常：拒绝结论不变（40301 照常抛出），
        钩子异常不向外传播——留痕降级为日志告警，权限判定不受审计通道故障拖累。

        KPI 映射：NFR-SEC-02 越权必拒（审计链故障不产生越权放行窗口）。
        """

        def broken_hook(record: Mapping[str, object]) -> None:
            raise RuntimeError("审计链写入通道故障")

        set_deny_auditor(broken_hook)

        @require_role("user:manage")
        def handler() -> str:
            return "ok"

        with principal_context(make_principal("trainee")):
            with pytest.raises(PermissionDeniedError) as excinfo:
                handler()
        assert excinfo.value.code == 40301  # 拒绝优先，未被钩子异常吞掉

    def test_anonymous_deny_writes_audit_with_placeholder_actor(self) -> None:
        """【异常】匿名（无主体）越权：审计留痕 actor='anonymous'（暴力探测线索）。

        KPI 映射：NFR-SEC-03 越权尝试留痕完整率（匿名侧不缺行）。
        """
        seen: list[Mapping[str, object]] = []
        set_deny_auditor(seen.append)

        @require_role("user:manage")
        def handler() -> str:
            return "ok"

        clear_current_principal()
        with pytest.raises(PermissionDeniedError):
            handler()
        assert len(seen) == 1
        record = seen[0]
        assert record["actor"] == "anonymous"
        assert record["action_type"] == "perm_change" and record["result"] == "denied"
        detail = dict(record["detail"])  # type: ignore[arg-type]
        assert detail["kind"] == "rbac_deny" and "user:manage" in detail["required_perms"]

    def test_principal_context_thread_isolation(self) -> None:
        """【并发】ContextVar 线程隔离：主线程注入的主体在工作线程不可见；
        工作线程注入也不回流主线程——并发请求主体不串号（SR §6.2 并发安全口径）。

        KPI 映射：NFR-SEC-02 并发请求主体串号数 = 0。
        """
        main_principal = make_principal("sys_admin", uid="admin")
        set_current_principal(main_principal)
        seen: dict[str, Principal | None] = {}

        def worker() -> None:
            seen["inherited"] = get_current_principal()  # 新线程：默认 None（不继承主线程上下文）
            worker_principal = make_principal("trainee", uid="worker")
            set_current_principal(worker_principal)
            seen["worker_self"] = get_current_principal()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen["inherited"] is None          # 主线程主体不泄漏进工作线程
        assert seen["worker_self"] is not None    # 工作线程内注入生效
        assert seen["worker_self"].uid == "worker"
        assert get_current_principal() is main_principal  # 工作线程注入不回流主线程


# ---------------------------------------------------------------- SEC-03 审计哈希链


def _canonical_row(**overrides: Any) -> dict[str, Any]:
    """构造参与哈希计算的九字段行（与 test_audit._row 同构的本地桩）。"""
    row: dict[str, Any] = {
        "tenant_id": 1,
        "actor": "u1",
        "action_type": "login",
        "target": "auth",
        "result": "success",
        "detail": {},
        "trace_id": "",
        "ip": None,
        "created_at": "2026-10-11T02:00:00.000000+00:00",
    }
    row.update(overrides)
    return row


class TestAuditSupplement:
    def test_single_row_chain_verify(self) -> None:
        """【边界】单条链：首条 prev_hash=GENESIS，全链校验通过且锚点=head 哈希。

        KPI 映射：NFR-SEC-03 链完整性校验覆盖（最小非空链可对账）。
        """
        writer = AuditWriter(InMemoryAuditStorage())
        writer.append(AuditEvent(actor="u", action_type=AuditEventType.LOGIN, target="auth"))
        rows = list(writer.storage.iter_rows())
        assert len(rows) == 1 and rows[0].prev_hash == GENESIS_HASH
        report = writer.verify_chain()
        assert report.ok
        assert report.checked_until_id == 1
        assert report.chain_head_hash == rows[0].row_hash

    def test_tamper_first_row_detected(self) -> None:
        """【边界】链首行被篡改：对账报 row_tampered（id=1）——首行也是审计面。

        KPI 映射：NFR-SEC-03 篡改告警检出率 100%（含链首）。
        """
        storage = InMemoryAuditStorage()
        writer = AuditWriter(storage)
        writer.append(AuditEvent(actor="u", action_type=AuditEventType.KB_CHANGE, target="k:0"))
        writer.append(AuditEvent(actor="u", action_type=AuditEventType.KB_CHANGE, target="k:1"))
        storage.rows[0].actor = "mallory"
        report = writer.verify_chain()
        assert not report.ok
        tampered = [p for p in report.problems if p["kind"] == "row_tampered"]
        assert tampered and tampered[0]["id"] == 1

    def test_delete_first_row_detected(self) -> None:
        """【边界】链首行被删除：对账报 gap_or_delete（expect 1 / found 2）+ 断链。

        KPI 映射：NFR-SEC-03 删除检出率（链首删除同样不留死角）。
        """
        storage = InMemoryAuditStorage()
        writer = AuditWriter(storage)
        for i in range(3):
            writer.append(AuditEvent(actor="u", action_type=AuditEventType.KB_CHANGE, target=f"k:{i}"))
        storage.rows.pop(0)  # 删除链首（id=1）
        report = writer.verify_chain()
        assert not report.ok
        gaps = [p for p in report.problems if p["kind"] == "gap_or_delete"]
        assert gaps and gaps[0]["expect_id"] == 1 and gaps[0]["found_id"] == 2
        assert any(p["kind"] == "chain_break" for p in report.problems)  # 次行 prev≠GENESIS 连带断链

    def test_canonical_bool_stringized(self) -> None:
        """【边界】detail 布尔值统一字符串化：True/"true"、1/"1" 哈希一致（bool 先于 int 判定）。

        KPI 映射：NFR-SEC-03 哈希链写入/对账两侧逐字节一致。
        """
        assert canonical_payload(_canonical_row(detail={"f": True})) == canonical_payload(
            _canonical_row(detail={"f": "true"})
        )
        assert canonical_payload(_canonical_row(detail={"n": 1})) == canonical_payload(
            _canonical_row(detail={"n": "1"})
        )
        # bool 与数值不同形（True ≠ 1 的哈希输入，"true" ≠ "1"）
        assert canonical_payload(_canonical_row(detail={"f": True})) != canonical_payload(
            _canonical_row(detail={"f": 1})
        )

    def test_ip_participates_in_hash(self) -> None:
        """【等价类】ip 字段参与内容哈希：None 与具体 IP 哈希不同（来源可追溯不可篡改）。

        KPI 映射：NFR-SEC-03 审计字段完整参与防篡改（哈希输入字段集无遗漏）。
        """
        assert canonical_payload(_canonical_row(ip=None)) != canonical_payload(
            _canonical_row(ip="10.0.0.8")
        )

    def test_concurrent_same_event_id_appended_once(self) -> None:
        """【并发】多线程并发追加同一 event_id：串行化下仅入链一次，全部拿到同一 id。

        KPI 映射：NFR-SEC-03 幂等去重（at-least-once 消费 × 并发竞态下不重复入链）。
        """
        writer = AuditWriter(InMemoryAuditStorage())
        workers, ids, lock = 12, [], threading.Lock()

        def produce() -> None:
            row_id = writer.append(
                AuditEvent(
                    actor="u", action_type=AuditEventType.SENSITIVE_CONFIRM,
                    target="price_draft:1", event_id="evt-race-1",
                )
            )
            with lock:
                ids.append(row_id)

        threads = [threading.Thread(target=produce) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(ids) == workers and len(set(ids)) == 1      # 全部返回同一既有 id
        assert len(list(writer.storage.iter_rows())) == 1      # 仅入链一次
        assert writer.verify_chain().ok

    def test_verify_chain_consistent_during_concurrent_append(self) -> None:
        """【并发】追加进行中的任意时点对账：报告恒 pass（append 读-算-写全程持锁，
        对账快照看到的每行都是完整哈希行——无半成品行进入校验视野）。

        KPI 映射：NFR-SEC-03 对账可用性（在线对账不误报、不读脏链）。
        """
        writer = AuditWriter(InMemoryAuditStorage())
        stop = threading.Event()

        def produce() -> None:
            i = 0
            while not stop.is_set() and i < 300:
                writer.append(
                    AuditEvent(actor="ai", action_type=AuditEventType.LOGIN, target=f"auth:{i}")
                )
                i += 1

        producer = threading.Thread(target=produce)
        producer.start()
        reports = [writer.verify_chain() for _ in range(25)]
        producer.join()
        stop.set()
        assert all(r.ok for r in reports), next(p for r in reports if not r.ok).problems
        assert writer.verify_chain().ok  # 收尾全量对账仍通过
