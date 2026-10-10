# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""向量化客户端与出域防护单元测试（FR-KB-013）。

覆盖（对应实现方案 §9 测试要点）：
- FakeEmbeddingClient 确定性：跨实例同文本同向量、批内与单条按位一致、
  词频稀疏映射、1024 维单位向量；
- 出域防护：公网域名 / 公网 IP / https 拒绝（fail-closed），内网主机名 /
  私网 IP / 回环放行；TeiEmbeddingClient 构造即拒绝公网端点；
- TeiEmbeddingClient：batch=32 分片对齐（100 条 -> 32/32/32/4）、
  超时指数退避重试（httpx MockTransport 模拟）、重试耗尽抛 EmbeddingError
  （上层降级 BM25 信号）、维度断言（768 维拒绝）、稀疏字段解析、空入参。

实现方案：docs/07-实现方案/KB/SR-KB-013-向量化服务.md §6.1、§6.2、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import sysconfig
from types import ModuleType
from typing import Any


def _bootstrap_stdlib_platform() -> None:
    """恢复标准库 platform 语义（幂等，必须在 import httpx 之前执行）。

    仓库顶层包目录名为 ``platform``（开发规范 §2 代码组织），从仓库根运行
    pytest 时它先于标准库被解析——httpx→rich→attrs 链路上的
    ``platform.python_implementation()`` 会抛 AttributeError。与
    platform/tests/conftest.py 同法：把标准库 platform 源码装载后回填到
    已缓存对象上，本地包的子模块导入不受影响（本文件无法落 conftest，
    故在模块内自举；services/kb/tests 无共享 conftest 注册点）。
    """
    cached: ModuleType | None = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:  # 标准库定位失败属环境损坏，应当显式失败
        raise RuntimeError("无法定位标准库 platform 模块（测试环境损坏）")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


_bootstrap_stdlib_platform()

import httpx  # noqa: E402 —— 必须在标准库 platform 自举之后导入
import pytest  # noqa: E402

from services.kb.core.pipeline.embedding.base import (  # noqa: E402
    DENSE_DIM,
    EmbeddingClient,
    EmbeddingError,
    EmbeddingResult,
)
from services.kb.core.pipeline.embedding.client import (
    FakeEmbeddingClient,
    TeiEmbeddingClient,
)
from services.kb.core.pipeline.embedding.guard import (
    EmbeddingEgressError,
    assert_allowed_embedding_endpoint,
    is_allowed_embedding_endpoint,
)

TEI_BASE = "http://tei-embedding:8080"


def _no_sleep(_seconds: float) -> None:
    """测试用假睡眠：重试退避不产生真实等待。"""


# ─────────────────────────── FakeEmbeddingClient 确定性 ───────────────────────────

TEXTS = [
    "不锈钢保温杯 500ml 出口欧洲",
    "MOQ 1000pcs, FOB Ningbo",
    "产品说明书：电源 220V，功率 800W",
    "hello world",
    "",
]


