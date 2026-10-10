# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""Embedding 客户端实现（FR-KB-013）：内网 TEI 真实客户端 + 确定性假实现。

- TeiEmbeddingClient：经 httpx 同步调用内网 TEI 容器 /embed_all（稠密 + 稀疏
  一次取回，SR-KB-013 §5 细化标注）。构造即做出域断言，配置外部公网端点直接
  拒绝启动；batch=32 分片、2s 超时、×2 指数退避重试，重试耗尽抛
  EmbeddingError 供上层降级 BM25（02-技术选型论证 §5）。
- FakeEmbeddingClient：确定性假实现——dense 为以文本 hash 为种子的 1024 维
  单位伪向量，sparse 为词频映射 {词ID: 词频}；不发起任何网络请求，供全仓
  单元测试使用（对齐 EmbeddingClient 协议）。

实现方案：docs/07-实现方案/KB/SR-KB-013-向量化服务.md §6.1、§6.2、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import hashlib
import math
import random
import re
import time
from collections.abc import Callable, Iterable

import httpx

from .base import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    DENSE_DIM,
    EmbeddingError,
    EmbeddingResult,
)
from .guard import assert_allowed_embedding_endpoint

#: 假实现稀疏词元切分：unicode \w（覆盖中英文与数字），小写化后统计词频
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class TeiEmbeddingClient:
    """内网 TEI 向量化客户端（唯一真实实现，数据不出域的执行端）。

    构造参数均可配置（对应 SR-KB-013 §7 app_config，端点可配）：
        base_url:          TEI 服务入口（必须通过出域断言，公网地址拒绝启动）；
        batch_size:        客户端分片批，默认 32；
        timeout_seconds:   单请求超时，默认 2.0s；
        retries:           失败重试次数，默认 2（1 次原生 + 2 次重试）；
        dense_dim:         稠密维度断言值，默认 1024（防权重误换，COM-IP-03）；
        transport / sleep / resolver：测试注入位（MockTransport / 假睡眠 / 假解析）。

    连续失败语义：某分片重试耗尽后 consecutive_failures +1 并抛
    EmbeddingError（上层批量侧留队列、查询侧降级 BM25）；任一分片成功即清零。
    """

    def __init__(
        self,
        base_url: str,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        dense_dim: int = DENSE_DIM,
        endpoint_path: str = "/embed_all",
        allowed_hosts: Iterable[str] | None = None,
        allowed_cidrs: Iterable[str] | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        backoff_base_seconds: float = 0.2,
    ) -> None:
        # 出域防护（NFR-SEC-06）：配置外部公网端点即拒绝启动，fail-fast
        assert_allowed_embedding_endpoint(
            base_url, allowed_hosts=allowed_hosts, allowed_cidrs=allowed_cidrs, resolver=resolver
        )
        if batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，当前 {batch_size}")
        if retries < 0:
            raise ValueError(f"retries 必须 >= 0，当前 {retries}")
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds 必须为正数，当前 {timeout_seconds}")

        self.base_url = base_url.rstrip("/")
        self.consecutive_failures = 0  # 连续失败分片数（重试耗尽计 1，成功清零）
        self._batch_size = batch_size
        self._retries = retries
        self._dense_dim = dense_dim
        self._endpoint_path = endpoint_path
        self._sleep = sleep
        self._backoff_base = backoff_base_seconds
        self._http = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    # ── 对外契约 ────────────────────────────────────────────────────────────

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        """批量向量化：按 batch_size 分片逐片调用 /embed_all，结果与入参同序对齐。"""
        shard_texts = self._validate_texts(texts)
        dense_all: list[list[float]] = []
        sparse_all: list[dict[int, float]] = []
        for start in range(0, len(shard_texts), self._batch_size):
            shard = shard_texts[start : start + self._batch_size]
            data = self._post_with_retry({"inputs": shard, "truncate": True})
            dense_rows, sparse_rows = self._parse_response(data, expected=len(shard))
            dense_all.extend(dense_rows)
            sparse_all.extend(sparse_rows)
        return EmbeddingResult(dense=dense_all, sparse=sparse_all)

    def close(self) -> None:
        """释放底层 HTTP 连接（长生命周期进程退出前调用）。"""
        self._http.close()

    def __enter__(self) -> "TeiEmbeddingClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── 内部实现 ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_texts(texts: list[str]) -> list[str]:
        """入参校验：必须为 list[str]（空列表合法，返回空结果、零网络调用）。"""
        if not isinstance(texts, list):
            raise TypeError(f"texts 必须为 list[str]，当前 {type(texts).__name__}")
        for i, item in enumerate(texts):
            if not isinstance(item, str):
                raise TypeError(f"texts[{i}] 必须为字符串，当前 {type(item).__name__}")
        return list(texts)

    def _post_with_retry(self, payload: dict[str, object]) -> dict[str, object]:
        """POST /embed_all：超时 / 传输 / 5xx 错误按指数退避重试，耗尽抛 EmbeddingError。"""
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = self._http.post(self._endpoint_path, json=payload)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError as exc:  # json.JSONDecodeError：响应体非合法 JSON
                    raise EmbeddingError(
                        f"TEI 响应非合法 JSON：{exc}", endpoint=self.base_url
                    ) from exc
                self.consecutive_failures = 0  # 成功即清零连续失败计数
                return data  # type: ignore[no-any-return]
            except EmbeddingError:
                raise  # 已是统一异常（如 JSON 解析失败），不重试直接上抛
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt < self._retries:
                    self._sleep(self._backoff_base * (2 ** attempt))  # 指数退避
        self.consecutive_failures += 1
        raise EmbeddingError(
            f"TEI 向量化服务连续失败（超时/不可用，已重试 {self._retries} 次）：{last_exc}；"
            f"批量任务将滞留队列，查询侧可降级 BM25 检索",
            endpoint=self.base_url,
        ) from last_exc

    def _parse_response(
        self, data: object, *, expected: int
    ) -> tuple[list[list[float]], list[dict[int, float]]]:
        """解析 /embed_all 响应：维度断言 + 稀疏字段展开，异常一律转 EmbeddingError。"""
        if not isinstance(data, dict) or "embeddings" not in data or "sparse_embeddings" not in data:
            raise EmbeddingError(
                f"TEI 响应缺少 embeddings/sparse_embeddings 字段：{type(data).__name__}",
                endpoint=self.base_url,
            )
        embeddings = data["embeddings"]
        sparse_raw = data["sparse_embeddings"]
        if not isinstance(embeddings, list) or not isinstance(sparse_raw, list):
            raise EmbeddingError("TEI 响应 embeddings/sparse_embeddings 必须为数组", endpoint=self.base_url)
        if len(embeddings) != expected or len(sparse_raw) != expected:
            raise EmbeddingError(
                f"TEI 响应条数与请求不一致（请求 {expected}，"
                f"响应 dense={len(embeddings)}，sparse={len(sparse_raw)}）",
                endpoint=self.base_url,
            )
        dense_rows: list[list[float]] = []
        sparse_rows: list[dict[int, float]] = []
        for dense, sparse in zip(embeddings, sparse_raw):
            if not isinstance(dense, list) or len(dense) != self._dense_dim:
                # 维度断言：与 Milvus Schema 强一致，防模型权重被误换（COM-IP-03 版本锁定）
                raise EmbeddingError(
                    f"稠密向量维度异常：{len(dense) if isinstance(dense, list) else '非数组'} "
                    f"!= {self._dense_dim}（疑似模型权重被误换，须停用核查）",
                    endpoint=self.base_url,
                )
            try:
                indices = sparse["indices"]
                values = sparse["values"]
                sparse_rows.append({int(idx): float(val) for idx, val in zip(indices, values)})
            except (TypeError, KeyError, ValueError) as exc:
                raise EmbeddingError(
                    f"稀疏向量格式异常（应为 {{indices, values}}）：{exc}", endpoint=self.base_url
                ) from exc
            dense_rows.append([float(x) for x in dense])
        return dense_rows, sparse_rows


