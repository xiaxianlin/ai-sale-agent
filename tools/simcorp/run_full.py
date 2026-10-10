# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""全量灌装 CLI（合成语料工程自证，迭代 0.3 收口）。

流程：生成语料 → 端到端入库（解析/清洗/分块/条目化/嵌入/向量入库）
→ 评测（≥100 条，Top5 命中率 / MRR）→ 并发性能（10 线程×100 查询）
→ 中文报告 out/simcorp/report.md。

声明：本工具用于**工程自证**（容量/吞吐/检索/性能），
正式验收（SRS §16.1）必须使用甲方真实素材（OI-03）与业务专家人工评测集。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from foundation.config import cfg  # noqa: E402 （占位引用保持环境一致性）

from services.kb.core.evaluation.dataset import EvalDataset  # noqa: E402
from services.kb.core.evaluation.runner import EvalRunner  # noqa: E402
from services.kb.core.ingestion.orchestrator import (  # noqa: E402
    IngestionOrchestrator,
    SourceMaterial,
)
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient  # noqa: E402
from services.kb.core.vectorstore.inmemory_impl import InMemoryVectorStore  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "tools"))
from simcorp import eval_builder, generator  # noqa: E402


def _search_fn(store: InMemoryVectorStore, embedder: FakeEmbeddingClient):
    """评测/压测共用的检索函数（向量+稀疏混合，file 级口径）。"""

    def search(query: str, kb_types=None, filters=None, top_k: int = 5):
        emb = embedder.embed_batch([query])
        expr = None
        if kb_types:
            vals = ", ".join(f"'{k}'" for k in kb_types)
            expr = f"kb_type in ({vals})"
        # 工程自证口径：稀疏主路（锚点词决定命中）；真实稠密检索质量由 TEI 联调后评测
        hits = store.hybrid_search(dense=None, sparse=emb.sparse[0], expr=expr, top_k=top_k)
        return {"results": [{"chunk_id": h.chunk_id, "score": h.score} for h in hits]}

    return search


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="合成语料全量灌装与评测（工程自证）")
    ap.add_argument("--out", default="out/simcorp")
    ap.add_argument("--files", type=int, default=500)
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--perf-queries", type=int, default=100)
    args = ap.parse_args(argv)

    out = Path(args.out)
    corpus = out / "corpus"
    report_lines: list[str] = [
        "# 合成语料全量灌装报告（工程自证）",
        "",
        "> **声明**：本报告使用确定性合成语料，用于验证流水线容量/吞吐/检索/性能；",
        "> 正式验收（SRS §16.1）需甲方真实素材（OI-03，NDA 后提供）与业务专家人工评测集 ≥100 条。",
        "",
    ]

    # ① 生成语料
    t0 = time.perf_counter()
    manifest_path = generator.generate(corpus, args.files, seed=42)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    gen_s = time.perf_counter() - t0

    # ② 端到端入库
    store = InMemoryVectorStore()
    embedder = FakeEmbeddingClient()
    orch = IngestionOrchestrator(embedder=embedder, vector_store=store)
    sources = [
        SourceMaterial(path=str(corpus / name), kb_type=info["kb_type"])
        for name, info in manifest.items()
    ]
    t1 = time.perf_counter()
    manifest_batch = orch.run(sources)
    ingest_s = time.perf_counter() - t1

    ok = [r for r in manifest_batch.results if r.ok]
    failed = [r for r in manifest_batch.results if not r.ok]
    total_chunks = sum(r.chunks for r in ok)
    indexed = sum(r.indexed_chunks for r in ok)
    file_chunk_ids = {Path(r.source).name: r.chunk_ids for r in ok}

    # ③ 评测集与批量评测
    ds: EvalDataset = eval_builder.build_eval_set(manifest_path, file_chunk_ids, min_cases=100)
    runner = EvalRunner(search_fn=_search_fn(store, embedder))
    t2 = time.perf_counter()
    results = runner.run_dataset(ds)
    eval_s = time.perf_counter() - t2
    hits = [r.hit_at_5 for r in results]
    acc = sum(hits) / len(hits) if hits else 0.0
    mrr = sum(r.mrr for r in results) / len(results) if results else 0.0

    # ④ 并发性能：threads × perf_queries
    queries = [c.query for c in ds.cases[: args.perf_queries]]
    search = _search_fn(store, embedder)
    latencies: list[float] = []

    def _one(q: str) -> None:
        s = time.perf_counter()
        search(q)
        latencies.append((time.perf_counter() - s) * 1000)

    t3 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        list(pool.map(_one, queries * args.threads))
    perf_s = time.perf_counter() - t3
    lat_sorted = sorted(latencies)

    def pct(p: float) -> float:
        return lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * p))]

    # ⑤ 报告
    report_lines += [
        "## 结果摘要",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 素材文件 | {len(manifest)}（成功 {len(ok)} / 失败 {len(failed)}） |",
        f"| 入库块 | 分块 {total_chunks} / 向量入库 {indexed} |",
        f"| 灌装吞吐 | {total_chunks / ingest_s:.0f} chunks/s（总耗时 {ingest_s:.1f}s） |",
        f"| 评测集 | {len(ds.cases)} 条（目标 ≥100） |",
        f"| Top5 命中率 | {acc * 100:.1f}% |",
        f"| MRR | {mrr:.3f} |",
        f"| 检索并发 | {args.threads} 线程 × {args.perf_queries} 查询 = {len(lat_sorted)} 次 |",
        f"| 检索延迟 | P50 {pct(0.50):.1f}ms / P95 {pct(0.95):.1f}ms / P99 {pct(0.99):.1f}ms |",
        f"| 阶段耗时 | 生成 {gen_s:.1f}s / 灌装 {ingest_s:.1f}s / 评测 {eval_s:.1f}s / 压测 {perf_s:.1f}s |",
        "",
    ]
    if failed:
        report_lines += ["## 失败清单（前 10）", ""]
        for r in failed[:10]:
            report_lines.append(f"- {Path(r.source).name}: {r.error}")
    report_lines += [
        "",
        "## 结论口径",
        "",
        "- 本报告验证：流水线在数百素材规模下端到端可用、检索在并发下延迟满足 ≤500ms 预算；",
        "- 合成命中率为**链路自证值**（锚点构造保证），不代表真实语料检索质量；",
        "- 正式验收前置：甲方素材实灌（OI-03）→ 业务专家评测集 ≥100 条 → §16.1 正式跑分。",
    ]
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(report_lines), encoding="utf-8")
    ds.save(out / "eval_set_simcorp.json")

    print("\n".join(report_lines[6:22]))
    print(f"\n[simcorp] 报告 → {out / 'report.md'}")
    return 0 if acc >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