class TestFakeEmbeddingClient:
    """确定性假实现：全仓测试的向量基准。"""

    def test_deterministic_across_instances(self) -> None:
        """跨实例、跨调用同文本产出完全一致的稠密与稀疏向量。"""
        r1 = FakeEmbeddingClient().embed_batch(TEXTS)
        r2 = FakeEmbeddingClient().embed_batch(TEXTS)
        assert r1.dense == r2.dense
        assert r1.sparse == r2.sparse

    def test_batch_single_alignment(self) -> None:
        """批内第 i 条与单独 embed 第 i 条按位一致（等长对齐契约）。"""
        fake = FakeEmbeddingClient()
        singles = [fake.embed_batch([t]).dense[0] for t in TEXTS]
        single_sparse = [fake.embed_batch([t]).sparse[0] for t in TEXTS]
        batch = fake.embed_batch(TEXTS)
        assert len(batch) == len(TEXTS)
        for i, text in enumerate(TEXTS):
            assert batch.dense[i] == singles[i], f"第 {i} 条稠密向量错位：{text!r}"
            assert batch.sparse[i] == single_sparse[i], f"第 {i} 条稀疏向量错位：{text!r}"

    def test_dense_dim_and_unit_norm(self) -> None:
        """稠密向量 1024 维且 L2 范数为 1（单位化）。"""
        result = FakeEmbeddingClient().embed_batch(["维度与归一化校验"])
        assert len(result.dense[0]) == DENSE_DIM
        norm = math.sqrt(sum(v * v for v in result.dense[0]))
        assert math.isclose(norm, 1.0, abs_tol=1e-9)

    def test_different_texts_differ(self) -> None:
        """不同文本产出不同稠密向量（哈希种子区分）。"""
        fake = FakeEmbeddingClient()
        assert fake.embed_batch(["alpha"]).dense[0] != fake.embed_batch(["beta"]).dense[0]

    def test_sparse_word_frequency(self) -> None:
        """稀疏向量为词频映射：重复词计数翻倍，小写化合并同词。"""
        fake = FakeEmbeddingClient()
        sparse = fake.sparse_vector("Hello world hello WORLD")
        assert sparse[FakeEmbeddingClient.token_id("hello")] == 2.0
        assert sparse[FakeEmbeddingClient.token_id("world")] == 2.0
        assert len(sparse) == 2

    def test_calls_recorded(self) -> None:
        """入参快照记录：供编排层断言调用次数与分批内容。"""
        fake = FakeEmbeddingClient()
        fake.embed_batch(TEXTS)
        fake.embed_batch([])
        assert fake.calls == [TEXTS, []]
        assert fake.call_count == 2

    def test_empty_input(self) -> None:
        """空列表：空结果、零异常。"""
        result = FakeEmbeddingClient().embed_batch([])
        assert len(result) == 0
        assert result.dense == [] and result.sparse == []

    def test_protocol_conformance(self) -> None:
        """两个实现均满足 EmbeddingClient 协议（结构化类型检查）。"""
        assert isinstance(FakeEmbeddingClient(), EmbeddingClient)
        client = TeiEmbeddingClient(
            TEI_BASE,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
            sleep=_no_sleep,
        )
        try:
            assert isinstance(client, EmbeddingClient)
        finally:
            client.close()

    def test_invalid_input_type(self) -> None:
        """非字符串入参：TypeError。"""
        with pytest.raises(TypeError):
            FakeEmbeddingClient().embed_batch([123])  # type: ignore[list-item]


# ─────────────────────────── 出域防护（NFR-SEC-06）───────────────────────────

PUBLIC_ENDPOINTS = [
    "https://api.openai.com/v1/embeddings",  # https 一律视为外部
    "http://8.8.8.8:8080",                   # 公网 IP 字面量
    "http://203.0.113.10:8080/embed_all",    # 公网 IP（TEST-NET-3）
    "http://100.20.30.40",                   # 公网 IP（非私网段）
]

INTERNAL_ENDPOINTS = [
    "http://tei-embedding:8080",        # 白名单主机名（容器服务名）
    "http://10.0.1.5:8080",             # 私网 10/8
    "http://172.16.0.9/embed_all",      # 私网 172.16/12
    "http://192.168.1.10:8080",         # 私网 192.168/16
    "http://127.0.0.1:9912",            # 回环
    "http://localhost:8080",            # 回环主机名
]


