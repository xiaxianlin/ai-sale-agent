# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""KB 管理面 TDD 补齐测试（FR-KB-020/021/022/016，SR §9 之外的五类场景补强）。

按 TDD 新规五类场景审计既有 test_admin_api_*/test_import_*/test_versioning_*/
test_evaluation_* 后补齐（覆盖四个 SR，每类标注归属）：

- KB-020 管理后台：分页 page_size=1/100 边界、store 层并发建档同 uid 恰一胜者（并发）、
  归档态状态环全部出边 40001（等价类）、两整圈状态环 + 归档软删 + 删后 40401（边界）；
- KB-021 批量导入：损坏 xlsx（扩展名合法、内容非 zip）→ ImportValidationError（2026-10-10 红→绿修复）、
  行数上限恰好/超出边界（小样本模拟 10 万行口径）、多分片水位推进、并发双 process 同
  job 恰一次创建（并发）；
- KB-022 版本控制：全差异 diff（等价类）、回滚到当前版本（边界）、多跳回滚链历史快照
  逐字节不变（边界）、store 层并发 append 同版本恰一入链（并发）；
- KB-016 评测：全失败 runner 保守分母 + 门禁阻断（降级）、首次运行无基线无底线恒通过
  （边界钉死）、归因瀑布四分支含默认兜底全覆盖（等价类）。

KPI 映射：KB-020/021/022 → SRS §16.1「后台易用性」（分页不丢不重、导入可续跑、
版本链可回放）；KB-016 → SRS §16.1「准确率」（Top5 ≥90% 门禁的保守分母与归因闭环）。

