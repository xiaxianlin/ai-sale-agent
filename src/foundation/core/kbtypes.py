# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""六大知识库类型契约（CR A1 修复：跨服务共享枚举下沉 foundation 单点）。

此前 training 经源码级 import 直取 kb.vectorstore.base.KB_TYPES——绕过 FR-KB-030
冻结契约且分容器部署即断裂。收敛至此单点后：kb 保留 re-export（既有引用零改动），
training 引用本模块（依赖方向 foundation→services 合法）。
"""
from __future__ import annotations

#: 六大知识库类型（SRS §5.2 FR-KB-001~006；与向量库标量过滤维度一一对应）
KB_TYPES: tuple[str, ...] = (
    "product",
    "customer",
    "skill",
    "process",
    "negative_case",
    "training",
)
