# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""入库路由（FR-KB-001~006 六库内容模型 → 检索/风控消费契约，迭代 0.3）。

三件事：
1. record_dims：六库 metadata 维度 → VectorRecord 标量维度
   （sku / scene_tag / process_node / reason_type / customer_ref）的映射规则。
   返回键与 services/kb/core/pipeline/sync/engine.py 的事件模型
   （ItemVectorMeta / KnowledgeItemEvent 的维度字段）同名对齐——引擎导入链
   较重（httpx），本文件不直接 import，由 test_kb_models_router.py 以
   dataclasses.fields(ItemVectorMeta) 做契约守护，两个模块互不耦合。

2. embedding_safe_metadata：分块/向量化侧的 metadata 投影——剥离敏感键并落
   显式标记（价格体系含底线价仅 PG 存储不入向量正文，SR-KB-001 §2 决策 2 /
   R-03；HMAC 指纹走标量等值检索不做语义化，SR-KB-002 §2 D3）。

3. export_intercept_rules：ProcessMeta 红线条目 → 风控引擎拦截规则清单
   （FR-AG-005 消费契约，SR-KB-004 §5②/§6 redline_export 的纯函数内核：
   本函数不碰 Redis/PG，版本协商与缓存在服务层包装）。

只读引用（不改其行为）：services/kb/core/vectorstore/base.py 的
FILTERABLE_FIELDS / FILTER_VALUE_MAX_LENGTH / KB_TYPES。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from services.kb.core.vectorstore.base import (
    FILTERABLE_FIELDS,
    FILTER_VALUE_MAX_LENGTH,
    KB_TYPES,
)

from .item import ItemStatus, KnowledgeItem, KnowledgeItemCreate
from .kb_types import (
    CustomerMeta,
    NegativeCaseMeta,
    ProcessMeta,
    ProductMeta,
    SkillMeta,
    TrainingMeta,
)

#: 五个检索过滤标量维度（VectorRecord 高频过滤列，03-数据库设计 §4.2）
DIM_FIELDS: tuple[str, ...] = ("sku", "scene_tag", "process_node", "reason_type", "customer_ref")

#: record_dims 返回的完整键集（kb_type 分区/事件路由 + 五个过滤维度）
DIM_PAYLOAD_KEYS: frozenset[str] = frozenset({"kb_type", *DIM_FIELDS})

#: 向量正文禁入的 metadata 键（按库；值仅 PG/标量链路使用，不做语义化）
VECTOR_EXCLUDED_METADATA_KEYS: dict[str, frozenset[str]] = {
    # 价格体系（含底线价 floor_price）不入向量正文：AI 报价只经 get_product_card
    # 从结构化字段取值，价格热更新仅动 PG + 缓存失效、向量零重建（SR-KB-001 §2 决策 2）
    "product": frozenset({"price"}),
    # HMAC 指纹是密文等值检索锚点（标量列），进入语义正文只会引入噪声（SR-KB-002 §2 D3）
    "customer": frozenset({"pii_digests"}),
}

#: 价格剥离标记键：投影结果以布尔显式标记「价格已剥离」「底线价仅授权可见」，
#: 使「价格（含底线价）不入向量正文」成为可断言的显式行为（测试与审计消费）
PRICE_REDACTED_MARKER: str = "price_redacted"
FLOOR_PRICE_AUTHORIZED_ONLY_MARKER: str = "floor_price_authorized_only"

#: 六库维度映射支持的条目形态（两者均有 kb_type / metadata 属性）
RoutableItem = Union[KnowledgeItem, KnowledgeItemCreate]


def record_dims(item: RoutableItem) -> dict[str, str]:
    """六库 metadata 维度 → VectorRecord 标量维度（条目粒度）。

    映射规则（各库方案「维度设计」节的条目级口径；块级细化在流水线内做）：
    - product       → sku=主 SKU（大写归一化后）；scene_tag 为卖点块级维度，
                      条目粒度不落（SR-KB-001 §4 映射表）
    - customer      → customer_ref=关联 customer.uid（分级标准类为空串）
    - skill         → scene_tag=主标签 scene_tags[0]（SR-KB-003 决策 4：
                      Milvus scene_tag 为单值，完整标签集合留在 PG metadata）
    - process       → 单节点条目落该节点编号；多节点/无节点为空串
                      （块级由 heading_path 解析覆盖，SR-KB-004 §6 chunk_mapper）
    - negative_case → reason_type=分类树路径（wrong_script 对照对子块在流水线内
                      以 reason_leaf 覆盖，SR-KB-005 §6.3）
    - training      → scene_tag=场景标签；scenario 素材另落 process_node=stage_node
                      （SR-KB-006 §4：过滤维度复用 scene_tag 与 process_node）

    返回恒含 kb_type + 五个维度键（不适用为空串），值长度对齐
    VectorRecord VarChar(64) 上限，超长即抛错（建模层兜底，fail-closed）。
    """
    meta = item.metadata
    kb_type = str(item.kb_type)
    sku = scene_tag = process_node = reason_type = customer_ref = ""

    if isinstance(meta, ProductMeta):
        sku = meta.sku  # 已在建模层大写归一化
    elif isinstance(meta, CustomerMeta):
        customer_ref = meta.customer_ref or ""
    elif isinstance(meta, SkillMeta):
        scene_tag = meta.scene_tags[0]  # 主标签（min_length=1 保证存在）
    elif isinstance(meta, ProcessMeta):
        # 单节点显式声明才落条目级标量；多节点/通识块由块级 heading 解析
        process_node = meta.process_nodes[0] if len(meta.process_nodes) == 1 else ""
    elif isinstance(meta, NegativeCaseMeta):
        reason_type = meta.reason_type
    elif isinstance(meta, TrainingMeta):
        scene_tag = meta.scene_tag
        if meta.material_kind == "scenario":
            process_node = meta.stage_node or ""
    else:  # pragma: no cover - 判别联合已穷举六库，防御分支
        raise TypeError(f"未支持的 metadata 类型：{type(meta).__name__}")

    dims = {
        "kb_type": kb_type,
        "sku": sku,
        "scene_tag": scene_tag,
        "process_node": process_node,
        "reason_type": reason_type,
        "customer_ref": customer_ref,
    }
    # 契约守护：键集与标量白名单对齐，值长度对齐 VarChar(64)
    assert set(dims) == DIM_PAYLOAD_KEYS
    assert set(DIM_FIELDS) <= FILTERABLE_FIELDS, "DIM_FIELDS 必须落在向量库过滤白名单内"
    if kb_type not in KB_TYPES:
        raise ValueError(f"kb_type 必须为六大知识库枚举之一 {KB_TYPES}，收到：{kb_type!r}")
    for field, value in dims.items():
        if len(value) > FILTER_VALUE_MAX_LENGTH:
            raise ValueError(
                f"维度 {field} 超长（上限 {FILTER_VALUE_MAX_LENGTH} 字符，对齐 VectorRecord 标量列）：{value[:32]!r}…"
            )
    return dims


