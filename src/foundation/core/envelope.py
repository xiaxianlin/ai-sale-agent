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


def error_envelope(code: int, message: str, request: Request | None = None,
                   data: Any = None) -> dict[str, Any]:
    """错误信封（CR 遗留重构·i18n 接线）：在统一信封上自动附带用户可行动文案。

    - message 保持技术口径（调用方/日志可读）；
    - data.hints 来自 foundation.i18n 文案中心（title=用户人话 + action=下一步建议，
      SR-KB-023 §6.3 口径），i18n 无词条的错误码不附 hints（零破坏）。
    """
    payload = envelope(code, message, data, resolve_trace_id(request))
    try:
        from foundation.i18n.messages import message as i18n_lookup

        hints = i18n_lookup(code)
        if hints is not None:
            base = dict(payload["data"]) if isinstance(payload["data"], dict) else {}
            base["hints"] = {"title": hints.title, "action": hints.action}
            payload["data"] = base
    except Exception:  # noqa: BLE001 —— 文案中心不可用不阻断错误响应本身
        pass
    return payload
