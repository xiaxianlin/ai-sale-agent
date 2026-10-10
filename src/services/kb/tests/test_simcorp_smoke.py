# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""合成语料端到端冒烟（CI 可跑的小规模自证，迭代 0.3 收口）。

20 素材 → 生成 → 端到端入库 → 评测集 ≥20 条 → Top5 命中率 ≥90%。
完整规模自证（500 素材 + ≥100 条）见 tools/simcorp/run_full.py（本地/联调运行）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tools"))

from services.kb.core.evaluation.runner import EvalRunner  # noqa: E402
from services.kb.core.ingestion.orchestrator import (  # noqa: E402
    IngestionOrchestrator,
    SourceMaterial,
)
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient  # noqa: E402
from services.kb.core.vectorstore.inmemory_impl import InMemoryVectorStore  # noqa: E402
from simcorp import eval_builder, generator  # noqa: E402


def _search(store, embedder):
    def search(query, kb_types=None, filters=None, top_k=5):
        emb = embedder.embed_batch([query])
        expr = None
        if kb_types:
            vals = ", ".join(f"'{k}'" for k in kb_types)
            expr = f"kb_type in ({vals})"
        hits = store.hybrid_search(dense=None, sparse=emb.sparse[0], expr=expr, top_k=top_k)
        return {"results": [{"chunk_id": h.chunk_id, "score": h.score} for h in hits]}

    return search


def test_合成语料端到端_命中率达标(tmp_path):
    corpus = tmp_path / "corpus"
    mf = generator.generate(corpus, files=20, seed=7)
    import json

    manifest = json.loads(mf.read_text(encoding="utf-8"))
    store = InMemoryVectorStore()
    embedder = FakeEmbeddingClient()
    orch = IngestionOrchestrator(embedder=embedder, vector_store=store)
    batch = orch.run(
        [SourceMaterial(path=str(corpus / n), kb_type=i["kb_type"]) for n, i in manifest.items()]
    )
    ok = [r for r in batch.results if r.ok]
    assert len(ok) == len(manifest), "全部素材应入库成功"
    assert sum(r.indexed_chunks for r in ok) > 0

    fcids = {Path(r.source).name: r.chunk_ids for r in ok}
    ds = eval_builder.build_eval_set(mf, fcids, min_cases=20, seed=7)
    assert len(ds.cases) >= 20
    runner = EvalRunner(search_fn=_search(store, embedder))
    results = runner.run_dataset(ds)
    acc = sum(r.hit_at_5 for r in results) / len(results)
    assert acc >= 0.90, f"合成语料链路自证命中率应 ≥90%，实际 {acc:.1%}（锚点构造或检索链路回归）"