class TestEgressGuard:
    """代码层出域断言：仅内网/回环合法。"""

    @pytest.mark.parametrize("endpoint", PUBLIC_ENDPOINTS)
    def test_reject_public_endpoints(self, endpoint: str) -> None:
        """公网端点（含 https、公网 IP）：判定不通过 + 断言抛 EmbeddingEgressError。"""
        assert is_allowed_embedding_endpoint(endpoint) is False
        with pytest.raises(EmbeddingEgressError):
            assert_allowed_embedding_endpoint(endpoint)

    @pytest.mark.parametrize("endpoint", INTERNAL_ENDPOINTS)
    def test_allow_internal_endpoints(self, endpoint: str) -> None:
        """内网端点：判定通过（白名单主机 / 私网 IP / 回环，无需 DNS）。"""
        assert is_allowed_embedding_endpoint(endpoint) is True
        assert_allowed_embedding_endpoint(endpoint)  # 不抛异常即通过

    def test_domain_resolves_to_private_ip_allowed(self) -> None:
        """域名解析为私网 IP：放行（注入假解析器，避免真实 DNS）。"""
        ok = is_allowed_embedding_endpoint(
            "http://tei.internal.corp:8080", resolver=lambda host: ["10.1.2.3"]
        )
        assert ok is True

    def test_domain_resolves_mixed_ips_rejected(self) -> None:
        """域名解析含任一公网 IP：拒绝（全部 IP 均须私网）。"""
        ok = is_allowed_embedding_endpoint(
            "http://tei.internal.corp:8080", resolver=lambda host: ["10.1.2.3", "8.8.8.8"]
        )
        assert ok is False

    def test_unresolvable_domain_rejected(self) -> None:
        """域名解析失败：fail-closed 拒绝（防公网 IP 直写域名绕过）。"""
        ok = is_allowed_embedding_endpoint(
            "http://nonexistent.internal.corp:8080", resolver=lambda host: []
        )
        assert ok is False

    def test_custom_allowed_hosts(self) -> None:
        """自定义主机白名单：命中放行。"""
        ok = is_allowed_embedding_endpoint(
            "http://my-embed.svc:9999", allowed_hosts={"my-embed.svc"}
        )
        assert ok is True

    @pytest.mark.parametrize(
        "bad", ["", "tei-embedding:8080", "ftp://10.0.0.5:8080", "http://"]
    )
    def test_malformed_endpoints_rejected(self, bad: str) -> None:
        """畸形端点（无 scheme / 非 http / 无主机）：一律拒绝。"""
        assert is_allowed_embedding_endpoint(bad) is False

    @pytest.mark.parametrize("endpoint", PUBLIC_ENDPOINTS)
    def test_client_construction_rejects_public_url(self, endpoint: str) -> None:
        """配置外部公网端点：客户端构造即失败（拒绝启动，fail-fast）。"""
        with pytest.raises(EmbeddingEgressError):
            TeiEmbeddingClient(endpoint, sleep=_no_sleep)

    def test_client_construction_rejects_public_domain_via_resolver(self) -> None:
        """域名解析为公网 IP：客户端构造即失败。"""
        with pytest.raises(EmbeddingEgressError):
            TeiEmbeddingClient(
                "http://public.example.com", resolver=lambda host: ["93.184.216.34"]
            )


# ─────────────────────────── TeiEmbeddingClient ───────────────────────────

def _tei_payload(n: int, dim: int, base: int) -> dict[str, Any]:
    """构造 TEI /embed_all 响应：第 base+i 条稠密向量元素恒为 base+i+1（供对齐断言）。"""
    return {
        "embeddings": [[float(base + i + 1)] * dim for i in range(n)],
        "sparse_embeddings": [
            {"indices": [base + i, base + i + 1000], "values": [0.5, 0.25]} for i in range(n)
        ],
    }


def _make_client(handler: Any, **kwargs: Any) -> TeiEmbeddingClient:
    """构造测试客户端：MockTransport + 假睡眠（退避零等待，可覆盖记录）。"""
    kwargs.setdefault("sleep", _no_sleep)
    return TeiEmbeddingClient(TEI_BASE, transport=httpx.MockTransport(handler), **kwargs)


