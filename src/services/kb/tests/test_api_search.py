# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""统一检索 API 单元/契约测试（FR-KB-030 / SR-KB-030 §9，契约冻结 v1）。

真实链路组装（create_app + 内存实现，无任何 mock 桩在链路中途截断）：
    KeyRegistry（FR-KB-031 内存注册表）+ SlidingWindowRateLimiter
    + FakeEmbeddingClient（FR-KB-013 确定性假实现）+ InMemoryVectorStore
    （FR-KB-014 内存后端，灌数 VectorRecord）+ RetrievalPipeline（FR-KB-015
    三段式）+ InMemoryChunkMetaProvider（回表）。

覆盖（SR-KB-030 §9 单元测试表）：
- 契约测试：live OpenAPI 冻结字段断言（§6.5）+ 真实响应对 04-接口设计 §2
  示例的字段级 schema 校验（键集/类型逐项一致）；
- 正常检索：字段回填（title/source/kb_type 回表）、rewritten_query 改写
  上浮、latency_ms 在线测量、得分降序；
- 过滤组合：kb_types / filters(sku) / 三维组合（对齐 §2 请求示例）；
- top_k 边界：1/20 合法，0/21 越界 40001（data.errors 定位字段）；
- 校验失败 40001：query 空/超长、kb_types 非法枚举、filters 非白名单维度、
  契约外字段（extra=forbid）；
- 鉴权：未带 key / 错误 key → 40101 统一信封；限流 → 42901 + Retry-After；
- 下游 50002：全部召回路失败与检索链路超时（HTTP 503，降级语义文案）；
- 健康检查 /healthz、指标端点 /internal/metrics、trace_id 透传/生成、
  回表缺失命中丢弃（增量删除竞态兜底）。

注：仓库根 conftest.py 已处理顶层 platform 包名与标准库 platform 的冲突，
本文件不内联自愈；docs/openapi/openapi.yaml 快照比对属 CI 门禁（需 YAML
解析器），本文件以 live OpenAPI 断言承载同源口径。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from fastapi.testclient import TestClient
from foundation.apikey.calllog import ApiCallLogger
from foundation.apikey.keys import KeyRegistry, QuotaConfig
from foundation.apikey.ratelimit import SlidingWindowRateLimiter

from services.kb.api.routes import ChunkMeta, InMemoryChunkMetaProvider
from services.kb.app import create_app
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.retrieval import Hit
from services.kb.core.vectorstore import InMemoryVectorStore, VectorRecord

#: 被测端点（04-接口设计 §2）
PATH = "/api/v1/knowledge/search"
#: 主查询（与 04 §2 请求示例同源）
QUERY = "ST-3200 支架的最大承载和认证情况"

# ─────────────────────────── 测试链路组装 ───────────────────────────


def _seed_records(embedder: FakeEmbeddingClient) -> list[VectorRecord]:
    """灌数四条记录：稠密/稀疏向量取自 FakeEmbeddingClient 的确定性向量。"""

    def rec(chunk_id: int, kb_type: str, text: str, **dims: str) -> VectorRecord:
        return VectorRecord(
            chunk_id=chunk_id,
            kb_type=kb_type,
            dense=embedder.dense_vector(text),
            sparse=embedder.sparse_vector(text),
            **dims,
        )

    return [
        rec(10241, "product", QUERY, sku="ST-3200", scene_tag="询价"),
        rec(10242, "process", "ST-3200 报价流程与交期", sku="ST-3200", scene_tag="询价", process_node="报价"),
        rec(10243, "skill", "询价场景接待话术", scene_tag="询价"),
        rec(10244, "product", "摄影云台包 GP-100 产品参数", sku="GP-100"),
    ]


#: 回表元数据（10244 故意缺配：命中即整条丢弃的竞态兜底由专属用例覆盖）
META_ROWS: dict[int, ChunkMeta] = {
    10241: ChunkMeta(kb_type="product", title="ST-3200 支架参数表",
                     item_uid="k-3f2a0001", origin_file="产品目录/支架系列.pdf", version=3),
    10242: ChunkMeta(kb_type="process", title="报价流程 SOP",
                     item_uid="k-3f2a0002", origin_file="流程/报价SOP.docx", version=1),
    10243: ChunkMeta(kb_type="skill", title="询价接待话术",
                     item_uid="k-3f2a0003", origin_file="培训/话术库.xlsx", version=2),
}


