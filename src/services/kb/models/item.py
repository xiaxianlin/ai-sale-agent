# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""知识条目模型（FR-KB-001~006 六库共用，迭代 0.3）。

对齐 03-数据库设计 §3.1 knowledge_item 表：
- uid / kb_type / title / content / metadata(JSONB) / status / current_version
  （模型字段名 version 对应表列 current_version）+ 审计四字段
  （created_at / created_by / updated_at / updated_by）；
- metadata 为按 kb_type 判别的 discriminated union（kb_types.KbMetadata），
  创建即自动按库校验合法性（FR-KB-020 字段随类型自适应的建模层落点）。

创建链路约定（FR-KB-020 ③）：
    KnowledgeItemCreate(kb_type=..., title=..., content=..., metadata=...)
      → pydantic 按 metadata.kb_type 判别联合校验（六库各自的结构化规则全量生效）
      → 交叉校验 metadata.kb_type == kb_type（防手滑错库）
      → KnowledgeItem.from_create(create, actor=...) 落 uid/status/version/审计四字段。

metadata 判别标签自动注入：调用方可以不在 metadata 里重复携带 kb_type，
创建模型会从条目级 kb_type 注入判别标签后再走联合校验（两种写法等价）。
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .kb_types import KBType, KbMetadata

#: 条目状态（03-数据库设计 §3.1 status CHECK：draft / published / archived）
ITEM_STATUSES: tuple[str, ...] = ("draft", "published", "archived")


class ItemStatus(StrEnum):
    """条目生命周期状态（仅 published 参与检索，见 vectorstore.base.ITEM_STATUS_PUBLISHED）。"""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


def _utcnow() -> datetime:
    """UTC 时间戳（审计字段缺省值，带时区）。"""
    return datetime.now(timezone.utc)


class KnowledgeItemCreate(BaseModel):
    """知识条目创建载荷（FR-KB-020 创建/更新路由的请求体核心）。

    创建时按 kb_type 自动校验 metadata 合法性：
    - metadata 为 discriminated union（判别字段 kb_type）；
    - before 校验器把条目级 kb_type 注入缺失的判别标签（调用方可省写）；
    - after 校验器交叉核对两处 kb_type 一致（错库组合直接拒绝）。
    """

    model_config = ConfigDict(extra="forbid")

    kb_type: KBType
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", description="结构化正文（Markdown，向量化输入）")
    metadata: KbMetadata

    @model_validator(mode="before")
    @classmethod
    def _inject_discriminator_tag(cls, data: object) -> object:
        """判别标签注入：metadata 缺 kb_type 时以条目级 kb_type 补齐（不改动原入参）。"""
        if not isinstance(data, Mapping):
            return data
        metadata = data.get("metadata")
        kb_type = data.get("kb_type")
        if isinstance(metadata, Mapping) and "kb_type" not in metadata and kb_type is not None:
            merged = dict(data)
            merged["metadata"] = {**metadata, "kb_type": str(kb_type)}
            return merged
        return data

    @model_validator(mode="after")
    def _check_kb_type_match(self) -> "KnowledgeItemCreate":
        """交叉校验：metadata 判别标签必须与条目级 kb_type 一致（防错库组合）。

        用值比较而非身份比较：Literal 校验通过后 kb_type 可能以 str 形态返回
        （"product"），而条目级字段是 KBType 枚举成员——StrEnum 值相等即可判定一致。
        """
        if self.metadata.kb_type != self.kb_type:
            raise ValueError(
                f"kb_type 与 metadata 判别标签不一致：item.kb_type={self.kb_type!r}，"
                f"metadata.kb_type={self.metadata.kb_type!r}"
            )
        return self


class KnowledgeItem(BaseModel):
    """知识条目（PG knowledge_item 的 Python 侧投影，03-数据库设计 §3.1）。

    audit 四字段 = created_at / created_by / updated_at / updated_by；
    uid 为对外稳定标识（UUID），version 对应表列 current_version（发布 +1，
    配合 knowledge_version 快照，FR-KB-022）。
    """

    model_config = ConfigDict(extra="forbid")

    uid: uuid.UUID = Field(default_factory=uuid.uuid4)
    kb_type: KBType
    title: str = Field(min_length=1, max_length=200)
    content: str = ""
    metadata: KbMetadata
    status: ItemStatus = ItemStatus.DRAFT
    version: int = Field(default=1, ge=1, description="当前版本（=表列 current_version）")
    # ── 审计四字段（03-数据库设计 §3.1 created/updated 对；NFR-SEC-03 审计链上游）──
    created_at: datetime = Field(default_factory=_utcnow)
    created_by: str = Field(default="", max_length=64)
    updated_at: datetime = Field(default_factory=_utcnow)
    updated_by: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _check_kb_type_match(self) -> "KnowledgeItem":
        """交叉校验：条目 kb_type 与 metadata 判别标签一致（直接构造路径同样守护）。"""
        if self.metadata.kb_type != self.kb_type:
            raise ValueError(
                f"kb_type 与 metadata 判别标签不一致：item.kb_type={self.kb_type!r}，"
                f"metadata.kb_type={self.metadata.kb_type!r}"
            )
        return self

    @classmethod
    def from_create(cls, create: KnowledgeItemCreate, actor: str = "") -> "KnowledgeItem":
        """从创建载荷落成条目：uid 生成、状态 draft、版本 1、审计四字段就位。"""
        now = _utcnow()
        return cls(
            kb_type=create.kb_type,
            title=create.title,
            content=create.content,
            metadata=create.metadata,
            status=ItemStatus.DRAFT,
            version=1,
            created_at=now,
            created_by=actor,
            updated_at=now,
            updated_by=actor,
        )


__all__ = [
    "ITEM_STATUSES",
    "ItemStatus",
    "KnowledgeItemCreate",
    "KnowledgeItem",
]
