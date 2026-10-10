# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""app 工厂管理后台挂载测试（迭代 0.3 收口：create_app 产物上 admin 端点可用）。

覆盖：
- create_app 缺省挂载（mount_admin=True）后 /api/v1/knowledge/items、
  /api/v1/knowledge/schemas、/api/v1/admin/stats 三组端点可达；
- 发布条目经 SyncHook → IncrementalSyncEngine 真实驱动向量入库（FR-KB-017
  联动），写操作留有审计事件（NFR-SEC-03 内存写入器）；
- Schema 下发来自 models/registry 真实注册表（六库齐全、product 含 sku 字段）；
- 既有链路不回归：/healthz 与统一检索 /api/v1/knowledge/search 照常可用；
- mount_admin=False 可显式关闭挂载（独立部署检索 API 时使用）。
"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from foundation.apikey.keys import KeyRegistry, QuotaConfig

from services.kb.app import InMemoryAuditTrail, RegistrySchemaProvider, create_app
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.vectorstore import InMemoryVectorStore


class _PassthroughReranker:
    """RRF 序直出重排桩（与 test_api_search 同款口径，保命中不重排）。"""

    async def rerank(self, query: str, cands: Any, top_k: int) -> list[Any]:
        return list(cands)[:top_k]


def _build_app_env() -> tuple[TestClient, InMemoryVectorStore, FakeEmbeddingClient, Any]:
    """组装 create_app 产物与 TestClient（内存向量库空库起步）。"""
    store = InMemoryVectorStore()
    embedder = FakeEmbeddingClient()
    key_registry = KeyRegistry()
    issued = key_registry.issue("admin-test", quota=QuotaConfig(qps=1000, daily=100_000))
    app = create_app(
        store,
        embedder,
        _PassthroughReranker(),
        key_registry,
        meta_provider=None,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {issued.api_key}"})
    return client, store, embedder, app


# ── 管理端点可用性 ───────────────────────────────────────────────────────────


def test_管理端点挂载可达() -> None:
    """create_app 缺省挂载后三组管理端点 200（空库形态）。"""
    client, _store, _embedder, _app = _build_app_env()

    response = client.get("/api/v1/knowledge/items")
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    assert payload["data"]["total"] == 0
    assert payload["data"]["items"] == []

    stats = client.get("/api/v1/admin/stats")
    assert stats.status_code == 200 and stats.json()["code"] == 0

    schemas = client.get("/api/v1/knowledge/schemas")
    assert schemas.status_code == 200
    assert set(schemas.json()["data"]["schemas"]) == {
        "product", "customer", "skill", "process", "negative_case", "training",
    }


def test_真实注册表下发_schema字段契约() -> None:
    """RegistrySchemaProvider：六库齐全；product 库含 sku 字段且 control=input。"""
    provider = RegistrySchemaProvider()
    schemas = {entry["kb_type"]: entry for entry in provider.list_schemas()}
    assert set(schemas) == {
        "product", "customer", "skill", "process", "negative_case", "training",
    }
    product_fields = {field["name"]: field for field in schemas["product"]["fields"]}
    assert product_fields["sku"]["required"] is True
    assert product_fields["sku"]["control"] == "input"
    assert product_fields["category_path"]["control"] == "cascader"


def test_发布条目_增量同步真实入库_审计留痕() -> None:
    """POST items(publish) → SyncHook 驱动 IncrementalSyncEngine 向量入库 + 审计事件。"""
    client, store, embedder, app = _build_app_env()

    response = client.post(
        "/api/v1/knowledge/items",
        json={
            "kb_type": "product",
            "title": "ST-3200 支架手册",
            "content": "ST-3200 为壁挂电视支架，最大承载85千克。认证情况：CE 认证已通过。",
            "metadata": {"category_path": ["电视支架"], "sku": "ST-3200"},
            "action": "publish",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "published"
    assert data["sync_status"] in ("indexing", "synced")

    # 增量同步真实写入向量库：发布内容经分块嵌入后可按 sku 维度检索
    dense, sparse = next(embedder.embed_batch(["ST-3200 为壁挂电视支架"]).rows())
    hits = store.hybrid_search(dense=dense, sparse=sparse, expr="sku = 'ST-3200'", top_k=5)
    assert hits, "发布条目未通过 IncrementalSyncEngine 写入向量库"

    # 审计留痕（NFR-SEC-03 内存写入器）：create + publish 动作
    audit = app.state.admin_audit_hook
    assert isinstance(audit, InMemoryAuditTrail)
    assert audit.events, "写操作未留审计事件"
    first = audit.events[0]
    assert first["action_type"] == "kb_change"
    assert first["action"] == "create"
    assert first["state_action"] == "publish"

    # 列表可见该条目
    listing = client.get("/api/v1/knowledge/items", params={"kb_type": "product"})
    assert listing.json()["data"]["total"] == 1
    assert listing.json()["data"]["items"][0]["uid"] == data["uid"]


# ── 既有链路不回归 ───────────────────────────────────────────────────────────


def test_既有healthz与检索端点不受挂载影响() -> None:
    """挂载管理端点后 /healthz 与统一检索 API 照常可用（契约不回归）。"""
    from services.kb.api.routes import ChunkMeta, InMemoryChunkMetaProvider
    from services.kb.core.vectorstore import VectorRecord

    client, store, embedder, _app = _build_app_env()
    # 预灌一条检索种子（FakeEmbeddingClient 确定向量 + 回表元数据）
    query = "ST-3200 支架的最大承载"
    seed_id = 900001
    store.upsert(
        [
            VectorRecord(
                chunk_id=seed_id,
                kb_type="product",
                dense=embedder.dense_vector(query),
                sparse=embedder.sparse_vector(query),
                sku="ST-3200",
            )
        ]
    )
    client.app.state.chunk_meta = InMemoryChunkMetaProvider(
        {
            seed_id: ChunkMeta(
                kb_type="product",
                title="ST-3200 支架参数表",
                item_uid="k-seed0001",
                origin_file="种子/支架.pdf",
                version=1,
            )
        }
    )

    health = client.get("/healthz")
    assert health.status_code == 200 and health.json() == {"status": "ok"}

    search = client.post("/api/v1/knowledge/search", json={"query": query, "top_k": 5})
    assert search.status_code == 200
    body = search.json()
    assert body["code"] == 0
    assert any(item["chunk_id"] == seed_id for item in body["data"]["results"])


def test_显式关闭挂载_管理端点404() -> None:
    """mount_admin=False：检索 API 独立部署形态，管理端点不暴露。"""
    key_registry = KeyRegistry()
    issued = key_registry.issue("solo", quota=QuotaConfig(qps=10, daily=10))
    app = create_app(
        InMemoryVectorStore(),
        FakeEmbeddingClient(),
        None,
        key_registry,
        mount_admin=False,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {issued.api_key}"})
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/knowledge/items").status_code == 404
    assert client.get("/api/v1/admin/stats").status_code == 404