@dataclass
class RrfOrderReranker:
    """确定性重排桩：保 RRF 融合序截断 TopK，写回 rerank_score（重排生效正例）。"""

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def rerank(self, query: str, cands: Sequence[Hit], top_k: int) -> list[Hit]:
        self.calls.append({"query": query, "n_cands": len(cands), "top_k": top_k})
        ordered = sorted(cands, key=lambda h: (-h.rrf_score, str(h.chunk_id)))
        return [replace(h, score=h.rrf_score, rerank_score=h.rrf_score) for h in ordered[:top_k]]


class FailingStore:
    """向量库故障桩：hybrid_search 恒抛错 → 全部召回路失败（50002 语义）。"""

    def __init__(self) -> None:
        self.calls = 0

    def hybrid_search(self, *, dense: Any = None, sparse: Any = None,
                      expr: Any = None, top_k: int = 5) -> list[Any]:
        self.calls += 1
        raise RuntimeError("Milvus 不可达（测试桩）")


class SlowStore:
    """慢查询桩：异步睡眠超过 API 硬截止 → 50002（超时口径）。"""

    async def hybrid_search(self, *, dense: Any = None, sparse: Any = None,
                            expr: Any = None, top_k: int = 5) -> list[Any]:
        await asyncio.sleep(0.3)
        return []


class ApiTestEnv:
    """真实链路测试环境：每个用例独立注册表/限流器/指标，互不串扰。"""

    def __init__(
        self,
        *,
        store: Any = None,
        qps: int = 1000,
        timeout_s: float = 0.5,
        meta_provider: InMemoryChunkMetaProvider | None = None,
    ) -> None:
        self.embedder = FakeEmbeddingClient()
        if store is None:
            self.store: Any = InMemoryVectorStore()
            self.store.upsert(_seed_records(self.embedder))
        else:
            self.store = store
        self.reranker = RrfOrderReranker()
        self.key_registry = KeyRegistry()
        self.issued = self.key_registry.issue(
            "agent-core", quota=QuotaConfig(qps=qps, daily=100_000)
        )
        self.call_logger = ApiCallLogger()
        self.app = create_app(
            self.store,
            self.embedder,
            self.reranker,
            self.key_registry,
            limiter=SlidingWindowRateLimiter(),
            call_logger=self.call_logger,
            meta_provider=meta_provider if meta_provider is not None else InMemoryChunkMetaProvider(META_ROWS),
            timeout_s=timeout_s,
        )
        self.client = TestClient(self.app)
        self.headers = {"Authorization": f"Bearer {self.issued.api_key}"}

    def search(self, payload: dict[str, Any], *, headers: dict[str, str] | None = None):
        """发起一次检索请求（默认带有效 key）。"""
        return self.client.post(PATH, json=payload, headers=headers or self.headers)


ENVELOPE_KEYS = {"code", "message", "data", "trace_id"}


# ─────────────────────────── 契约测试（SR-KB-030 §6.5）───────────────────────────

#: 04-接口设计 §2 响应示例（信封三键补自 §1.3）：字段级 schema 校验的权威样张
DESIGN_EXAMPLE_RESPONSE: dict[str, Any] = {
    "code": 0,
    "message": "ok",
    "trace_id": "5f0c-0000-0000-0000-0000-0000",
    "data": {
        "results": [
            {
                "chunk_id": 10241,
                "kb_type": "product",
                "title": "ST-3200 支架参数表",
                "snippet": "…最大承载 85kg，通过 CE / SGS 认证…",
                "source": {"item_uid": "k-3f2a…", "origin_file": "产品目录/支架系列.pdf", "version": 3},
                "score": 0.87,
            }
        ],
        "latency_ms": 312,
        "rewritten_query": "ST-3200 支架 最大承载 认证 CE SGS",
    },
}


def assert_same_shape(example: Any, actual: Any, path: str = "$") -> None:
    """字段级 schema 断言：键集完全一致 + 标量类型一致（数组逐元素递归）。

    对齐 04-接口设计 §2 的字段结构：多键/少键/类型漂移均在此失败（契约
    防漂移的提供方证据，SR-KB-030 §6.5）。
    """
    if isinstance(example, dict):
        assert isinstance(actual, dict), f"{path}: 期望对象，实际 {type(actual).__name__}"
        assert set(actual) == set(example), f"{path}: 键集漂移 {set(actual) ^ set(example)}"
        for key in example:
            assert_same_shape(example[key], actual[key], f"{path}.{key}")
    elif isinstance(example, list):
        assert isinstance(actual, list), f"{path}: 期望数组，实际 {type(actual).__name__}"
        if example:
            for index, element in enumerate(actual):
                assert_same_shape(example[0], element, f"{path}[{index}]")
    elif isinstance(example, bool):
        assert isinstance(actual, bool), f"{path}: 期望布尔"
    elif isinstance(example, int):
        assert isinstance(actual, int) and not isinstance(actual, bool), f"{path}: 期望整数"
    elif isinstance(example, float):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool), f"{path}: 期望数值"
    elif isinstance(example, str):
        assert isinstance(actual, str), f"{path}: 期望字符串"


