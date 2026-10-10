# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Milvus 真实对拍集成测试（容器化部署联调，迭代 0.3 收口）。

同一批 VectorRecord 分别灌入 InMemoryVectorStore 与 MilvusStore（本地容器
milvus v2.5，外部 etcd + 本地存储形态），同一组查询断言两者 top-k 的
chunk_id 集合高度一致（RRF 实现细节允许 ≤1 位差异，命中集合应一致）。

运行条件：环境变量 KB_MILVUS_URI（缺省 http://localhost:19530）；
容器栈：docker compose -f deploy/compose/compose.yaml up -d
不可连接时整文件跳过（不影响 CI）。
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient  # noqa: E402
from services.kb.core.vectorstore.base import VectorRecord  # noqa: E402
from services.kb.core.vectorstore.inmemory_impl import InMemoryVectorStore  # noqa: E402
from services.kb.core.vectorstore.milvus_impl import MilvusStore  # noqa: E402

MILVUS_URI = os.environ.get("KB_MILVUS_URI", "http://localhost:19530")
IT_COLLECTION = "it_kb_paridad"

KB_TYPES = ["product", "customer", "skill", "process", "negative_case", "training"]


def _milvus_reachable() -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{MILVUS_URI.replace('19530', '9091')}/healthz", timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _milvus_reachable(), reason=f"Milvus 不可连接（{MILVUS_URI}）——集成测试跳过"
)


def _make_records(n: int = 50, seed: int = 7) -> list[VectorRecord]:
    """确定性构造 n 条覆盖六库与过滤维度的记录（Fake 向量）。"""
    rnd = random.Random(seed)
    fake = FakeEmbeddingClient()
    texts = [f"SIM{i:03d} 素材文本 关键词组{i % 7}" for i in range(n)]
    emb = fake.embed_batch(texts)
    records = []
    for i in range(n):
        records.append(
            VectorRecord(
                chunk_id=i + 1,
                kb_type=KB_TYPES[i % 6],
                dense=emb.dense[i],
                sparse=emb.sparse[i],
                sku=f"ST-{1000 + i}" if i % 3 == 0 else "",
                scene_tag=f"场景{i % 5}" if i % 4 == 0 else "",
                item_status="published",
            )
        )
    del rnd
    return records


@pytest.fixture(scope="module")
def stores():
    """两套存储 + 同批数据；模块级复用，结束时清理 Milvus 测试集合。"""
    mem = InMemoryVectorStore()
    mv = MilvusStore(uri=MILVUS_URI, collection=IT_COLLECTION)
    records = _make_records()
    mem.upsert(records)
    mv.upsert(records)
    yield mem, mv
    try:
        mv._client.drop_collection(IT_COLLECTION)  # noqa: SLF001 测试清理
    except Exception:  # noqa: BLE001
        pass


def _ids(hits) -> set[int]:
    return {h.chunk_id for h in hits}


def test_向量主路_两实现结果集一致(stores):
    """dense 单路：Fake 向量余弦值连续无并列 → 两实现 top5 集合应一致（HNSW
    近似 vs 精确扫允许个别差异，交集 ≥4）；锚点语义仅存在于稀疏路（见下一用例）。
    """
    mem, mv = stores
    fake = FakeEmbeddingClient()
    _r = fake.embed_batch(["SIM005 素材文本"])
    qd, qs = _r.dense[0], _r.sparse[0]
    a = _ids(mem.hybrid_search(dense=qd, top_k=5))
    b = _ids(mv.hybrid_search(dense=qd, top_k=5))
    assert a and b and len(a & b) >= 4, f"InMemory={sorted(a)} vs Milvus={sorted(b)}"


def test_稀疏主路_锚点块双方命中且居前(stores):
    """SIM012 → chunk13：稀疏 IP 手算该块最高（唯一双词元命中），双方应排第一。"""
    mem, mv = stores
    fake = FakeEmbeddingClient()
    _r = fake.embed_batch(["SIM012 素材文本"])
    qd, qs = _r.dense[0], _r.sparse[0]
    a = [h.chunk_id for h in mem.hybrid_search(sparse=qs, top_k=5)]
    b = [h.chunk_id for h in mv.hybrid_search(sparse=qs, top_k=5)]
    assert a[0] == 13 and b[0] == 13, f"锚点 chunk13 应双方第一：{a} vs {b}"


def test_标量过滤_两实现结果集合一致(stores):
    mem, mv = stores
    fake = FakeEmbeddingClient()
    _r = fake.embed_batch(["素材文本 关键词组"])
    qd, qs = _r.dense[0], _r.sparse[0]
    expr = "kb_type in ('product','skill')"
    a = _ids(mem.hybrid_search(dense=qd, expr=expr, top_k=10))
    b = _ids(mv.hybrid_search(dense=qd, expr=expr, top_k=10))
    # 过滤语义一致：双方都只含 product/skill；并集差异 ≤1（内存实现正分才召回，
    # 较 Milvus 可能少并列尾部一项——已记录为两实现已知语义差异，评审定夺）
    _allowed = {n for n in range(1, 51) if KB_TYPES[(n - 1) % 6] in ("product", "skill")}
    assert a and b and a <= _allowed and b <= _allowed, "过滤语义：只应返回 product/skill"
    assert b and len(a & b) >= min(len(a), len(b)) - 1, f"{sorted(a)} vs {sorted(b)}"


def test_top_k_截断_两实现规模一致(stores):
    mem, mv = stores
    fake = FakeEmbeddingClient()
    _r = fake.embed_batch(["素材文本"])
    qd, qs = _r.dense[0], _r.sparse[0]
    assert len(mem.hybrid_search(sparse=qs, top_k=7)) == 7
    assert len(mv.hybrid_search(sparse=qs, top_k=7)) == 7
