# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""统一响应信封与 trace_id 解析（CR A2 收敛：全仓 22 份拷贝的单点实现）。

四键信封 {code, message, data, trace_id} 契约见 04-接口设计 §1.3——此前五服务
各自演化出 22 份 `_envelope` 拷贝与 4 种 trace_id 格式，本模块收敛为唯一实现；
各服务保留自身函数签名、内部委托此处（轻量收敛：行为不变、测试零改动）。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request

#: trace_id 请求头名（透传优先；缺省生成）
TRACE_HEADER = "X-Trace-Id"


def resolve_trace_id(request: Request | None = None) -> str:
    """解析 trace_id：X-Trace-Id 头透传 → 生成 `trc-` 前缀 16 位十六进制。"""
    if request is not None:
        raw = request.headers.get(TRACE_HEADER, "").strip()
        if raw:
            return raw
    return f"trc-{uuid.uuid4().hex[:16]}"


def envelope(code: int, message: str, data: Any = None, trace_id: str = "") -> dict[str, Any]:
    """统一四键信封（04-接口设计 §1.3；trace_id 缺省由 resolve_trace_id 补齐）。"""
    return {
        "code": code,
        "message": message,
        "data": data,
        "trace_id": trace_id or f"trc-{uuid.uuid4().hex[:16]}",
    }
