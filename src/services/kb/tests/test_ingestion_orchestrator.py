# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""端到端入库编排测试（迭代 0.3 收口：Phase 0 六库入库主线，§16.1 预跑底座）。

覆盖（真实文件 → 真实全链，无 mock 截断）：
- 六库各一样例素材 → 解析→清洗→分块→条目化→嵌入→向量入库，逐库可检索断言；
- 批次台账（BatchManifest 统计）与 ingest 留痕（七段齐全、batch_id 贯穿）；
- 单文件失败不中断批次（部分成功策略）+ 失败留痕；
- metadata 覆盖注入生效 / 非法 metadata 在条目化阶段拦截。

依赖 python-docx 与 openpyxl（环境缺失时对应用例跳过，与解析器测试同口径）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services.kb.core.ingestion import (
    FILE_STATUS_FAILED,
    FILE_STATUS_SUCCESS,
    IngestionOrchestrator,
    SourceMaterial,
)
from services.kb.core.ingestion.orchestrator import STAGE_INDEX
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.vectorstore import InMemoryVectorStore

# ── 测试素材构造（六库各一：5 份 docx + 1 份 xlsx）───────────────────────────

#: 每库样例正文（(标题, [段落])；词串锚点供检索断言复用）
DOCX_FIXTURES: dict[str, tuple[str, list[str]]] = {
    "product": (
        "ST-3200 支架手册",
        ["ST-3200 为壁挂电视支架，最大承载85千克。", "认证情况：CE 认证已通过。"],
    ),
    "customer": ("客户分级标准", ["A 级客户为月采购超过五千件的经销商。"]),
    "skill": ("砍价应对话术", ["让步必须换取对等条件。"]),
    "process": ("报价流程规程", ["报价单必须包含型号与FOB单价。"]),
    "negative_case": ("丢单复盘", ["向欧洲经销商承诺十五天交货。"]),
    "training": ("新人第一课", ["支架产品线主打型号为 ST-3200。"]),
}

#: 各库检索断言词串（须与清洗后文本的 unicode \w 词串一致；product 为 xlsx 表格内容）
KB_QUERY_ANCHORS: dict[str, str] = {
    "product": "最大承载 85kg",
    "customer": "级客户为月采购超过五千件的经销商",
    "skill": "让步必须换取对等条件",
    "process": "报价单必须包含型号与FOB单价",
    "negative_case": "向欧洲经销商承诺十五天交货",
    "training": "支架产品线主打型号为 ST-3200",
}


def _write_docx(path: Path, title: str, paragraphs: list[str]) -> None:
    """写一份真实 docx（Heading 1 + Normal 段落，word_parser 按样式识别标题）。"""
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading(title, level=1)
    for text in paragraphs:
        document.add_paragraph(text)
    document.save(str(path))


def _write_xlsx(path: Path, rows: list[list[str]]) -> None:
    """写一份真实 xlsx（单 sheet 单逻辑表，excel_parser 整表发射）。"""
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    workbook.save(str(path))


def _six_kb_sources(tmp_path: Path) -> list[SourceMaterial]:
    """构造六库样例素材（product 用 xlsx 覆盖表格链路，其余五库用 docx）。"""
    sources: list[SourceMaterial] = []
    for kb_type, (title, paragraphs) in DOCX_FIXTURES.items():
        if kb_type == "product":
            # 产品库换 xlsx：标题进 sheet 名 heading 块，正文进表格块
            path = tmp_path / "product_样例.xlsx"
            _write_xlsx(path, [["参数", "数值"], ["最大承载", "85千克"], ["认证", "CE 认证已通过"]])
        else:
            path = tmp_path / f"{kb_type}_样例.docx"
            _write_docx(path, title, paragraphs)
        sources.append(SourceMaterial(str(path), kb_type))
    return sources


def _build_orchestrator(store: InMemoryVectorStore | None = None) -> IngestionOrchestrator:
    """组装内存全链编排器（FakeEmbeddingClient + InMemoryVectorStore）。"""
    return IngestionOrchestrator(
        embedder=FakeEmbeddingClient(),
        vector_store=store if store is not None else InMemoryVectorStore(),
    )


# ── 端到端：六库入库与可检索 ──────────────────────────────────────────────────


def test_六库端到端入库_逐库向量可检索(tmp_path: Path) -> None:
    """六库各一样例素材全链入库：终态 success、有入库块、逐库检索命中本库块。"""
    sources = _six_kb_sources(tmp_path)
    store = InMemoryVectorStore()
    manifest = _build_orchestrator(store).run(sources)

    assert [r.status for r in manifest.results] == [FILE_STATUS_SUCCESS] * 6
    for result in manifest.results:
        assert result.indexed_chunks >= 1, f"{result.source} 未入库任何向量块"
        assert result.item_uid, "条目化未落 uid"
        assert result.quality.get("lossless_blocks") is True, "分块账目对账失守"

    # 逐库检索：锚点词串 + kb_type 过滤 → 命中该库素材的 chunk
    embedder = FakeEmbeddingClient()
    for kb_type, query in KB_QUERY_ANCHORS.items():
        expected_ids = {
            cid for r in manifest.results if r.kb_type == kb_type for cid in r.chunk_ids
        }
        dense, sparse = next(embedder.embed_batch([query]).rows())
        hits = store.hybrid_search(
            dense=dense, sparse=sparse, expr=f"kb_type in ('{kb_type}')", top_k=5
        )
        hit_ids = {hit.chunk_id for hit in hits}
        assert hit_ids & expected_ids, f"{kb_type} 库检索未命中本库素材：{hit_ids} vs {expected_ids}"