class FakeEmbeddingClient:
    """确定性假实现（供全仓单元测试用，不发起任何网络请求）。

    确定性保证：
    - dense：以文本 blake2b 哈希为种子的伪随机 1024 维向量，L2 归一化为单位向量
      （种子与 PYTHONHASHSEED 无关，跨进程 / 跨运行稳定）；
    - sparse：按 unicode \\w 分词并小写化，{token_id: 词频}；token_id 为词元
      blake2b 哈希的稳定 32 位整数列号。

    另记录每次 embed_batch 的入参快照（calls），供测试断言分批与调用次数。
    """

    def __init__(self, dense_dim: int = DENSE_DIM) -> None:
        if dense_dim < 1:
            raise ValueError(f"dense_dim 必须 >= 1，当前 {dense_dim}")
        self._dense_dim = dense_dim
        self.calls: list[list[str]] = []  # 每次 embed_batch 的入参快照（测试断言用）

    @property
    def call_count(self) -> int:
        """embed_batch 累计调用次数（含空入参调用）。"""
        return len(self.calls)

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        """批量向量化（确定性假实现）：结果与入参等长按位对齐。"""
        if not isinstance(texts, list):
            raise TypeError(f"texts 必须为 list[str]，当前 {type(texts).__name__}")
        for i, item in enumerate(texts):
            if not isinstance(item, str):
                raise TypeError(f"texts[{i}] 必须为字符串，当前 {type(item).__name__}")
        self.calls.append(list(texts))
        return EmbeddingResult(
            dense=[self.dense_vector(t) for t in texts],
            sparse=[self.sparse_vector(t) for t in texts],
        )

    def dense_vector(self, text: str) -> list[float]:
        """单条文本的确定性稠密向量（哈希种子伪随机 + L2 单位化）。"""
        rng = random.Random(self.text_seed(text))
        vec = [rng.uniform(-1.0, 1.0) for _ in range(self._dense_dim)]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0  # 全零兜底，避免除零
        return [v / norm for v in vec]

    def sparse_vector(self, text: str) -> dict[int, float]:
        """单条文本的确定性稀疏向量：{token_id: 词频}（unicode 分词、小写化）。"""
        freq: dict[str, int] = {}
        for token in _TOKEN_RE.findall(text.lower()):
            freq[token] = freq.get(token, 0) + 1
        return {self.token_id(tok): float(count) for tok, count in sorted(freq.items())}

    @staticmethod
    def text_seed(text: str) -> int:
        """文本 -> 稳定 64 位随机种子（blake2b，跨进程一致）。"""
        return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")

    @staticmethod
    def token_id(token: str) -> int:
        """词元 -> 稳定 32 位稀疏列号（blake2b，跨进程一致）。"""
        return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "big")