class TestContractFreeze:
    """契约冻结 v1：live OpenAPI 模型断言 + 真实响应字段级 schema 校验。"""

    def test_openapi_models_frozen_fields(self) -> None:
        """live OpenAPI 对冻结字段的模型级断言（docs/openapi/openapi.yaml 同源）。"""
        schemas = ApiTestEnv().app.openapi()["components"]["schemas"]
        # 请求模型：五字段，仅 query 必填，缺省值冻结
        req = schemas["SearchRequest"]
        assert req["required"] == ["query"]
        props = req["properties"]
        assert set(props) == {"query", "kb_types", "filters", "top_k", "rerank"}
        assert props["query"]["type"] == "string"
        assert props["query"]["minLength"] == 1
        assert props["query"]["maxLength"] == 512
        assert props["top_k"]["type"] == "integer"
        assert props["top_k"]["default"] == 5
        assert props["top_k"]["minimum"] == 1
        assert props["top_k"]["maximum"] == 20
        assert props["rerank"]["type"] == "boolean"
        assert props["rerank"]["default"] is True
        # 可选字段：anyOf 数组/对象 + null（缺省即 null，查全部六大库 / 不过滤）
        assert {choice.get("type") for choice in props["kb_types"]["anyOf"]} == {"array", "null"}
        assert {choice.get("type") for choice in props["filters"]["anyOf"]} == {"object", "null"}
        assert props["kb_types"].get("default") is None
        assert props["filters"].get("default") is None
        # 信封：四键；data 与 trace_id 必填（code/message 有缺省）
        resp_schema = schemas["SearchResponse"]
        assert set(resp_schema["properties"]) == ENVELOPE_KEYS
        assert set(resp_schema["required"]) == {"data", "trace_id"}
        assert resp_schema["properties"]["code"]["default"] == 0
        assert resp_schema["properties"]["message"]["default"] == "ok"
        # data 段：三字段全必填（冻结）
        data_schema = schemas["SearchData"]
        assert set(data_schema["properties"]) == {"results", "latency_ms", "rewritten_query"}
        assert set(data_schema["required"]) == {"results", "latency_ms", "rewritten_query"}
        # 单条结果：六字段全必填
        item = schemas["SearchResultItem"]
        assert set(item["properties"]) == {"chunk_id", "kb_type", "title", "snippet", "source", "score"}
        assert set(item["required"]) == {"chunk_id", "kb_type", "title", "snippet", "source", "score"}
        # source：三字段全必填
        src = schemas["SourceRef"]
        assert set(src["properties"]) == {"item_uid", "origin_file", "version"}
        assert set(src["required"]) == {"item_uid", "origin_file", "version"}

    def test_response_matches_design_example_shape(self) -> None:
        """真实响应对 04 §2 示例的字段级 schema 校验（消费方防漂移同源口径）。"""
        env = ApiTestEnv()
        resp = env.search({"query": QUERY, "top_k": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["data"]["results"], "契约校验样张要求结果非空（稀疏路保证命中）"
        assert_same_shape(DESIGN_EXAMPLE_RESPONSE, body)

    def test_error_envelopes_share_shape(self) -> None:
        """错误路径同样走统一信封：四键齐全（40101/40001 两类代表）。"""
        env = ApiTestEnv()
        no_key = env.client.post(PATH, json={"query": QUERY})
        assert no_key.status_code == 401
        assert set(no_key.json()) == ENVELOPE_KEYS
        bad_body = env.search({"query": QUERY, "top_k": 0})
        assert bad_body.status_code == 400
        assert set(bad_body.json()) == ENVELOPE_KEYS
        assert isinstance(bad_body.json()["data"]["errors"], list)


# ─────────────────────────── 正常检索与过滤 ───────────────────────────


class TestSearchBehavior:
    """正常检索链路：字段回填 / 改写上浮 / 时延测量 / 过滤组合 / top_k 边界。"""

    def test_search_ok_fields_and_rewrite(self) -> None:
        env = ApiTestEnv()
        resp = env.search({"query": QUERY, "top_k": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0 and body["message"] == "ok"
        uuid.UUID(body["trace_id"])  # 未带 X-Trace-Id → 新生成 uuid（04 §1.4）
        data = body["data"]
        ids = [item["chunk_id"] for item in data["results"]]
        # 稀疏路（共享词元 st/3200/中文长词）保证两块 ST-3200 相关命中
        assert 10241 in ids and 10242 in ids
        item = next(r for r in data["results"] if r["chunk_id"] == 10241)
        assert item["kb_type"] == "product"  # 回表权威源
        assert item["title"] == "ST-3200 支架参数表"
        assert item["source"] == {"item_uid": "k-3f2a0001",
                                  "origin_file": "产品目录/支架系列.pdf", "version": 3}
        assert isinstance(item["snippet"], str)
        assert isinstance(item["score"], float) and item["score"] > 0
        assert isinstance(data["latency_ms"], int) and data["latency_ms"] >= 0
        # 改写上浮：意图对齐（认证 → certification）标准词追加，原词保留
        assert data["rewritten_query"].startswith(QUERY)
        assert "certification" in data["rewritten_query"]
        scores = [r["score"] for r in data["results"]]
        assert scores == sorted(scores, reverse=True)  # 得分降序
        assert env.reranker.calls and env.reranker.calls[0]["top_k"] == 5  # 三段式精排已发起

    def test_filter_kb_types(self) -> None:
        env = ApiTestEnv()
        resp = env.search({"query": QUERY, "kb_types": ["product"], "top_k": 5})
        assert resp.status_code == 200
        ids = [r["chunk_id"] for r in resp.json()["data"]["results"]]
        assert set(ids) <= {10241, 10244}  # 仅 product 分区
        assert 10241 in ids

    def test_filter_sku(self) -> None:
        env = ApiTestEnv()
        resp = env.search({"query": QUERY, "filters": {"sku": ["ST-3200"]}, "top_k": 5})
        assert resp.status_code == 200
        ids = [r["chunk_id"] for r in resp.json()["data"]["results"]]
        assert set(ids) <= {10241, 10242}  # SKU 维度过滤
        assert 10241 in ids

    def test_filter_combined_like_design_example(self) -> None:
        """kb_types + filters 三维组合（对齐 04 §2 请求示例的完整入参形态）。"""
        env = ApiTestEnv()
        resp = env.search({
            "query": QUERY,
            "kb_types": ["product", "process"],
            "filters": {"sku": ["ST-3200"], "scene_tag": ["询价"], "process_node": ["报价"]},
            "top_k": 5,
            "rerank": True,
        })
        assert resp.status_code == 200
        ids = [r["chunk_id"] for r in resp.json()["data"]["results"]]
        assert ids == [10242]  # 三维 AND 组合仅 10242 满足

    def test_rerank_false_returns_rrf_order(self) -> None:
        env = ApiTestEnv()
        resp = env.search({"query": QUERY, "top_k": 5, "rerank": False})
        assert resp.status_code == 200
        assert env.reranker.calls == []  # 第③段未发起
        assert resp.json()["data"]["results"]  # RRF 序直出仍有结果

    def test_topk_boundaries_valid(self) -> None:
        env = ApiTestEnv()
        assert len(env.search({"query": QUERY, "top_k": 1}).json()["data"]["results"]) == 1
        resp = env.search({"query": QUERY, "top_k": 20})
        assert resp.status_code == 200
        assert len(resp.json()["data"]["results"]) <= 4  # 灌数总量即上限

    def test_topk_out_of_range_40001(self) -> None:
        env = ApiTestEnv()
        for bad_topk in (0, 21):
            resp = env.search({"query": QUERY, "top_k": bad_topk})
            assert resp.status_code == 400
            body = resp.json()
            assert body["code"] == 40001
            assert any("top_k" in error["loc"] for error in body["data"]["errors"]), (
                f"top_k={bad_topk} 的 40001 须定位到字段"
            )


class TestValidation40001:
    """参数校验失败 40001（SR-KB-030 §9 校验-入参边界/枚举与白名单）。"""

    def test_empty_query(self) -> None:
        resp = ApiTestEnv().search({"query": ""})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40001 and body["trace_id"]
        assert any("query" in error["loc"] for error in body["data"]["errors"])

    def test_overlong_query(self) -> None:
        resp = ApiTestEnv().search({"query": "撑" * 513})
        assert resp.status_code == 400
        assert resp.json()["code"] == 40001

    def test_unknown_kb_type_lists_legal_values(self) -> None:
        resp = ApiTestEnv().search({"query": "ok", "kb_types": ["unknown"]})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40001
        assert "合法值" in str(body["data"]["errors"])  # 提示合法枚举清单

    def test_filter_dimension_not_whitelisted(self) -> None:
        resp = ApiTestEnv().search({"query": "ok", "filters": {"secret_dim": ["x"]}})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40001
        assert "白名单" in str(body["data"]["errors"])

    def test_unknown_field_rejected(self) -> None:
        """契约外字段 fail-closed（extra=forbid）→ 40001。"""
        resp = ApiTestEnv().search({"query": "ok", "unknown_field": 1})
        assert resp.status_code == 400
        assert resp.json()["code"] == 40001


class TestAuthAndQuota:
    """鉴权与限流（FR-KB-031 通道复用）：40101 / 42901 + Retry-After。"""

    def test_missing_key_40101(self) -> None:
        resp = ApiTestEnv().client.post(PATH, json={"query": QUERY})
        assert resp.status_code == 401
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["code"] == 40101 and body["data"] is None and body["trace_id"]

    def test_bad_key_40101(self) -> None:
        env = ApiTestEnv()
        resp = env.client.post(PATH, json={"query": QUERY},
                               headers={"Authorization": "Bearer kbk_" + "x" * 40})
        assert resp.status_code == 401
        assert resp.json()["code"] == 40101

    def test_rate_limited_42901_with_retry_after(self) -> None:
        env = ApiTestEnv(qps=1)
        assert env.search({"query": QUERY}).status_code == 200  # 窗口内首个请求
        second = env.search({"query": QUERY})
        assert second.status_code == 429
        body = second.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["code"] == 42901 and body["data"] is None
        assert second.headers.get("retry-after")  # SR-KB-031 §6.3 增补头


class TestDownstream50002:
    """下游依赖故障/超时 → 50002（HTTP 503，降级语义文案）。"""

    def test_all_routes_failed_50002(self) -> None:
        store = FailingStore()
        env = ApiTestEnv(store=store)
        resp = env.search({"query": QUERY})
        assert resp.status_code == 503
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS
        assert body["code"] == 50002
        # CR i18n 接线：错误信封附用户行动建议（技术 message 不变；原 data=None → hints 增强）
        assert body["data"]["hints"]["title"]
        assert "降级" in body["message"]
        assert store.calls >= 2  # dense/sparse 两路均已发起后全部失败

    def test_pipeline_timeout_50002(self) -> None:
        env = ApiTestEnv(store=SlowStore(), timeout_s=0.05)
        resp = env.search({"query": QUERY})
        assert resp.status_code == 503
        body = resp.json()
        assert body["code"] == 50002
        assert "超时" in body["message"] and "降级" in body["message"]


class TestObservability:
    """健康检查 / 指标端点 / trace 透传 / 回表缺失兜底。"""

    def test_healthz(self) -> None:
        env = ApiTestEnv()
        resp = env.client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_internal_metrics_mounted(self) -> None:
        env = ApiTestEnv()
        assert env.search({"query": QUERY}).status_code == 200
        resp = env.client.get("/internal/metrics")
        assert resp.status_code == 200
        payload = resp.json()
        assert {"counters", "histograms", "traces", "alerts"} <= set(payload)
        # API 层计时中间件已观测该请求（NFR-PER-02 P95 口径）
        assert payload["histograms"]["kb_search_request_ms"]["count"] >= 1

    def test_trace_id_passthrough(self) -> None:
        env = ApiTestEnv()
        resp = env.search({"query": QUERY},
                          headers={**env.headers, "X-Trace-Id": "trace-e2e-0001"})
        assert resp.status_code == 200
        assert resp.json()["trace_id"] == "trace-e2e-0001"
        assert resp.headers["x-trace-id"] == "trace-e2e-0001"  # 中间件回显同源

    def test_recalled_without_meta_dropped(self) -> None:
        """增量删除竞态兜底：召回有命中、回表全缺 → 整条丢弃，链路仍 200。"""
        env = ApiTestEnv(meta_provider=InMemoryChunkMetaProvider())  # 空回表
        resp = env.search({"query": "GP-100 云台包参数", "top_k": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0 and body["data"]["results"] == []
        assert env.reranker.calls and env.reranker.calls[0]["n_cands"] >= 1  # 确有召回后被丢弃

    def test_call_log_written_for_success_and_rejection(self) -> None:
        """调用留痕（FR-KB-031 §6.4）：成功与被拒（40101）各落一行。"""
        env = ApiTestEnv()
        assert env.search({"query": QUERY}).status_code == 200
        env.client.post(PATH, json={"query": QUERY})  # 未带 key → 401
        entries = env.call_logger.entries()
        assert len(entries) == 2
        assert [e.code for e in entries] == [0, 40101]
        assert all(e.method == "POST" and e.path == PATH for e in entries)
