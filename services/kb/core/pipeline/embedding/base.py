# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""向量化服务公共契约（FR-KB-013，Phase 0）。

本模块定义向量化服务的对外稳定接口，下游模块一律面向本模块编程、
不得直接依赖具体客户端实现：

- EmbeddingResult：批量向量化结果（稠密 + 稀疏，与入参文本列表等长对齐）；
- EmbeddingClient：向量化客户端统一协议（同步 embed_batch）；
- EmbeddingError：超时 / 服务不可用的统一异常（中文上下文），上层捕获后
  按外部依赖韧性设计降级 BM25 关键词检索（02-技术选型论证 §5）。

已知消费方：FR-KB-014 Milvus 入库、FR-KB-017 增量 upsert、
FR-KB-030 查询向量化、批量编排 embed_orchestrator（本包内）。

实现方案：docs/07-实现方案/KB/SR-KB-013-向量化服务.md §5、§6.2
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from pydantic import BaseModel, model_validator

#: 稠密向量维度：BGE-M3 输出 1024 维（03-数据库设计 §4.2 FloatVector(1024)）
DENSE_DIM = 1024

#: 客户端分片批大小（05-核心模块详细设计 §2.1：batch=32）
DEFAULT_BATCH_SIZE = 32

#: 单请求超时秒数（02-技术选型论证 §5：BGE-M3 服务 2s）
DEFAULT_TIMEOUT_SECONDS = 2.0

#: 失败重试次数（02-技术选型论证 §5：×2）
DEFAULT_RETRIES = 2


class EmbeddingError(RuntimeError):
    """向量化服务统一异常：请求超时或重试耗尽后仍不可用（带中文上下文）。

    语义（SR-KB-013 §6.2/§6.3，两类调用方处理方式不同）：

    - 批量编排侧：任务滞留队列不丢失、状态不转 failed，TEI 恢复后续跑；
    - 查询侧（FR-KB-030）：捕获后降级 BM25-only 检索并告警。
    """

    def __init__(self, message: str, *, endpoint: str | None = None) -> None:
        super().__init__(message)
        self.message = message    # 中文错误上下文（对外展示）
        self.endpoint = endpoint  # 触发异常的 TEI 端点（审计 / 排障留痕）


class EmbeddingResult(BaseModel):
    """批量向量化结果：与入参文本列表按位一一对应（等长对齐）。

    dense[i] / sparse[i] 分别是第 i 条文本的 1024 维稠密向量与
    learned sparse 非零项 {列号: 权重}，写入 Milvus 双字段
    dense_vec / sparse_vec（03-数据库设计 §4.2）。
    """

    dense: list[list[float]]          # 每条文本的稠密向量（1024 维）
    sparse: list[dict[int, float]]    # 每条文本的稀疏向量非零项 {列号: 权重}

    @model_validator(mode="after")
    def _validate_alignment(self) -> "EmbeddingResult":
        """构造即校验：稠密 / 稀疏结果必须与文本列表等长对齐，防止错位入库。"""
        if len(self.dense) != len(self.sparse):
            raise ValueError(
                f"EmbeddingResult 稠密/稀疏长度不一致（dense={len(self.dense)}，"
                f"sparse={len(self.sparse)}），禁止错位入库"
            )
        return self

    def __len__(self) -> int:
        """批量结果条数（= 入参文本条数）。"""
        return len(self.dense)

    def rows(self) -> Iterator[tuple[list[float], dict[int, float]]]:
        """按位迭代 (dense, sparse) 结果对，供入库层逐条消费。"""
        return iter(zip(self.dense, self.sparse))


@runtime_checkable
class EmbeddingClient(Protocol):
    """向量化客户端统一协议（FR-KB-013 对外契约，下游按此集成）。

    已知实现：

    - TeiEmbeddingClient：内网 TEI /embed_all 真实客户端（client.py）；
    - FakeEmbeddingClient：确定性假实现，供全仓测试使用（client.py）。
    """

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        """批量向量化：返回与 texts 等长对齐的结果。

        约定：texts 为 list[str]（空列表返回空结果、零网络调用）；
        服务超时 / 不可用时抛 EmbeddingError，由调用方决定降级策略。
        """
        ...
