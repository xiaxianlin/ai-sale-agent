# 知识产权归甲方所有（COM-IP-01）
"""platform.core.config——配置中心对外唯一入口（NFR-MAI-02 配置外置）。

依据：docs/07-实现方案/NFR/SR-NFR-MAI-02-配置外置.md。
业务代码统一从本包导入（不直接引用 center/schema 子模块，便于后续服务化平移）。

典型用法::

    from platform.core.config import ConfigSchema, cfg

    # 声明式登记（各业务模块启动期执行；默认值/取值范围/中文说明一并在注册表声明）
    cfg.register(ConfigSchema(
        scope="risk", key="limits.max_ops_per_day", vtype="int",
        default=150, minimum=30, maximum=400,
        desc="单账号每日操作量上限", dangerous=True,
    ))

    # 读取（双层解析：租户层 → 默认层 → 注册表默认值；未注册 key 直接抛错）
    limit = cfg.get("risk.limits.max_ops_per_day")

    # 热更新（值校验 + 版本递增 + 历史留痕 + 订阅回调推送）
    cfg.set("risk.limits.max_ops_per_day", 120, operator="admin", reason="大促前收紧")
"""

from .center import (
    CFG_ERR_CONFIRM,
    CFG_ERR_RANGE,
    CFG_ERR_TYPE,
    DEFAULT_TENANT_ID,
    ConfigBootstrapError,
    ConfigCenter,
    ConfigChangeRecord,
    ConfigChangeEvent,
    ConfigError,
    ConfigRegistrationError,
    ConfigRollbackError,
    ConfigSnapshot,
    ConfigSubscriber,
    UnknownConfigKeyError,
    ConfigValidationError,
)
from .schema import ConfigSchema, ValueType

__all__ = [
    "CFG_ERR_CONFIRM",
    "CFG_ERR_RANGE",
    "CFG_ERR_TYPE",
    "DEFAULT_TENANT_ID",
    "ConfigBootstrapError",
    "ConfigCenter",
    "ConfigChangeRecord",
    "ConfigChangeEvent",
    "ConfigError",
    "ConfigRegistrationError",
    "ConfigRollbackError",
    "ConfigSnapshot",
    "ConfigSubscriber",
    "UnknownConfigKeyError",
    "ConfigValidationError",
    "ConfigSchema",
    "ValueType",
    "cfg",
]

#: 进程级默认配置中心单例（业务代码统一经此读取；测试用例请自行 new ConfigCenter 以隔离状态）
cfg = ConfigCenter()
