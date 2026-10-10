# 知识产权归甲方所有（COM-IP-01）
"""Milvus 向量存储实现（FR-KB-014 生产后端，迭代 0.2）。

联调条件（本模块逻辑写全，但无法在本仓库 CI 环境运行，原因与前提如下）：
1. 本机未安装 pymilvus——MilvusStore() 构造时 import 守护抛中文提示
   （MilvusDependencyError）；本地开发与单元测试请使用 InMemoryVectorStore；
2. 需 docker compose 拉起 Milvus 2.5 standalone（etcd + minio + milvus 三容器，
   SR-KB-014 §6.1 部署片段）；
3. 联调用例：init_collection 幂等建表 → 灌数 upsert → hybrid_search 与
   InMemoryVectorStore 排序对拍（小数据集上融合序应一致）。

与 SR-KB-014 的对应关系：
- §6.1 init_collection：幂等建表（HNSW M=16/efC=200 + SPARSE_INVERTED_INDEX +
  标量 INVERTED，Bounded 一致性）；已存在则跳过，防误删重建；
- §6.2 分区：P0-P4 按 kb_type 六分区路由写入；P5 切换 tenant_id Partition Key
  （自动路由，写入不再指定分区名）；
- §6.3 hybrid_search：AnnSearchRequest 双路（稠密 ef / 稀疏 drop_ratio_search）
  + RRFRanker 融合；expr 由简化 DSL 翻译为 Milvus 方言，并强制注入
  tenant_id 与 item_status == "published" 条件（调用方不可省略）；
- §6.4 upsert / delete：by chunk_id 幂等；delete 用主键列表拼 filter，无注入面。

偏差说明：SR §6.3 示例为 async（asyncio.to_thread 包装）；本迭代 VectorStore
契约为同步方法（base.py），事件循环包装由 kb-service 的 FastAPI 调用方承担，
存储层不感知 async——契约一致性与联调时机见迭代计划。
"""
from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any

from .base import (
    DEFAULT_TENANT_ID,
    ITEM_STATUS_PUBLISHED,
    FilterCondition,
    KB_TYPES,
    SearchHit,
    VectorRecord,
    parse_filter_expr,
)
from .collection_schema import (
    COLLECTION_NAME,
    CONSISTENCY_LEVEL,
    DEFAULT_RRF_K,
    DEFAULT_SPARSE_DROP_RATIO_SEARCH,
    DEFAULT_SEARCH_EF,
    DENSE_INDEX_SPEC,
    DENSE_DIM,
    FIELD_CHUNK_ID,
    FIELD_DENSE_VEC,
    FIELD_ITEM_STATUS,
    FIELD_KB_TYPE,
    FIELD_SPARSE_VEC,
    FIELD_TENANT_ID,
    PARTITION_MODE_KB_TYPE,
    PARTITION_MODE_TENANT,
    SCALAR_INDEX_TYPE,
    SCALAR_INVERTED_FIELDS,
    SCALAR_VARCHAR_FIELDS,
    SEARCH_OUTPUT_FIELDS,
    SPARSE_INDEX_SPEC,
    TENANT_PARTITION_NUM,
    VARCHAR_LIMITS,
    record_to_milvus_row,
)


class MilvusStoreError(RuntimeError):
    """MilvusStore 相关错误基类（供调用方统一捕获）。"""


class MilvusDependencyError(MilvusStoreError):
    """pymilvus 未安装（import 守护触发），中文提示安装与联调条件。"""


class MilvusUnavailableError(MilvusStoreError):
    """Milvus 连接/读写失败——对应业务码 E_VEC_UNAVAILABLE（检索 API 503 + critical 告警）。"""