class TestTeiClient:
    """TEI 真实客户端：分批 / 重试 / 降级信号 / 维度断言。"""

    def test_batch_sharding_100_texts(self) -> None:
        """100 条文本按 batch=32 分片：4 次调用（32/32/32/4），结果同序对齐。"""
        calls: list[int] = []
        counter = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(len(body["inputs"]))
            n = len(body["inputs"])
            resp = _tei_payload(n, DENSE_DIM, counter["n"])
            counter["n"] += n
            return httpx.Response(200, json=resp)

        client = _make_client(handler)
        try:
            result = client.embed_batch([f"知识块 {i}" for i in range(100)])
        finally:
            client.close()
        assert calls == [32, 32, 32, 4]
        assert len(result) == 100
        assert result.dense[0][0] == 1.0     # 第 1 条
        assert result.dense[42][0] == 43.0   # 第 43 条（跨分片仍同序）

    def test_sparse_parsed_to_int_float_mapping(self) -> None:
        """稀疏字段解析：indices/values -> {int 列号: float 权重}。"""
        client = _make_client(lambda req: httpx.Response(200, json=_tei_payload(1, DENSE_DIM, 7)))
        try:
            result = client.embed_batch(["单条"])
        finally:
            client.close()
        sparse = result.sparse[0]
        assert sparse == {7: 0.5, 1007: 0.25}
        assert all(isinstance(k, int) for k in sparse)
        assert all(isinstance(v, float) for v in sparse.values())

    def test_timeout_retry_then_success(self) -> None:
        """前两次超时、第三次成功：重试 2 次后成功，退避呈指数（0.2/0.4）。"""
        attempts = {"n": 0}
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise httpx.ConnectTimeout(f"模拟连接超时（第 {attempts['n']} 次）")
            return httpx.Response(200, json=_tei_payload(1, DENSE_DIM, 0))

        client = _make_client(handler, sleep=sleeps.append)
        try:
            result = client.embed_batch(["重试后成功"])
        finally:
            client.close()
        assert attempts["n"] == 3
        assert sleeps == [0.2, 0.4]  # backoff_base=0.2，指数退避 0.2*2^0 / 0.2*2^1
        assert len(result) == 1
        assert client.consecutive_failures == 0  # 成功清零

    def test_timeout_retries_exhausted_raises(self) -> None:
        """持续超时：1 次原生 + 2 次重试全部失败 -> EmbeddingError（降级 BM25 信号）。"""
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            raise httpx.ReadTimeout("模拟读超时")

        client = _make_client(handler)
        with pytest.raises(EmbeddingError) as excinfo:
            client.embed_batch(["持续超时"])
        client.close()
        assert attempts["n"] == 3                      # 1 + retries=2
        assert "TEI" in str(excinfo.value)             # 中文上下文
        assert "降级" in excinfo.value.message
        assert excinfo.value.endpoint == TEI_BASE      # 端点留痕
        assert client.consecutive_failures == 1        # 连续失败计数

    def test_http_5xx_retried_then_degrades(self) -> None:
        """服务端 5xx：同样走重试，耗尽抛 EmbeddingError。"""
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(502, text="Bad Gateway")

        client = _make_client(handler)
        with pytest.raises(EmbeddingError):
            client.embed_batch(["服务端错误"])
        client.close()
        assert attempts["n"] == 3

    def test_dimension_assertion(self) -> None:
        """响应为 768 维：维度断言失败抛 EmbeddingError（防权重被误换）。"""
        client = _make_client(lambda req: httpx.Response(200, json=_tei_payload(1, 768, 0)))
        with pytest.raises(EmbeddingError) as excinfo:
            client.embed_batch(["维度校验"])
        client.close()
        assert "768" in excinfo.value.message
        assert "1024" in excinfo.value.message

    def test_missing_fields_rejected(self) -> None:
        """响应缺少 sparse_embeddings 字段：EmbeddingError。"""
        client = _make_client(
            lambda req: httpx.Response(200, json={"embeddings": [[0.1] * DENSE_DIM]})
        )
        with pytest.raises(EmbeddingError, match="sparse_embeddings"):
            client.embed_batch(["缺字段"])
        client.close()

    def test_count_mismatch_rejected(self) -> None:
        """响应条数与请求不一致：EmbeddingError。"""
        client = _make_client(lambda req: httpx.Response(200, json=_tei_payload(3, DENSE_DIM, 0)))
        with pytest.raises(EmbeddingError, match="不一致"):
            client.embed_batch(["a", "b"])
        client.close()

    def test_empty_input_zero_http_calls(self) -> None:
        """空列表：空结果且不发起任何 HTTP 请求。"""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=_tei_payload(0, DENSE_DIM, 0))

        client = _make_client(handler)
        result = client.embed_batch([])
        client.close()
        assert len(result) == 0
        assert calls == []

    def test_request_payload_format(self) -> None:
        """请求体格式：POST /embed_all，inputs=分片文本，truncate=True。"""
        seen: list[tuple[str, dict[str, Any]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json=_tei_payload(1, DENSE_DIM, 0))

        client = _make_client(handler)
        client.embed_batch(["payload 校验"])
        client.close()
        assert len(seen) == 1
        path, body = seen[0]
        assert path == "/embed_all"
        assert body == {"inputs": ["payload 校验"], "truncate": True}


# ─────────────────────────── EmbeddingResult 对齐校验 ────────────────────────

class TestEmbeddingResult:
    """结果模型：稠密/稀疏等长对齐是硬约束。"""

    def test_misaligned_rejected(self) -> None:
        """稠密 1 条、稀疏 2 条：构造即拒绝。"""
        with pytest.raises(ValueError):
            EmbeddingResult(dense=[[0.1, 0.2]], sparse=[{}, {}])

    def test_rows_iterates_pairs(self) -> None:
        """rows() 按位迭代 (dense, sparse) 对。"""
        result = EmbeddingResult(dense=[[1.0], [2.0]], sparse=[{1: 0.5}, {2: 0.25}])
        pairs = list(result.rows())
        assert pairs == [([1.0], {1: 0.5}), ([2.0], {2: 0.25})]
        assert len(result) == 2
