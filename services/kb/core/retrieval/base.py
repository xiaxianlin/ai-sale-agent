# 知识产权归甲方所有（COM-IP-01）
"""检索三段式共享内核（FR-KB-015 / SR-KB-015 §5~§6）。

本模块承载三段式链路（改写 → 多路召回 → 重排）各段之间的公共契约：
    - maybe_await：外部依赖鸭子类型的同步/异步双形态兼容器；
    - Hit：召回候选单元（chunk 级），贯穿召回、融合、重排、组装各段；
    - RetrievalDownstreamError：下游故障（向量库不可用等），对应 04-接口设计
      §1.3 错误码 50002 的模块内语义（API 层负责映射为 HTTP 响应）。

依赖边界：向量库（store）与向量化（embedder）模块由并行任务建设，接口已冻结
（hybrid_search / embed_batch），本包一律按 Protocol 鸭子类型调用，不做具体实现
导入——测试中自建桩，生产中由装配层注入。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, TypeVar

T = TypeVar("T")


async def maybe_await(value: T | Awaitable[T]) -> T:
    """兼容同步/异步两种鸭子类型实现：可等待则等待，否则原样返回。

    向量库/向量化/LLM 钩子的冻结接口未约定同步或异步形态（Milvus 客户端为
    同步、TEI httpx 客户端为异步），统一经本函数调用可同时接纳两种实现，
    装配层无需适配器。
    """
    if inspect.isawaitable(value):
        return await value
    return value  # type: ignore[return-value]


@dataclass
class Hit:
    """召回候选单元：一个 knowledge_chunk 的检索视图（SR-KB-015 §5）。

    字段口径：
        - score：当前生效得分——召回段为 RRF 融合分，重排后被精排分覆盖；
        - route / routes：首命中路与全部命中路（各路召回贡献观测，§6.5）；
        - rrf_score：RRF 融合得分（重排后保留，供 NDCG@5 提升量离线评测）；
        - rerank_score：精排得分，None 表示该候选未经精排（降级直出的判据）。
    """

    chunk_id: str | int
    score: float = 0.0
    route: str = ""
    routes: list[str] = field(default_factory=list)
    snippet: str = ""  # 摘录文本（重排送分文本；正式部署由 PG 回表补全正文）
    title: str = ""
    kb_type: str = ""
    source: str = ""
    rrf_score: float = 0.0
    rerank_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class RetrievalDownstreamError(RuntimeError):
    """检索下游故障：全部召回路均失败，链路无可用降级（50002 语义）。

    SR-KB-015 §6.1：Milvus 故障 → 50002 + 告警——单路失败走 degrade 留痕，
    仅当所有已发起的召回路全部失败时抛出本异常。
    """

    def __init__(self, component: str, reason: str = "") -> None:
        self.component = component
        self.reason = reason
        super().__init__(f"检索下游故障（{component}）: {reason}" if reason else f"检索下游故障（{component}）")
