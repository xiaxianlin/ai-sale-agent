# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""管理端看板统计 API（FR-KB-020 可视化管理·看板数据，迭代 0.3）。

GET /api/v1/admin/stats：条目总数 / 状态分布 / 近 7 日变更——管理后台首页看板数据源
（六库 Tab 总角标、草稿-发布-归档占比、近 7 日活跃度曲线）。

口径标注：
- 统计范围不含软删行（与列表可见性一致）；
- 「近 7 日变更」按条目最新 updated_at 计（含当日共 7 个自然日刻度，空日补 0）；
  历史版本事件流（每日多次修改逐笔计数）属 FR-KB-022 版本快照表，随该迭代接入；
- 聚合经存储协议 iter_all 全量遍历（内存实现）；PG 生产形态应在 SQL 侧聚合
  （status/kb_type GROUP BY + updated_at 直方图），协议不变、端点组装逻辑不变。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .items import (
    ITEM_STATUSES,
    AdminEnvelope,
    KnowledgeItemStore,
    get_item_store,
    request_trace_id,
)

# --------------------------------------------------------------------------- 契约模型


class DailyChange(BaseModel):
    """单日变更计数（看板活跃度曲线单点）。"""

    date: str = Field(..., description="自然日（UTC，YYYY-MM-DD）")
    count: int = Field(..., description="当日发生变更的条目数")


class ChangesLast7Days(BaseModel):
    """近 7 日变更汇总（含当日，7 个自然日连续刻度）。"""

    total: int = Field(..., description="近 7 日发生变更的条目总数")
    by_day: list[DailyChange] = Field(..., description="按日计数（日期升序，空日补 0）")


class StatsData(BaseModel):
    """看板统计 data 段。"""

    total_items: int = Field(..., description="条目总数（不含软删）")
    status_distribution: dict[str, int] = Field(..., description="状态分布（draft/published/archived 三键齐全）")
    kb_type_distribution: dict[str, int] = Field(..., description="六库类型分布")
    changes_last_7_days: ChangesLast7Days = Field(..., description="近 7 日变更")
    generated_at: datetime = Field(..., description="统计生成时间（ISO 8601 带时区）")


#: 端点信封别名（FastAPI response_model 消费参数化具体模型）
StatsEnvelope = AdminEnvelope[StatsData]

# --------------------------------------------------------------------------- 端点

router = APIRouter(prefix="/api/v1/admin", tags=["kb-admin"])


@router.get(
    "/stats",
    response_model=StatsEnvelope,
    summary="管理看板统计（条目数/状态分布/近 7 日变更）",
)
async def admin_stats(
    request: Request,
    store: KnowledgeItemStore = Depends(get_item_store),
) -> StatsEnvelope:
    """聚合全量未软删条目：总数、状态/类型分布、近 7 日变更（口径见模块 docstring）。"""
    trace_id = request_trace_id(request)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    total = 0
    by_status: dict[str, int] = {status: 0 for status in ITEM_STATUSES}
    by_kb_type: dict[str, int] = {}
    changed = 0
    by_day: dict[str, int] = {}
    for record in store.iter_all():
        total += 1
        by_status[record.status] = by_status.get(record.status, 0) + 1
        by_kb_type[record.kb_type] = by_kb_type.get(record.kb_type, 0) + 1
        if record.updated_at is not None and record.updated_at >= cutoff:
            changed += 1
            day = record.updated_at.date().isoformat()
            by_day[day] = by_day.get(day, 0) + 1
    # 近 7 日连续刻度（含当日，日期升序），无变更的日期补 0——看板零拼装
    daily = [
        DailyChange(date=(now - timedelta(days=offset)).date().isoformat(), count=by_day.get((now - timedelta(days=offset)).date().isoformat(), 0))
        for offset in range(6, -1, -1)
    ]
    return StatsEnvelope(
        trace_id=trace_id,
        data=StatsData(
            total_items=total,
            status_distribution=by_status,
            kb_type_distribution=by_kb_type,
            changes_last_7_days=ChangesLast7Days(total=changed, by_day=daily),
            generated_at=now,
        ),
    )
