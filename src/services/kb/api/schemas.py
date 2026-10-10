# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""统一检索 API 契约模型（FR-KB-030，契约冻结 v1，SR-KB-030 §6.1）。

本文件即「契约即代码」的冻结基线：字段名/类型/默认值与
02-设计文档/04-接口设计.md §2 示例**逐字段一致**；P0 验收后任何签名变更必须
走 04 §5 版本化流程（向下兼容新增 → v1.1 并登记变更；破坏性变更 → v2，
v1 保留过渡期 ≥1 个阶段），并同步 docs/openapi/openapi.yaml 冻结快照。

冻结口径：
- 请求：query（1~512 字符必填）、kb_types（六大库枚举子集，缺省查全部）、
  filters（五维白名单 sku/scene_tag/process_node/reason_type/customer_ref）、
  top_k（1~20，缺省 5）、rerank（缺省 true，走三段式链路）；
- 响应：统一信封 {code, message, data, trace_id}（04 §1.3），data 内
  results[{chunk_id, kb_type, title, snippet, source{item_uid, origin_file,
  version}, score}] + latency_ms + rewritten_query 与 §2 示例逐字段一致。

偏差标注：SR-KB-030 §6.1 草案中 SearchData.meta（cache_hit/degraded/stage_ms
观测增补）按其 §5 细化标注属 v1.1 向下兼容新增，**不进 v1 冻结基线**；
kw_types 非法枚举与 filters 非白名单维度均映射 40001（见 routes.py 错误处理）。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 六大知识库枚举（03-数据库设计 §4.2 kb_type 取值，与 vectorstore.KB_TYPES 同源）
KB_TYPE_ENUM: tuple[str, ...] = (
    "product",
    "customer",
    "skill",
    "process",
    "negative_case",
    "training",
)

#: 过滤维度白名单（03-数据库设计 §4.2 标量维度；超出即 40001，fail-closed）
FILTER_DIMENSIONS: tuple[str, ...] = ("sku", "scene_tag", "process_node", "reason_type", "customer_ref")

#: query 长度边界（KB_API_QUERY_MAX_LEN，SR-KB-030 §7，取值范围 64~2048）
QUERY_MAX_LENGTH: int = 512

#: top_k 全局上限（KB_API_TOPK_MAX，与 SR-KB-015 KB_SEARCH_TOPK_MAX 同源）
TOPK_MAX: int = 20

#: top_k 契约缺省值（KB_API_TOPK_DEFAULT；属冻结字段，改动须升 v2）
TOPK_DEFAULT: int = 5


class SearchRequest(BaseModel):
    """检索入参模型（04-接口设计 §2 请求示例逐字段一致，冻结 v1）。

    extra="forbid"：契约外字段 fail-closed 拒绝（映射 40001）——冻结接口的
    防漂移治理口径，杜绝消费方字段拼错的静默吞没。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=QUERY_MAX_LENGTH,
        description="查询文本（1~512 字符）",
        examples=["ST-3200 支架的最大承载和认证情况"],
    )
    kb_types: list[str] | None = Field(
        None,
        description="知识库类型子集（六大库枚举）；缺省查全部六大库",
        examples=[["product", "process"]],
    )
    filters: dict[str, list[str]] | None = Field(
        None,
        description="标量维度过滤（维度白名单校验，各维度间 AND 组合）",
        examples=[{"sku": ["ST-3200"], "scene_tag": ["询价"], "process_node": ["报价"]}],
    )
    top_k: int = Field(TOPK_DEFAULT, ge=1, le=TOPK_MAX, description="返回条数；缺省 5，上限 20")
    rerank: bool = Field(True, description="是否走三段式精排（SR-KB-015）；缺省 true")

    @field_validator("kb_types")
    @classmethod
    def _check_kb_types(cls, value: list[str] | None) -> list[str] | None:
        """kb_types 枚举校验：含未知类型即拒绝并列出合法值（40001 定位信息）。"""
        if value and (bad := set(value) - set(KB_TYPE_ENUM)):
            raise ValueError(f"kb_types 含未知类型: {sorted(bad)}（合法值 {list(KB_TYPE_ENUM)}）")
        return value

    @field_validator("filters")
    @classmethod
    def _check_filter_whitelist(cls, value: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        """filters 维度白名单校验：非白名单维度拒绝并列出合法维度（fail-closed）。"""
        if value and (bad := set(value) - set(FILTER_DIMENSIONS)):
            raise ValueError(f"filters 含非白名单维度: {sorted(bad)}（合法维度 {list(FILTER_DIMENSIONS)}）")
        return value


class SourceRef(BaseModel):
    """来源追溯（04 §2 source 对象；SRS §12.2 可回溯原始材料与版本）。"""

    item_uid: str = Field(..., description="知识条目 UID")
    origin_file: str = Field(..., description="原始材料路径")
    version: int = Field(..., description="条目当前版本号")


class SearchResultItem(BaseModel):
    """单条检索结果（04 §2 results 数组元素）。"""

    chunk_id: int = Field(..., description="知识块主键（= PG knowledge_chunk.id）")
    kb_type: str = Field(..., description="所属知识库类型（六大库枚举）")
    title: str = Field(..., description="知识条目标题（回表补齐）")
    snippet: str = Field(..., description="命中摘录文本（检索视图携带）")
    source: SourceRef = Field(..., description="来源追溯（回表补齐）")
    score: float = Field(..., description="得分（重排生效时为精排分，否则为 RRF 融合分）")


class SearchData(BaseModel):
    """检索结果 data 段（04 §2 响应 data 逐字段一致，冻结 v1）。"""

    results: list[SearchResultItem] = Field(..., description="命中结果列表（按得分降序）")
    latency_ms: int = Field(..., description="端到端时延（毫秒，NFR-PER-02 在线测量口径）")
    rewritten_query: str = Field(..., description="查询改写结果（SR-KB-015，可观测）")


class SearchResponse(BaseModel):
    """统一响应信封（04-接口设计 §1.3：code/message/data/trace_id 四键齐全）。"""

    code: int = Field(0, description="0=成功；非 0 见 §1.3 错误码表")
    message: str = Field("ok", description="成功恒 ok；失败为错误摘要")
    data: SearchData
    trace_id: str = Field(..., description="全链路追踪 ID（X-Trace-Id 透传或新生成 uuid）")
