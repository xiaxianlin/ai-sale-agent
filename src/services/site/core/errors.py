# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""site-service 统一业务错误（04-接口设计 §1.3 错误码体系）。

各 core 子模块异常一律继承 :class:`SiteServiceError`，携带业务错误码；
api 层据此映射 HTTP 状态码与统一信封 {code, message, data, trace_id}。

错误码表（含本服务增补码，均已按 04 §5 走 v1.x 变更登记口径）：
- 0      成功
- 40001  参数校验失败（HTTP 400）
- 40101  visitor token / data-key 无效（HTTP 401）
- 40301  Origin 不在白名单（HTTP 403）
- 40302  consent 未同意，拒绝采集（HTTP 403，本服务增补码，SR-SITE-002）
- 42901  限流（HTTP 429）
- 40401  资源不存在（HTTP 404，本服务增补码，DSR 工单查询用）
- 50002  下游依赖故障（HTTP 503）
"""
from __future__ import annotations

#: 业务错误码 → HTTP 状态码映射（api 层装配用）
CODE_HTTP: dict[int, int] = {
    40001: 400,
    40101: 401,
    40301: 403,
    40302: 403,
    40401: 404,
    42901: 429,
    50002: 503,
}


class SiteServiceError(Exception):
    """site-service 业务错误基类：携带 04 §1.3 业务错误码。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


__all__ = ["CODE_HTTP", "SiteServiceError"]
