# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""覆盖率核算测试（§16.1 知识库覆盖率 100% 口径，迭代 0.3）。

覆盖：全覆盖（100% 通过）/ 清单缺素材（missing）/ 编排失败（failed）/
成功但零入库块（no_chunks）/ 整库无成功条目（kb_empty）/ 批次未跑（None 全缺）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.kb.core.ingestion import (
    FILE_STATUS_SUCCESS,
    BatchManifest,
    FileResult,
    IngestionOrchestrator,
    MaterialExpectation,
    SourceMaterial,
    compute_coverage,
    material_plan_from_sources,
)
from services.kb.core.ingestion.coverage import (
    REASON_FAILED,
    REASON_KB_EMPTY,
    REASON_MISSING,
    REASON_NO_CHUNKS,
)
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.vectorstore import InMemoryVectorStore

# ── 测试素材与批次构造 ───────────────────────────────────────────────────────


def _docx_source(tmp_path: Path, name: str, kb_type: str, text: str) -> SourceMaterial:
    """写一份单段落 docx 素材（真实解析链路）。"""
    docx = pytest.importorskip("docx")
    path = tmp_path / name
    document = docx.Document()
    document.add_heading(f"{name} 标题", level=1)
    document.add_paragraph(text)
    document.save(str(path))
    return SourceMaterial(str(path), kb_type)


def _run_batch(sources: list[SourceMaterial]) -> BatchManifest:
    """跑一次内存全链入库，返回批次台账。"""
    orchestrator = IngestionOrchestrator(
        embedder=FakeEmbeddingClient(), vector_store=InMemoryVectorStore()
    )
    return orchestrator.run(sources)


def _handmade_manifest(rows: list[FileResult], sources: list[SourceMaterial]) -> BatchManifest:
    """手工构造批次台账（缺口分支的受控输入，不必跑真实链路）。"""
    return BatchManifest(
        batch_id="b-test000000000",
        sources=sources,
        results=rows,
        started_at="2026-01-01T00:00:00.000+00:00",
        finished_at="2026-01-01T00:00:01.000+00:00",
    )


def _result(source: SourceMaterial, *, status: str = FILE_STATUS_SUCCESS,
            indexed: int = 1, error: str = "") -> FileResult:
    """构造单文件结果行（success 缺省带 1 个入库块）。"""
    return FileResult(
        source=source.path,
        kb_type=source.kb_type,
        status=status,
        error=error,
        error_code="X-0000" if status != FILE_STATUS_SUCCESS else "",
        indexed_chunks=indexed,
        chunk_ids=list(range(1, indexed + 1)) if indexed else [],
    )


# ── 用例 ─────────────────────────────────────────────────────────────────────


def test_全覆盖_通过(tmp_path: Path) -> None:
    """六库素材全部成功入库：覆盖率 100%、passed、无缺口、分库计数正确。"""
    sources = [
        _docx_source(tmp_path, "产品.docx", "product", "ST-3200 支架参数与认证情况说明。"),
        _docx_source(tmp_path, "客户.docx", "customer", "客户分级标准与画像标签口径。"),
    ]
    manifest = _run_batch(sources)
    assert all(r.status == FILE_STATUS_SUCCESS for r in manifest.results)

    plan = material_plan_from_sources(manifest.sources)
    report = compute_coverage(plan, manifest)
    assert report.coverage == 1.0
    assert report.coverage_pct == 100.0
    assert report.passed is True
    assert report.gaps == []
    assert report.expected_total == report.ingested_total == 2
    assert report.per_kb == {
        "customer": {"expected": 1, "ingested": 1},
        "product": {"expected": 1, "ingested": 1},
    }


def test_清单缺素材_missing缺口(tmp_path: Path) -> None:
    """清单含未入批次素材：missing 缺口 + 覆盖率减半 + 不达标。"""
    sources = [_docx_source(tmp_path, "产品.docx", "product", "ST-3200 支架参数说明。")]
    manifest = _run_batch(sources)
    plan = [
        MaterialExpectation(source=sources[0].path, kb_type="product"),
        MaterialExpectation(source=str(tmp_path / "漏跑素材.docx"), kb_type="skill"),
    ]
    report = compute_coverage(plan, manifest)
    assert report.coverage == 0.5
    assert report.passed is False
    assert [gap.reason for gap in report.gaps][0] == REASON_MISSING
    assert report.gaps[0].kb_type == "skill"
    # 整库齐备性：skill 库清单有分配但成功 0 → 追加 kb_empty 缺口
    assert [gap.reason for gap in report.gaps][1:] == [REASON_KB_EMPTY]


def test_编排失败_failed缺口带原因(tmp_path: Path) -> None:
    """失败文件 → failed 缺口（附中文原因），成功文件照常计数。"""
    ok = SourceMaterial("/data/产品.docx", "product")
    bad = SourceMaterial("/data/坏文件.docx", "product")
    manifest = _handmade_manifest(
        [_result(ok), _result(bad, status="failed", indexed=0, error="解析失败（word_parser）：文件损坏")],
        [ok, bad],
    )
    report = compute_coverage(material_plan_from_sources([ok, bad]), manifest)
    assert report.ingested_total == 1
    assert report.coverage == 0.5
    gaps = [gap for gap in report.gaps if gap.reason == REASON_FAILED]
    assert len(gaps) == 1 and gaps[0].source == bad.path
    assert "解析失败" in gaps[0].detail


def test_成功但零入库块_no_chunks缺口(tmp_path: Path) -> None:
    """终态 success 但零向量入库（检索不可见）→ no_chunks 缺口，不计覆盖。"""
    empty = SourceMaterial("/data/空素材.docx", "training")
    manifest = _handmade_manifest([_result(empty, indexed=0)], [empty])
    report = compute_coverage(material_plan_from_sources([empty]), manifest)
    assert report.ingested_total == 0
    assert [gap.reason for gap in report.gaps] == [REASON_NO_CHUNKS, REASON_KB_EMPTY]


def test_批次未跑_全清单missing(tmp_path: Path) -> None:
    """manifest=None：全部清单素材记 missing，覆盖率 0（显式失败优于静默通过）。"""
    plan = [MaterialExpectation(source="/data/产品.docx", kb_type="product")]
    report = compute_coverage(plan, None)
    assert report.coverage == 0.0
    assert report.passed is False
    assert [gap.reason for gap in report.gaps] == [REASON_MISSING, REASON_KB_EMPTY]


def test_清单非法kb_type_构造期拒绝() -> None:
    """清单行的 kb_type 不在六库枚举 → 构造期 ValueError（fail-closed）。"""
    with pytest.raises(ValueError, match="六大知识库枚举"):
        MaterialExpectation(source="/data/x.docx", kb_type="unknown_kb")


def test_序列化形态_roundtrip() -> None:
    """CoverageReport.to_dict：计数/覆盖率/缺口中文标签齐全（报告消费口径）。"""
    ok = SourceMaterial("/data/产品.docx", "product")
    bad = SourceMaterial("/data/坏文件.docx", "skill")
    manifest = _handmade_manifest(
        [_result(ok), _result(bad, status="failed", indexed=0, error="解析失败")],
        [ok, bad],
    )
    payload = compute_coverage(material_plan_from_sources([ok, bad]), manifest).to_dict()
    assert payload["expected_total"] == 2
    assert payload["ingested_total"] == 1
    assert payload["coverage"] == 0.5
    assert payload["passed"] is False
    labels = {gap["reason_label"] for gap in payload["gaps"]}
    assert "编排失败" in labels
    assert "整库无成功条目" in labels
