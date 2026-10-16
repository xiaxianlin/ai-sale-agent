# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""site-service API 层共享辅助（统一信封 + trace_id 解析，04-接口设计 §1.3/§1.4）。"""
from __future__ import annotations

from foundation.core.envelope import envelope as foundation_envelope

import uuid
from typing import Any

from fastapi import Request


def trace_id_of(request: Request) -> str:
    """解析本请求 trace_id：X-Trace-Id 头优先，缺失新生成（04 §1.4）。"""
    return (request.headers.get("x-trace-id") or "").strip() or uuid.uuid4().hex[:16]


def envelope(request: Request, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（04 §1.3）：{code, message, data, trace_id} 四键齐全，data 允许 None。"""
    return foundation_envelope(code, message, data, trace_id_of(request))  # CR A2：值构造委托 foundation 单点


def ok(request: Request, data: Any) -> dict[str, Any]:
    """成功信封快捷形态（code=0）。"""
    return envelope(request, 0, "ok", data)