依据文档：docs/07-实现方案/KB/SR-KB-020/021/022/016-*.md §9；docs/03-技术文档/02-开发规范.md §3。
"""
from __future__ import annotations

import asyncio
import threading
import zipfile
from typing import Any

import pytest

from services.kb.api.admin.items import InMemoryKnowledgeItemStore, KnowledgeItemRecord
from services.kb.core.evaluation.dataset import EvalCase
from services.kb.core.evaluation.gate import evaluate_gate
from services.kb.core.evaluation.metrics import (
    ATTR_CHUNKING,
    ATTR_EMBEDDING,
    ATTR_KNOWLEDGE_MISSING,
    ATTR_REWRITE,
    AttributionContext,
    RuleBasedMissClassifier,
    aggregate,
    attribution_distribution,
    classify_misses,
)
from services.kb.core.evaluation.runner import EvalCaseResult, EvalRunner
from services.kb.core.import_service.pipeline import (
    ImportConfig,
    ImportValidationError,
)
from services.kb.core.versioning import VersioningService, VersionSnapshot
from services.kb.core.versioning.diff import diff_snapshots
from services.kb.core.versioning.store import InMemoryVersionStore
from services.kb.tests.test_admin_api_items import StubSchemaProvider, make_client, post_item
from services.kb.tests.test_import_pipeline import (
    PRODUCT_HEADER,
    build_csv_bytes,
    make_service,
    valid_row,
)

ITEMS = "/api/v1/knowledge/items"


# ─────────────────────────── KB-020 管理后台（KPI：SRS §16.1 后台易用性） ───────────────────────────


class TestKb020PaginationAndConcurrency:
    def test_page_size_one_and_max_boundary(self) -> None:
        """边界：page_size=1 逐页不丢不重；page_size=100 上限全量放行且回显口径一致。"""
        client = make_client(provider=StubSchemaProvider())
        for index in range(3):
            post_item(client, title=f"条目 {index}", metadata={"sku": f"SKU-{index}"})
        seen: list[str] = []
        for page in (1, 2, 3):
            data = client.get(ITEMS, params={"page": page, "page_size": 1}).json()["data"]
            assert data["total"] == 3 and len(data["items"]) == 1
            assert data["page"] == page and data["page_size"] == 1
            seen.append(data["items"][0]["uid"])
        assert len(set(seen)) == 3  # 三页并集恰好覆盖全量（不丢不重）
        full = client.get(ITEMS, params={"page": 1, "page_size": 100}).json()["data"]
        assert full["total"] == 3 and len(full["items"]) == 3 and full["page_size"] == 100

    def test_store_concurrent_create_same_uid_exactly_once(self) -> None:
        """并发：8 线程并发建档同 uid → 恰 1 成功，其余 ValueError（对齐 PG 主键冲突语义）。"""
        store = InMemoryKnowledgeItemStore()
        successes: list[int] = []
        conflicts: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()  # 同步起跑，最大化竞态窗口
            try:
                store.create(KnowledgeItemRecord(kb_type="product", title=f"t{index}", uid="k-fixed"))
                won = 1
            except ValueError:
                won = 0
            with lock:
                (successes if won else conflicts).append(index)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(successes) == 1 and len(conflicts) == 7
        assert len(list(store.iter_all())) == 1


class TestKb020StateRing:
    def test_archived_state_all_outgoing_edges_rejected(self) -> None:
        """等价类：归档态的状态环出边（publish/save_draft/archive）全部 40001，仅 restore 合法。"""
        client = make_client(provider=StubSchemaProvider())
        uid = post_item(client, action="publish")["uid"]
        body: dict[str, Any] = {
            "kb_type": "product", "title": "归档后改题", "content": "c", "metadata": {"sku": "ST-3200"},
        }
        assert (
            client.put(f"{ITEMS}/{uid}", json={**body, "action": "archive"}).json()["data"]["status"]
            == "archived"
        )  # 先合法归档，进入被测状态
        assert client.put(f"{ITEMS}/{uid}", json={**body, "action": "publish"}).json()["code"] == 40001
        assert client.put(f"{ITEMS}/{uid}", json={**body, "action": "save_draft"}).json()["code"] == 40001
        resp = client.put(f"{ITEMS}/{uid}", json={**body, "action": "archive"})
        assert resp.status_code == 400 and resp.json()["data"]["errors"][0]["field"] == "action"
        # 唯一合法出边 restore → 回草稿态
        restored = client.put(
            f"{ITEMS}/{uid}", json={**body, "title": "恢复标题", "action": "restore"}
        ).json()["data"]
        assert restored["status"] == "draft"

    def test_two_full_state_cycles_then_archive_delete(self) -> None:
        """边界：状态环跑满两圈（draft→publish→archive→restore×2）+ 归档软删 + 删后 40401。"""
        client = make_client(provider=StubSchemaProvider())
        uid = post_item(client, action="publish")["uid"]

        def put(action: str, title: str) -> dict[str, Any]:
            body = {
                "kb_type": "product", "title": title, "content": "c", "metadata": {"sku": "ST-3200"},
                "action": action,
            }
            return client.put(f"{ITEMS}/{uid}", json=body).json()["data"]

        assert put("archive", "环-归档一")["status"] == "archived"
        assert put("restore", "环-恢复一")["status"] == "draft"      # 第一圈闭环
        assert put("publish", "环-发布二")["status"] == "published"  # 第二圈
        assert put("archive", "环-归档二")["status"] == "archived"
        assert client.delete(f"{ITEMS}/{uid}").json()["data"]["deleted"] is True  # 归档态可软删
        assert client.get(f"{ITEMS}/{uid}").json()["code"] == 40401  # 软删后不可见


# ─────────────────────────── KB-021 批量导入（KPI：SRS §16.1 后台易用性） ───────────────────────────


class TestKb021UploadBoundaryAndConcurrency:
    def test_corrupt_xlsx_declared_extension_maps_validation_error(self) -> None:
        """异常：声明 .xlsx 但内容非 zip → 收敛为 ImportValidationError（2026-10-10 红→绿修复）。"""
        service, _hook = make_service()
        with pytest.raises(ImportValidationError, match="解析失败|没有数据行"):
            service.create_job(file_name="清单.xlsx", file_bytes=b"not-a-real-xlsx", kb_type="product")

    def test_max_rows_boundary_exact_accept_over_reject(self) -> None:
        """边界：行数上限恰等（5/5）放行跑完 done；超出（6 行）建档即 40001（10 万行口径小样本模拟）。"""
        config = ImportConfig(max_rows=5, chunk_rows=2)
        service, hook = make_service(config=config)
        rows_exact = [valid_row(title=f"t{i}", sku=f"SKU-{i:03d}") for i in range(5)]
        job = service.create_job(
            file_name="x.csv", file_bytes=build_csv_bytes(PRODUCT_HEADER, rows_exact), kb_type="product"
        )
        done = asyncio.run(service.process(job.job_id))
        assert done.status == "done" and done.valid_rows == 5 and len(hook.items) == 5
        rows_over = [valid_row(title=f"t{i}", sku=f"SKU-{i:03d}") for i in range(6)]
        with pytest.raises(ImportValidationError, match="不超过 5 行"):
            service.create_job(
                file_name="x.csv", file_bytes=build_csv_bytes(PRODUCT_HEADER, rows_over), kb_type="product"
            )

    def test_multi_chunk_watermark_progress(self) -> None:
        """边界：chunk_rows=2、5 行 → 3 个分片全部处理，水位与进度精确到 100%。"""
        service, hook = make_service(config=ImportConfig(chunk_rows=2))
        rows = [valid_row(title=f"t{i}", sku=f"SKU-{i:03d}") for i in range(5)]
        job = service.create_job(
            file_name="x.csv", file_bytes=build_csv_bytes(PRODUCT_HEADER, rows), kb_type="product"
        )
        done = asyncio.run(service.process(job.job_id))
        assert done.processed_rows == 5 and done.valid_rows == 5 and len(done.item_ids) == 5
        assert done.progress_pct == 100
        assert len(hook.items) == 5  # 三个分片无一行遗漏

    def test_concurrent_process_same_job_creates_exactly_once(self) -> None:
        """并发：同 job 双 process（running 幂等入口）→ 每行恰一次创建、状态轨迹无重复 running。"""
        service, hook = make_service(config=ImportConfig(chunk_rows=2))
        rows = [valid_row(title=f"t{i}", sku=f"SKU-{i:03d}") for i in range(6)]
        job = service.create_job(
            file_name="x.csv", file_bytes=build_csv_bytes(PRODUCT_HEADER, rows), kb_type="product"
        )

        async def run_both() -> None:
            await asyncio.gather(service.process(job.job_id), service.process(job.job_id))

        asyncio.run(run_both())
        assert job.status == "done" and job.valid_rows == 6
        assert len(job.item_ids) == len(set(job.item_ids)) == 6  # 恰一次创建（续跑幂等）
        assert len(hook.items) == 6
        assert job.status_history.count("running") == 1  # 单执行者推进，无双 running


# ─────────────────────────── KB-022 版本控制（KPI：SRS §16.1 后台易用性） ───────────────────────────


def _versioning_payload(**overrides: Any) -> dict[str, Any]:
    """最小合法条目载荷（业务五字段）。"""
    base: dict[str, Any] = {
        "kb_type": "product",
        "title": "ST-3200 支架",
        "content": "工业级铝合金支架。",
        "metadata": {"sku": "ST-3200"},
        "status": "draft",
    }
    base.update(overrides)
    return base


def _make_versioning_service() -> tuple[VersioningService, InMemoryVersionStore]:
    store = InMemoryVersionStore()
    return VersioningService(store), store


class TestKb022DiffAndRollbackChain:
    def test_full_diff_all_fields_changed_summary(self) -> None:
        """等价类：全差异（五业务字段全部不同）→ changed=5、无增删，summary 计数闭合。"""
        snap_a = _versioning_payload(
            kb_type="product", title="旧题", content="旧正文", metadata={"sku": "A"}, status="draft"
        )
        snap_b = _versioning_payload(
            kb_type="customer", title="新题", content="新正文", metadata={"company": "Acme"}, status="published"
        )
        result = diff_snapshots(snap_a, snap_b, from_version=1, to_version=2)
        assert result.added == () and result.removed == ()
        assert {change.field for change in result.changed} == {"kb_type", "title", "content", "metadata", "status"}
        assert result.summary() == {"changed_fields": 5, "modified": 5, "added": 0, "removed": 0}
        assert result.is_empty() is False

    def test_rollback_to_current_version_appends_referencing_version(self) -> None:
        """边界：回滚目标=当前版本（to_version=current）→ 仍追加引用新版本，链完整不报错。"""
        service, _ = _make_versioning_service()
        item, v1 = service.create_item(_versioning_payload(), actor="admin")
        service.save(item.item_id, _versioning_payload(title="改题"), actor="admin")
        v3 = service.rollback(item.item_id, 2, actor="admin", reason="误改回退")
        assert v3.version == 3 and v3.rollback_from == 2
        assert v3.snapshot_json == service.get_snapshot(item.item_id, 2).snapshot_json
        assert v1.rollback_from is None
        assert [snap.version for snap in service.list_versions(item.item_id)] == [1, 2, 3]

    def test_multi_hop_rollback_chain_history_immutable(self) -> None:
        """边界：V1..V3 + 两跳回滚（→V1→V2）→ 历史快照逐字节不变、rollback_from 链可回放。"""
        service, _ = _make_versioning_service()
        payloads = [
            _versioning_payload(title="原始题"),
            _versioning_payload(title="第二次编辑"),
            _versioning_payload(title="第三次编辑"),
        ]
        item, _v1 = service.create_item(payloads[0], actor="admin")
        service.save(item.item_id, payloads[1], actor="admin")
        service.save(item.item_id, payloads[2], actor="admin")
        service.rollback(item.item_id, 1, actor="admin")  # V4 引用 V1
        service.rollback(item.item_id, 2, actor="admin")  # V5 引用 V2（多跳）
        snapshots = service.list_versions(item.item_id)
        assert [snap.version for snap in snapshots] == [1, 2, 3, 4, 5]
        assert [snap.rollback_from for snap in snapshots] == [None, None, None, 1, 2]
        for index, payload in enumerate(payloads):  # 历史不可变：逐字节比对原始载荷
            assert snapshots[index].snapshot_json == payload
        assert snapshots[3].snapshot_json == payloads[0]  # V4 内容 = V1
        assert snapshots[4].snapshot_json == payloads[1]  # V5 内容 = V2

    def test_store_concurrent_append_same_version_single_entry(self) -> None:
        """并发：8 线程并发 append 同 (item, version) → UNIQUE 幂等收敛为恰一入链。"""
        store = InMemoryVersionStore()
        record = store.create_item(_versioning_payload(), "admin")
        store.replace_item(
            record.item_id, _versioning_payload(), expected_version=0, new_version=1, updated_by="admin"
        )
        snapshot = VersionSnapshot(version=1, snapshot_json=_versioning_payload(), created_by="admin")
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            store.append_snapshot(record.item_id, snapshot)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        chain = store.list_snapshots(record.item_id)
        assert len(chain) == 1 and chain[0].version == 1  # 事件重放并发下仍恰一


# ─────────────────────────── KB-016 评测（KPI：SRS §16.1 准确率 Top5 ≥90%） ───────────────────────────


def _case(case_id: str, query: str, expected: tuple[int, ...]) -> EvalCase:
    """构造合法评测用例（product 库）。"""
    return EvalCase(case_id=case_id, query=query, expected_chunk_ids=expected, kb_type="product")


class TestKb016DegradationAndAttribution:
    def test_all_fail_runner_conservative_denominator_and_gate_blocks(self) -> None:
        """降级：并发 runner 全失败 → 失败样本按未命中计入分母（n 保留），门禁对基线阻断。"""

        def broken_search(**kwargs: Any) -> Any:
            raise TimeoutError("检索下游不可用")

        runner = EvalRunner(search_fn=broken_search, concurrency=4)
        cases = [_case(f"c{i}", f"问题 {i}", (i + 1,)) for i in range(5)]
        results = runner.run(cases)
        assert all(item.hit_at_5 is False and item.error for item in results)  # 全部降级留痕
        metrics = aggregate(results, cases)
        assert (metrics.n, metrics.top5_acc, metrics.mrr) == (5, 0.0, 0.0)  # 保守分母不剔除
        baseline = aggregate(
            [
                EvalCaseResult.from_rank(case_id=f"c{i}", hit_rank=1, latency_ms=1)
                for i in range(5)
            ],
            cases,
        )
        gate = evaluate_gate(metrics, baseline)
        assert gate.passed is False
        assert any("总体" in reason and "回落" in reason for reason in gate.reasons)

    def test_first_run_no_baseline_no_floor_always_passes(self) -> None:
        """边界（钉死口径）：首次运行无基线且未启用绝对门槛 → 相对门槛整体跳过恒通过（仅记录）。"""
        from services.kb.core.evaluation.metrics import RunMetrics

        zero = RunMetrics(n=10, hit_count=0, top5_acc=0.0, mrr=0.0)
        gate = evaluate_gate(zero, None)  # baseline=None 且 floor_pct=None
        assert gate.passed is True and gate.reasons == ()

    def test_miss_attribution_waterfall_all_four_branches(self) -> None:
        """等价类：归因瀑布四分支全覆盖（知识缺失/改写/分块/向量化默认兜底），分布恒全键。"""
        classifier = RuleBasedMissClassifier()
        # ① knowledge_missing：期望块不在已入库集合（已知全缺）
        miss_known = EvalCaseResult(case_id="m1", hit_at_5=False, mrr=0.0, latency_ms=1, retrieved_ids=(9,))
        case_known = _case("m1", "ST-3200 承载", (1,))
        ctx_empty = AttributionContext(indexed_chunk_ids=frozenset())
        assert classifier.classify(miss_known, case_known, ctx_empty) == ATTR_KNOWLEDGE_MISSING
        # ② rewrite：语料可见性未知，改写文本为空
        miss_rewrite = EvalCaseResult(case_id="m2", hit_at_5=False, mrr=0.0, latency_ms=1, retrieved_ids=(9,))
        assert classifier.classify(miss_rewrite, _case("m2", "ST-3200 承载", (1,)), AttributionContext()) == ATTR_REWRITE
        # ③ chunking：改写保留关键实体、邻居块命中（语义被切散）
        miss_chunk = EvalCaseResult(
            case_id="m3", hit_at_5=False, mrr=0.0, latency_ms=1, retrieved_ids=(77,), rewritten_query="ST-3200 的承载参数"
        )
        ctx_neighbor = AttributionContext(
            indexed_chunk_ids=frozenset({1}), neighbor_ids={1: frozenset({77})}
        )
        assert classifier.classify(miss_chunk, _case("m3", "ST-3200 承载", (1,)), ctx_neighbor) == ATTR_CHUNKING
        # ④ embedding（默认兜底）：已入库、改写正常、邻居未命中
        miss_embed = EvalCaseResult(
            case_id="m4", hit_at_5=False, mrr=0.0, latency_ms=1, retrieved_ids=(99,), rewritten_query="ST-3200 的承载参数"
        )
        ctx_no_neighbor = AttributionContext(indexed_chunk_ids=frozenset({1}))
        assert classifier.classify(miss_embed, _case("m4", "ST-3200 承载", (1,)), ctx_no_neighbor) == ATTR_EMBEDDING
        # 汇总：分布四类恒全键（0 计保留）
        cases = [_case("m1", "q", (1,)), _case("m2", "q", (1,)), _case("m3", "q", (1,)), _case("m4", "q", (1,))]
        results = [miss_known, miss_rewrite, miss_chunk, miss_embed]
        attributions = classify_misses(results, cases, classifier=classifier, context=AttributionContext())
        distribution = attribution_distribution({**attributions, "m1": ATTR_KNOWLEDGE_MISSING})
        assert set(distribution) == {ATTR_KNOWLEDGE_MISSING, ATTR_REWRITE, ATTR_CHUNKING, ATTR_EMBEDDING}
