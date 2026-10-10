# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Schema 注册表下发 API（FR-KB-020 / SR-KB-020 §5①，迭代 0.3）。

GET /api/v1/knowledge/schemas（可选 ?kb_type= 单库）：输出六大知识库字段定义，
供前端动态表单渲染（前后端一套定义，杜绝「表单放行、后端拒绝」的口径漂移，
SR-KB-020 §2 决策 1）。

提供者为注入的鸭子契约（services/kb/models/registry.py 并行任务的对接口径，
**不 import 其实现**）：返回 ``[{kb_type, fields: [{name,label,required,control,options}]}]``。
本端点对字段做契约规整：五键必有（缺省 label=name / required=False /
control=input / options=None），契约外键（rules/visible_when/help 等）原样透传，
注册表同级键（如库中文 label）一并透传——前端零拼装。

偏差标注：SR §5① 示例的字段键为 key/widget（§6.1 FieldDef 口径），与本处
name/control 的收敛由注册表侧适配器完成（待设计文档评审合并，见 items.py 偏差②）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from services.kb.api.routes import CODE_BAD_REQUEST, ApiError

from ..schemas import KB_TYPE_ENUM
from .items import (
    AdminEnvelope,
    SchemaProvider,
    get_schema_provider,
    provider_schemas,
    request_trace_id,
)

# --------------------------------------------------------------------------- 契约模型

#: 字段定义契约五键（SR-KB-020 对接口径；规整后必有，缺省值见 normalize_field）
_FIELD_CONTRACT_KEYS: tuple[str, ...] = ("name", "label", "required", "control", "options")


class SchemasData(BaseModel):
    """Schema 下发 data 段：kb_type → 库定义映射。"""

    schemas: dict[str, dict[str, Any]] = Field(
        ..., description="六大知识库字段定义（键为 kb_type，值为 {fields: [...], ...注册表透传键}）"
    )


#: 端点信封别名（FastAPI response_model 消费参数化具体模型）
SchemasEnvelope = AdminEnvelope[SchemasData]


class EmptySchemaProvider:
    """空注册表占位（未接线 registry 时的缺省注入：schemas 返回空映射）。"""

    def list_schemas(self) -> list[dict[str, Any]]:
        """占位实现：无任何库定义。"""
        return []

# --------------------------------------------------------------------------- 字段规整


def normalize_field(raw: Mapping[str, Any]) -> dict[str, Any]:
    """字段定义规整为契约五键必有 + 契约外键透传（见模块 docstring）。

    前五键按缺省规则补齐（label=name / required=False / control=input / options=None），
    契约外键原样保留；输出恒含 _FIELD_CONTRACT_KEYS 全部五键。
    """
    name = str(raw.get("name", ""))
    normalized: dict[str, Any] = {
        "name": name,
        "label": str(raw.get("label", name)),
        "required": bool(raw.get("required", False)),
        "control": str(raw.get("control", "input")),
        "options": raw.get("options"),
    }
    for key, value in raw.items():
        if key not in _FIELD_CONTRACT_KEYS:
            normalized[key] = value
    return normalized

# --------------------------------------------------------------------------- 端点（FR-KB-020 §5①）

router = APIRouter(prefix="/api/v1/knowledge", tags=["kb-admin"])


@router.get(
    "/schemas",
    response_model=SchemasEnvelope,
    summary="Schema 注册表下发（前端动态表单渲染依据）",
)
async def get_schemas(
    request: Request,
    kb_type: str | None = Query(None, description="单库下发（缺省全部已注册知识库）"),
    provider: SchemaProvider = Depends(get_schema_provider),
) -> SchemasEnvelope:
    """下发字段定义清单：规整五键契约 + 透传扩展键；kb_type 非法枚举 → 40001。

    同一 kb_type 多条登记时字段按序合并（注册表分段登记的宽容口径）；
    合法枚举但注册表未登记（如 customer 未接线）→ 该库不出现在结果中（空集语义）。
    """
    trace_id = request_trace_id(request)
    if kb_type is not None and kb_type not in KB_TYPE_ENUM:
        raise ApiError(
            CODE_BAD_REQUEST,
            400,
            "参数校验失败：kb_type 非法",
            {"errors": [{"field": "kb_type", "msg": f"未知知识库类型 {kb_type}（合法值 {list(KB_TYPE_ENUM)}）"}]},
        )
    schemas: dict[str, dict[str, Any]] = {}
    for entry in provider_schemas(provider):
        entry_type = entry.get("kb_type")
        if not entry_type or (kb_type is not None and entry_type != kb_type):
            continue
        bucket = schemas.setdefault(str(entry_type), {"fields": []})
        bucket["fields"].extend(normalize_field(raw) for raw in (entry.get("fields") or ()))
        for key, value in entry.items():
            if key not in ("kb_type", "fields") and key not in bucket:
                bucket[key] = value
    return SchemasEnvelope(trace_id=trace_id, data=SchemasData(schemas=schemas))