def embedding_safe_metadata(item: RoutableItem) -> dict[str, object]:
    """向量化侧 metadata 投影：剥离 VECTOR_EXCLUDED_METADATA_KEYS 并落显式标记。

    - product：整个 price 键（含 floor_price 底线价）剥离，写入
      price_redacted=True 与 floor_price_authorized_only=<原底线价是否存在>；
      底线价数值绝不出现在投影 JSON 的任何位置（仅授权链路可见，NFR-SEC-02/R-03）。
    - customer：pii_digests 指纹剥离（等值检索走标量列，不做语义化）。
    """
    kb_type = str(item.kb_type)
    payload: dict[str, object] = item.metadata.model_dump(mode="json")
    excluded = VECTOR_EXCLUDED_METADATA_KEYS.get(kb_type, frozenset())
    for key in excluded:
        removed = payload.pop(key, None)
        if kb_type == "product" and key == "price" and removed is not None:
            price = removed if isinstance(removed, dict) else {}
            payload[PRICE_REDACTED_MARKER] = True
            # 标记行为：只落「是否存在底线价（仅授权可见）」布尔，不落数值
            payload[FLOOR_PRICE_AUTHORIZED_ONLY_MARKER] = price.get("floor_price") is not None
    return payload


def export_intercept_rules(
    items: Sequence[KnowledgeItem],
    version: int = 1,
    client_version: int | None = None,
) -> dict[str, object]:
    """红线条目 → 拦截规则清单（FR-AG-005 风控引擎消费契约，SR-KB-004 §5②）。

    - 仅导出 kb_type=process、status=published 且 is_redline=True 的条目
      （草稿/归档红线不进拦截清单，SR-KB-004 §9 test_export_only_published）；
    - 版本协商：client_version 与当前 version 一致 → unchanged=True、rules 为空
      （风控免重建省流量；version 的自增与缓存在服务层包装）；
    - rule_id 按 (title, uid) 稳定排序后顺序编号 RL-0001 起，重放结果确定。

    返回结构与 SR-KB-004 §5② 响应 data 部分一致：
    {version, unchanged, rules:[{rule_id, item_uid, title, action_type, risk_level,
    intercept_rule, advice, rule_source, effective_at, item_version}]}。
    """
    if client_version is not None and client_version == version:
        return {"version": version, "unchanged": True, "rules": []}

    rules: list[dict[str, object]] = []
    redlines = [
        item
        for item in items
        if item.kb_type == "process"
        and item.status == ItemStatus.PUBLISHED
        and isinstance(item.metadata, ProcessMeta)
        and item.metadata.is_redline
    ]
    for index, item in enumerate(sorted(redlines, key=lambda it: (it.title, str(it.uid))), start=1):
        meta: ProcessMeta = item.metadata
        rules.append(
            {
                "rule_id": f"RL-{index:04d}",
                "item_uid": str(item.uid),
                "title": item.title,
                "action_type": meta.action_type,
                "risk_level": meta.risk_level,
                "intercept_rule": meta.intercept_rule.model_dump(mode="json") if meta.intercept_rule else None,
                "advice": meta.advice,
                "rule_source": meta.rule_source,
                "effective_at": meta.effective_at.isoformat() if meta.effective_at else None,
                "item_version": item.version,
            }
        )
    return {"version": version, "unchanged": False, "rules": rules}


__all__ = [
    "DIM_FIELDS",
    "DIM_PAYLOAD_KEYS",
    "VECTOR_EXCLUDED_METADATA_KEYS",
    "PRICE_REDACTED_MARKER",
    "FLOOR_PRICE_AUTHORIZED_ONLY_MARKER",
    "RoutableItem",
    "record_dims",
    "embedding_safe_metadata",
    "export_intercept_rules",
]