def test_产品库维度路由_sku注入向量记录(tmp_path: Path) -> None:
    """product 库 metadata.sku 覆盖注入 → VectorRecord.sku 同值（过滤维度路由）。"""
    path = tmp_path / "product_参数.docx"
    _write_docx(path, *DOCX_FIXTURES["product"])
    store = InMemoryVectorStore()
    manifest = _build_orchestrator(store).run(
        [SourceMaterial(str(path), "product", metadata={"sku": "ST-9999", "category_path": ["电视支架"]})]
    )
    result = manifest.results[0]
    assert result.status == FILE_STATUS_SUCCESS
    # 直接验证向量库行：sku 维度随条目 metadata 路由注入
    dense, sparse = next(FakeEmbeddingClient().embed_batch(["ST-3200 为壁挂电视支架"]).rows())
    hits = store.hybrid_search(dense=dense, sparse=sparse, expr="sku = 'ST-9999'", top_k=5)
    assert {hit.chunk_id for hit in hits} == set(result.chunk_ids)


def test_非法metadata_条目化阶段拦截(tmp_path: Path) -> None:
    """metadata 违反六库内容模型（sku 格式非法）→ 本文件 failed，不入向量库。"""
    path = tmp_path / "product_坏数据.docx"
    _write_docx(path, *DOCX_FIXTURES["product"])
    store = InMemoryVectorStore()
    manifest = _build_orchestrator(store).run(
        [SourceMaterial(str(path), "product", metadata={"sku": "坏 的 SKU！"})]
    )
    result = manifest.results[0]
    assert result.status == FILE_STATUS_FAILED
    assert result.chunk_ids == []
    assert len(store) == 0
    assert "sku" in result.error


# ── 批次台账与 ingest 留痕 ────────────────────────────────────────────────────


def test_批次台账统计与七段留痕(tmp_path: Path) -> None:
    """BatchManifest 统计自洽 + 每文件 parse/dedup/correct/mask/chunk/embed/index 留痕齐全。"""
    sources = _six_kb_sources(tmp_path)
    orchestrator = _build_orchestrator()
    manifest = orchestrator.run(sources)

    assert manifest.total_files == 6
    assert manifest.success_files == 6
    assert manifest.failed_files == 0
    assert manifest.indexed_chunks == manifest.total_chunks > 0
    assert manifest.status == "done"
    assert manifest.started_at and manifest.finished_at

    rows = list(orchestrator.log_store.iter_logs())
    assert len(rows) == 6 * 7, "每文件应恰有七段留痕"
    by_file: dict[str, set[str]] = {}
    for row in rows:
        assert row.action_detail.get("batch_id") == manifest.batch_id, "留痕须贯穿批次号"
        assert row.action_detail.get("status") == "success"
        by_file.setdefault(row.source_file, set()).add(row.stage)
    expected_stages = {"parse", "dedup", "correct", "mask", "chunk", "embed", "index"}
    for source in sources:
        assert by_file.get(source.path, set()) == expected_stages
    index_rows = [r for r in rows if r.stage == STAGE_INDEX]
    for row in index_rows:
        assert row.action_detail["upserted"] == len(row.action_detail["chunk_ids"]) >= 1


def test_单文件失败不中断批次_失败留痕可见(tmp_path: Path) -> None:
    """坏文件（损坏 docx）→ 该行 failed 留痕，其余五库照常入库（部分成功策略）。"""
    sources = _six_kb_sources(tmp_path)
    bad = tmp_path / "product_坏文件.docx"
    bad.write_bytes(b"not a real docx binary")
    sources.insert(0, SourceMaterial(str(bad), "product"))

    orchestrator = _build_orchestrator()
    manifest = orchestrator.run(sources)

    assert manifest.total_files == 7
    assert manifest.success_files == 6
    assert manifest.failed_files == 1
    failed = manifest.results[0]
    assert failed.status == FILE_STATUS_FAILED
    assert failed.source == str(bad)
    assert failed.error and failed.error_code
    assert failed.chunk_ids == []

    failed_rows = [
        r for r in orchestrator.log_store.iter_logs()
        if r.source_file == str(bad) and r.action_detail.get("status") == "failed"
    ]
    assert failed_rows, "失败文件必须留有 status=failed 的留痕行"
    assert failed_rows[-1].action_detail.get("error_msg")


def test_台账序列化_roundtrip(tmp_path: Path) -> None:
    """BatchManifest.to_dict 序列化形态：统计与结果行键齐全（报告/调试消费口径）。"""
    sources = _six_kb_sources(tmp_path)
    manifest = _build_orchestrator().run(sources)
    payload: dict[str, Any] = manifest.to_dict()
    assert payload["stats"]["total_files"] == 6
    assert payload["stats"]["indexed_chunks"] == payload["stats"]["total_chunks"]
    first = payload["results"][0]
    for key in ("source", "kb_type", "status", "chunks", "indexed_chunks", "chunk_ids", "item_uid"):
        assert key in first