def _load_pymilvus() -> Any:
    """pymilvus import 守护：未安装时抛中文提示（含安装命令与替代实现指引）。"""
    try:
        return importlib.import_module("pymilvus")
    except ImportError as exc:
        raise MilvusDependencyError(
            "未安装 pymilvus：MilvusStore 为生产实现，需在联调环境执行 "
            'pip install "pymilvus>=2.5" 并连接 Milvus 2.5 standalone'
            "（SR-KB-014 §6.1）；本地开发与单元测试请使用 InMemoryVectorStore。"
        ) from exc


def _quote_milvus_str(value: str) -> str:
    """Milvus expr 字符串字面量：双引号包裹，转义反斜杠与双引号（表达式注入防护）。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def translate_expr_to_milvus(expr: str | None, *, tenant_id: int = DEFAULT_TENANT_ID) -> str:
    """简化 DSL → Milvus expr 方言翻译，并强制注入租户与 published 条件（SR §6.3）。

    - `kb_type in ('product','skill')` → `kb_type in ["product", "skill"]`（分区裁剪条件）；
    - `sku = 'ST-3200'` → `sku == "ST-3200"`；
    - 强制前缀：`tenant_id == <n> and item_status == "published"`——调用方不可省略
      （P5 租户隔离中间件注入口径，NFR-SEC-07）；
    - 解析复用 base.parse_filter_expr（同一白名单与 fail-closed 语义），
      值转义由 _quote_milvus_str 承担。纯函数，无 pymilvus 依赖，可单测。
    """
    conditions: list[FilterCondition] = parse_filter_expr(expr)
    parts: list[str] = [
        f"tenant_id == {int(tenant_id)}",
        f'{FIELD_ITEM_STATUS} == "{ITEM_STATUS_PUBLISHED}"',
    ]
    for condition in conditions:
        if condition.op == "in":
            values = ", ".join(_quote_milvus_str(v) for v in condition.values)
            parts.append(f"{condition.field} in [{values}]")
        else:  # '='：单值等值
            parts.append(f"{condition.field} == {_quote_milvus_str(condition.values[0])}")
    return " and ".join(parts)


class MilvusStore:
    """VectorStore 协议的 Milvus 生产实现（pymilvus >= 2.5，Milvus 2.5 standalone/cluster）。

    本类逻辑按 SR-KB-014 §6.1~§6.4 写全；在本仓库（无 pymilvus / 无 Milvus 实例）
    只能触发 import 守护路径，联调条件见模块 docstring。
    """

    def __init__(
        self,
        uri: str = "http://localhost:19530",
        *,
        collection: str = COLLECTION_NAME,
        partition_mode: str = PARTITION_MODE_KB_TYPE,
        tenant_id: int = DEFAULT_TENANT_ID,
        rrf_k: int = DEFAULT_RRF_K,
        search_ef: int = DEFAULT_SEARCH_EF,
        sparse_drop_ratio_search: float = DEFAULT_SPARSE_DROP_RATIO_SEARCH,
        timeout_ms: int = 300,
    ) -> None:
        """初始化：import 守护 → 建连 → 幂等建表（配置项对齐 SR-KB-014 §7 app_config）。

        - partition_mode：'kb_type'（P0-P4 六分区）| 'tenant'（P5 Partition Key，切换走 CD/CR）；
        - timeout_ms：hybrid_search 超时预算（500ms 总预算内，SR §7 KB_VEC_SEARCH_TIMEOUT_MS）。
        """
        if partition_mode not in (PARTITION_MODE_KB_TYPE, PARTITION_MODE_TENANT):
            raise ValueError(f"partition_mode 仅支持 kb_type / tenant，收到：{partition_mode!r}")
        self._pm = _load_pymilvus()  # 未安装 pymilvus 时在此抛 MilvusDependencyError
        self._collection = collection
        self._partition_mode = partition_mode
        self._tenant_id = tenant_id
        self._rrf_k = rrf_k
        self._search_ef = search_ef
        self._sparse_drop_ratio_search = sparse_drop_ratio_search
        self._timeout_ms = timeout_ms
        # MilvusClient 建连（SR-KB-014 §3 client.py 单例职责的迭代内联版）
        self._client = self._pm.MilvusClient(uri=uri, timeout=timeout_ms)
        self._init_collection()

    # ── §6.1 Collection 初始化与索引创建（幂等）──
    def _init_collection(self) -> None:
        """幂等建表：已存在则跳过（防误删重建）；schema 变更必须走变更管理取号（CD/CR）。"""
        pm = self._pm
        client = self._client
        if not client.has_collection(self._collection):
            schema = pm.MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field(
                FIELD_CHUNK_ID, pm.DataType.INT64, is_primary=True,
                description="=PG knowledge_chunk.id",
            )
            schema.add_field(
                FIELD_TENANT_ID, pm.DataType.INT64,
                partition_key=(self._partition_mode == PARTITION_MODE_TENANT),
                num_partitions=TENANT_PARTITION_NUM,
                description="P0-P4 恒 1；P5 租户哈希分区",
            )
            schema.add_field(
                FIELD_KB_TYPE, pm.DataType.VARCHAR, max_length=VARCHAR_LIMITS[FIELD_KB_TYPE],
            )
            schema.add_field(FIELD_DENSE_VEC, pm.DataType.FLOAT_VECTOR, dim=DENSE_DIM)
            schema.add_field(FIELD_SPARSE_VEC, pm.DataType.SPARSE_FLOAT_VECTOR)
            for field in SCALAR_VARCHAR_FIELDS:  # 五个高频过滤维度，默认空串
                schema.add_field(
                    field, pm.DataType.VARCHAR,
                    max_length=VARCHAR_LIMITS[field], default_value="",
                )
            schema.add_field(
                FIELD_ITEM_STATUS, pm.DataType.VARCHAR,
                max_length=VARCHAR_LIMITS[FIELD_ITEM_STATUS],
                default_value=ITEM_STATUS_PUBLISHED,
            )
            index_params = pm.MilvusClient.prepare_index_params()
            index_params.add_index(
                field_name=FIELD_DENSE_VEC, index_type=DENSE_INDEX_SPEC["index_type"],
                metric_type=DENSE_INDEX_SPEC["metric_type"],
                params=dict(DENSE_INDEX_SPEC["params"]),  # type: ignore[arg-type]
            )
            index_params.add_index(
                field_name=FIELD_SPARSE_VEC, index_type=SPARSE_INDEX_SPEC["index_type"],
                metric_type=SPARSE_INDEX_SPEC["metric_type"],
            )
            for field in SCALAR_INVERTED_FIELDS:  # 标量过滤加速（kb_type P5 降级后由其承担）
                index_params.add_index(field_name=field, index_type=SCALAR_INDEX_TYPE)
            client.create_collection(
                self._collection, schema=schema, index_params=index_params,
                consistency_level=CONSISTENCY_LEVEL,  # Bounded：增量 ≤3s 可见（FR-KB-017）
            )
        if self._partition_mode == PARTITION_MODE_KB_TYPE:  # P0-P4：六大库各一分区
            for kb_type in KB_TYPES:
                if not client.has_partition(self._collection, partition_name=kb_type):
                    client.create_partition(self._collection, partition_name=kb_type)

    # ── §6.4 入库与删除 ──
    def upsert(self, records: list[VectorRecord]) -> None:
        """批量写入：按 kb_type 分组路由六分区（P0-P4）/ tenant Partition Key 自动路由（P5）。

        upsert by chunk_id 幂等：重试/重放无副作用（FR-KB-017）。
        """
        try:
            if self._partition_mode == PARTITION_MODE_KB_TYPE:
                grouped: dict[str, list[VectorRecord]] = {}
                for record in records:
                    grouped.setdefault(record.kb_type, []).append(record)
                for kb_type, group in grouped.items():
                    self._client.upsert(
                        self._collection, partition_name=kb_type,
                        data=[record_to_milvus_row(record) for record in group],
                    )
            else:  # P5：Partition Key 由服务端按 tenant_id 哈希路由
                self._client.upsert(
                    self._collection,
                    data=[record_to_milvus_row(record) for record in records],
                )
        except Exception as exc:  # pymilvus.MilvusException 等读写失败统一转译
            raise MilvusUnavailableError(f"Milvus upsert 失败：{exc}") from exc
        # 联调修正：写入后显式 flush，保证「写后即检」可见性（FR-KB-017 分钟级生效；
        # Milvus 缺省后台落盘延迟可达数秒，集成对拍曾因此 0 命中）
        try:
            self._client.flush(self._collection)
        except Exception as exc:  # noqa: BLE001  flush 失败不阻断写入（后台仍会落盘）
            raise MilvusUnavailableError(f"Milvus flush 失败（数据已写入）：{exc}") from exc

    def delete(self, chunk_ids: list[int]) -> None:
        """按主键批量删除：filter 仅由 int 字面量拼接，无注入面（SR §6.4）。"""
        if not chunk_ids:
            return  # 空列表幂等直通
        ids = ",".join(str(int(chunk_id)) for chunk_id in chunk_ids)
        try:
            self._client.delete(self._collection, filter=f"chunk_id in [{ids}]")
        except Exception as exc:
            raise MilvusUnavailableError(f"Milvus delete 失败：{exc}") from exc

    # ── §6.3 混合检索：标量过滤 expr + 多路 ANN + RRF 融合 ──
    def hybrid_search(
        self,
        dense: Sequence[float] | None = None,
        sparse: Mapping[int, float] | None = None,
        expr: str | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """双路 AnnSearchRequest + RRFRanker 融合，映射为 SearchHit 列表。

        - 稠密路：HNSW/IP，检索 ef 随召回率调参（KB_VEC_SEARCH_EF）；
        - 稀疏路：SPARSE_INVERTED_INDEX/IP，drop_ratio_search 裁低权重稀疏项提速；
        - expr：简化 DSL 经 translate_expr_to_milvus 翻译并强制注入租户/published 条件。
        """
        if dense is None and sparse is None:
            raise ValueError("dense 与 sparse 查询向量至少提供一路")
        milvus_expr = translate_expr_to_milvus(expr, tenant_id=self._tenant_id)
        requests: list[Any] = []
        if dense is not None:
            requests.append(
                self._pm.AnnSearchRequest(
                    data=[list(dense)], anns_field=FIELD_DENSE_VEC, limit=top_k,
                    expr=milvus_expr,
                    param={"metric_type": "IP", "params": {"ef": self._search_ef}},
                )
            )
        if sparse is not None:  # pymilvus 稀疏向量采用 {下标字符串: 权重} 行格式
            requests.append(
                self._pm.AnnSearchRequest(
                    data=[{str(index): float(weight) for index, weight in sparse.items()}],
                    anns_field=FIELD_SPARSE_VEC, limit=top_k, expr=milvus_expr,
                    param={
                        "metric_type": "IP",
                        "params": {"drop_ratio_search": self._sparse_drop_ratio_search},
                    },
                )
            )
        try:
            results = self._client.hybrid_search(
                self._collection,
                reqs=requests,
                ranker=self._pm.RRFRanker(self._rrf_k),
                limit=top_k,
                output_fields=list(SEARCH_OUTPUT_FIELDS),
            )
        except Exception as exc:
            # 检索链路整体不可用：快速失败交上层 503 + critical 告警（SR §2 不做半开降级）
            raise MilvusUnavailableError(f"Milvus hybrid_search 失败：{exc}") from exc
        hits = results[0] if results else []
        return [
            SearchHit(chunk_id=int(hit["entity"][FIELD_CHUNK_ID]), score=float(hit["distance"]))
            for hit in hits
        ]


__all__ = [
    "MilvusStore",
    "MilvusStoreError",
    "MilvusDependencyError",
    "MilvusUnavailableError",
    "translate_expr_to_milvus",
]
