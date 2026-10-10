# 知识产权归甲方所有（COM-IP-01）
"""ConfigCenter——配置中心核心实现（NFR-MAI-02 配置外置，P0 起）。

依据：docs/07-实现方案/NFR/SR-NFR-MAI-02-配置外置.md（§2 关键决策 / §5 接口契约 / §6 核心实现）。

本模块是 SR §3 ``services/common/config/center.py`` 的 platform 侧同构实现（仓库迭代 0.1
处于 Phase 0 骨架期，公共基础能力先落 ``platform/core``，服务化拆分后平移共用）。职责：

  1. ``register(schema)``       声明式注册表登记（重复登记拒绝；默认值已在 Schema 构造期校验）；
  2. ``startup_load(values)``   启动装载校验：未注册 key / 非法值 → ConfigBootstrapError，拒绝带病启动；
  3. ``get(key)`` / ``get_typed()``  get<T> 泛型读取：双层解析（租户层 → 默认层 → 注册表默认值）；
  4. ``set(key, value)``        热更新写路径：值校验（类型/范围/枚举/危险项原因）→ 版本递增
                                → 历史留痕 → 订阅回调推送（对应生产形态「事务提交后 publish」）；
  5. ``subscribe()``            热更新订阅接口（ConfigSubscriber 协议，支持 scope/key 过滤）；
  6. ``snapshot()`` / ``rollback_to(version)``  快照与一键回滚（回滚 = 新变更，审计链不断，SR §2 决策⑤）；
  7. ``tenant_override()``      租户覆盖层双层解析（P0-P4 恒默认层 tenant_id=1，P5 零改造启用，SR §2 决策③）。

与生产形态的差异（接口契约一致、存储形态不同）：本实现以进程内存为权威存储与历史来源；
生产环境以 ``app_config`` / ``app_config_history`` 表为持久层、Redis pub/sub（``cfg:pub``）为
跨服务推送通道、60s 兜底对账刷新（SR §4/§6.2），替换存储层时本类接口不变。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

from .schema import ConfigSchema

_logger = logging.getLogger(__name__)

#: 默认层租户 ID：P0-P4 单租户恒为 1（SR-NFR-MAI-02 §2 决策③）；租户覆盖层自 P5 启用
DEFAULT_TENANT_ID: int = 1

# 写校验错误码（与 SR-NFR-MAI-02 §5 REST API 错误码对齐，便于上层网关透传）
CFG_ERR_RANGE: int = 40002    # 超出取值范围（min-max 区间或枚举未命中）
CFG_ERR_TYPE: int = 40902     # 类型与注册表不符
CFG_ERR_CONFIRM: int = 40901  # 危险项未满足二次确认条件（缺变更原因）

_T = TypeVar("_T")

# 层缺失哨兵：与合法值 None 区分（「该租户层未写过」≠「值为 null」）
_MISSING: Any = object()


class ConfigError(Exception):
    """配置中心基础异常（全部子类异常携带中文可读信息，便于日志与面板展示）。"""


class ConfigRegistrationError(ConfigError):
    """注册失败：重复登记同一 scope.key。"""


class ConfigBootstrapError(ConfigError):
    """启动装载/校验失败：存在未注册 key 或非法值——拒绝带病运行（SR §6.2，容器启动失败）。"""


class UnknownConfigKeyError(ConfigError):
    """未注册的配置 key（运行期 fail fast：防拼写错配绕过注册表）。"""


class ConfigValidationError(ConfigError):
    """写校验失败：类型不符 / 超出取值范围 / 危险项缺变更原因（code 对齐 REST 错误码）。"""

    def __init__(self, message: str, *, code: int) -> None:
        super().__init__(message)
        self.code = code


class ConfigRollbackError(ConfigError):
    """回滚失败：目标版本号不在历史范围内。"""


@dataclass(frozen=True)
class ConfigChangeEvent:
    """热更新事件：配置变更后推送给订阅者的通知（生产形态对应 ``cfg:pub`` 通道消息体）。"""

    scope: str
    key: str            # 域内短键名（如 llm.temperature）
    tenant_id: int      # 变更作用的租户层（默认层为 1）
    old_value: Any      # 变更前生效值（读取侧视角，双层解析后的结果）
    new_value: Any      # 变更后生效值
    key_version: int    # 该 (租户, key) 变更后的版本号
    version: int        # 全局变更序号（严格递增，回滚亦占用序号）
    action: str         # 'update'（普通修改）| 'rollback'（回滚）
    operator: str       # 操作人（后台用户 / 'system'）
    reason: str         # 变更原因（危险项必填）

    @property
    def full_key(self) -> str:
        """全限定键 ``scope.key``。"""
        return f"{self.scope}.{self.key}"


class ConfigSubscriber(Protocol):
    """热更新订阅者协议：实现 ``on_config_change`` 即可接收配置变更推送（NFR-MAI-02 热更新）。

    约定：回调应快速返回（耗时操作由实现方自行异步化）；回调内抛出的异常会被隔离记录，
    不影响变更提交与其他订阅者。
    """

    def on_config_change(self, event: ConfigChangeEvent) -> None:
        """收到一次配置变更事件。"""
        ...


@dataclass(frozen=True)
class ConfigChangeRecord:
    """配置变更历史记录（生产形态对应 ``app_config_history`` 表的进程内形态，回滚取数源）。

    old_value/new_value 为「读取侧生效值」（双层解析后的结果，面板 diff 直接可用）；
    raw_new_value/layer_present_after 为「存储层语义」，回滚重放据此精确恢复层状态
    （含「移除租户覆盖层」这类非值写入型变更）。
    """

    version: int                 # 全局变更序号（严格递增）
    action: str                  # 'update' | 'rollback'
    scope: str
    key: str                     # 域内短键名
    tenant_id: int
    old_value: Any               # 变更前生效值
    new_value: Any               # 变更后生效值
    raw_new_value: Any           # 变更后该层存储原值（层被移除时为 None）
    layer_existed_before: bool   # 变更前该租户层是否存在
    layer_present_after: bool    # 变更后该租户层是否存在（False = 移除覆盖、回退默认层）
    key_version: int             # 该 (租户, key) 的新版本号
    operator: str
    reason: str
    at: float                    # 变更时间戳（Unix 秒）

    @property
    def full_key(self) -> str:
        """全限定键 ``scope.key``。"""
        return f"{self.scope}.{self.key}"


@dataclass(frozen=True)
class ConfigSnapshot:
    """配置快照：某一时刻全部层值的只读留档（运维诊断与回滚目标态参照）。"""

    version: int                                 # 快照时点的全局变更序号
    taken_at: float                              # 快照时间戳（Unix 秒）
    values: Mapping[tuple[int, str], Any]        # (tenant_id, full_key) → 层内原值（仅显式写过的层）


@dataclass(frozen=True)
class _Subscription:
    """内部订阅登记：订阅者 + 过滤条件（scope/key 均为 None 表示订阅全量变更）。"""

    subscriber: ConfigSubscriber
    scope: str | None = None
    key: str | None = None       # 域内短键名（也接受全限定 scope.key 形式）

    def matches(self, event: ConfigChangeEvent) -> bool:
        """判断事件是否命中订阅过滤条件。"""
        if self.scope is not None and event.scope != self.scope:
            return False
        if self.key is not None and event.key != self.key and event.full_key != self.key:
            return False
        return True


class ConfigCenter:
    """配置中心：声明式注册表驱动的配置读取/热更新/回滚/租户双层解析（NFR-MAI-02）。

    线程安全说明：全部公开方法可并发调用；读写以 RLock 串行化，订阅回调在锁外派发，
    回调内可以安全地再次读写配置（不会死锁）。
    """

    def __init__(self) -> None:
        self._schemas: dict[str, ConfigSchema] = {}                 # full_key → Schema（注册表）
        self._values: dict[tuple[int, str], Any] = {}               # (tenant_id, full_key) → 层内原值
        self._key_versions: dict[tuple[int, str], int] = {}         # (tenant_id, full_key) → 该 key 版本
        self._subscribers: list[_Subscription] = []                 # 热更新订阅列表
        self._history: list[ConfigChangeRecord] = []                # 变更历史（按全局序号升序追加）
        self._version: int = 0                                      # 全局变更序号（严格递增）
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 注册表

    def register(self, schema: ConfigSchema) -> None:
        """声明式登记一个配置项（各业务模块启动期 import 时执行）。

        - 重复登记同一 ``scope.key`` → ConfigRegistrationError（注册表唯一事实来源，防多处漂移）；
        - 默认值合法性已在 ConfigSchema 构造期校验（非法默认值根本无法构造）。
        """
        full_key = schema.full_key
        with self._lock:
            if full_key in self._schemas:
                raise ConfigRegistrationError(f"重复注册: {full_key}")
            self._schemas[full_key] = schema

    def registered_keys(self) -> list[str]:
        """已注册的全部全限定键（按字典序，供面板与启动校验报告使用）。"""
        with self._lock:
            return sorted(self._schemas)

    def _schema_for(self, key: str) -> ConfigSchema:
        """按键取注册表 Schema；未注册 → UnknownConfigKeyError（fail fast，SR §6.2 口径）。"""
        schema = self._schemas.get(key)
        if schema is None:
            raise UnknownConfigKeyError(f"未注册的配置项: {key}（先在注册表登记 Schema）")
        return schema

    # ------------------------------------------------------------------ 启动装载校验

    def startup_load(
        self,
        values: Mapping[str, Any],
        *,
        tenant_id: int = DEFAULT_TENANT_ID,
        service_name: str = "unknown",
    ) -> None:
        """服务启动装载与校验（SR §6.2 ``startup_load`` 的进程内形态）。

        ``values`` 模拟外部配置源（生产环境为 ``app_config`` 表全量行：full_key → 值）：

        - 校验一（防拼写）：存在未注册 key → ConfigBootstrapError（可能是代码删项未清数据或拼写错）；
        - 校验二（防脏值）：值类型/范围不符 → ConfigBootstrapError（附逐项明细）；
        - 全部通过后一次性灌入对应租户层缓存（装载非变更：不产生历史记录、不触发订阅回调；
          已存在的层不覆盖，保证重启/重放幂等，对应 SR §6.2 ``ON CONFLICT DO NOTHING`` 语义）。

        ``service_name`` 仅用于错误信息定位（生产形态中另承担心跳注册与订阅协程启动，本形态不涉及）。
        """
        with self._lock:
            # 校验一：未注册 key（孤儿键）整体拒收
            orphans = sorted(k for k in values if k not in self._schemas)
            if orphans:
                raise ConfigBootstrapError(
                    f"[{service_name}] 启动校验失败：未注册的配置项（先登记 Schema 或清理数据）: {orphans}"
                )
            # 校验二：逐项校验值合法性（与写路径共用 Schema 同一校验函数）
            problems: list[str] = []
            for key, value in values.items():
                schema = self._schemas[key]
                if not schema.check_type(value):
                    problems.append(f"{key}: 类型期望 {schema.vtype}，实为 {type(value).__name__}")
                elif not schema.check_value(value):
                    problems.append(f"{key}: 超出取值范围 {value!r}")
            if problems:
                raise ConfigBootstrapError(f"[{service_name}] 启动校验失败：{'；'.join(problems)}")
            # 全部通过 → 灌入缓存（幂等：仅首次铺底）
            for key, value in values.items():
                ck = (tenant_id, key)
                if ck not in self._values:
                    self._values[ck] = deepcopy(value)
                    self._key_versions[ck] = max(self._key_versions.get(ck, 0), 1)

    # ------------------------------------------------------------------ 读取（双层解析）

    def get(self, key: str, *, tenant_id: int = DEFAULT_TENANT_ID) -> Any:
        """读取配置值：未注册 key 拒绝；已注册 key 永不缺省（最坏回退注册表默认值）。

        解析顺序（SR §6.3 双层解析）：租户覆盖层 → 默认层（tenant_id=1）→ 注册表默认值。
        json 类型（list/dict）按值返回深拷贝，防止调用方改写共享默认值对象。
        """
        schema = self._schema_for(key)  # 未注册 key → 异常（fail fast）
        with self._lock:
            return self._copy_if_mutable(self._resolve(tenant_id, schema.full_key))

    def get_typed(self, tp: type[_T], key: str, *, tenant_id: int = DEFAULT_TENANT_ID) -> _T:
        """get<T> 泛型读取：按调用方期望类型取值，实际类型不符即抛 ConfigValidationError(40902)。

        泛型参数由调用方静态标注（``center.get_typed(int, "risk.limits.max_ops_per_day")``），
        运行期再做一次防御性核对，杜绝「注册表改型、消费方未跟改」的静默错值。
        """
        value = self.get(key, tenant_id=tenant_id)
        if not self._type_matches(tp, value):
            raise ConfigValidationError(
                f"类型不符: {key} 期望 {tp.__name__}，实为 {type(value).__name__}", code=CFG_ERR_TYPE
            )
        return value

    def tenant_override(self, tenant_id: int, key: str) -> Any:
        """租户双层解析入口（P5 预留，SR §6.3）：租户覆盖值 → 默认层回退。

        P0-P4 恒传 tenant_id=1（走默认层）；P5 新租户仅需 ``set(..., tenant_id=N)`` 写入
        覆盖值，本读取路径零改造（NFR-SCA-02、FR-SAAS-001/002）。
        """
        return self.get(key, tenant_id=tenant_id)

    def get_version(self, key: str, *, tenant_id: int = DEFAULT_TENANT_ID) -> int:
        """读取该 (租户, key) 的当前版本号（从未写过为 0；每次 set/回滚 +1，对应 app_config.version）。"""
        schema = self._schema_for(key)
        with self._lock:
            return self._key_versions.get((tenant_id, schema.full_key), 0)

    def _resolve(self, tenant_id: int, full_key: str) -> Any:
        """双层解析核心（须持锁调用）：租户层 → 默认层(tenant_id=1) → 注册表默认值，永不抛缺省异常。"""
        raw = self._values.get((tenant_id, full_key), _MISSING)
        if raw is not _MISSING:
            return raw
        if tenant_id != DEFAULT_TENANT_ID:
            raw = self._values.get((DEFAULT_TENANT_ID, full_key), _MISSING)
            if raw is not _MISSING:
                return raw
        return self._schemas[full_key].default

    @staticmethod
    def _copy_if_mutable(value: _T) -> _T:
        """json 类型（list/dict）深拷贝返回，防止调用方改写缓存或注册表共享默认值。"""
        return deepcopy(value) if isinstance(value, (list, dict)) else value

    @staticmethod
    def _type_matches(tp: type, value: Any) -> bool:
        """运行期类型核对（bool/int 子类陷阱与 Schema.check_type 同规则）。"""
        if tp is bool:
            return isinstance(value, bool)
        if tp is int:
            return isinstance(value, int) and not isinstance(value, bool)
        if tp is float:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, tp)

    # ------------------------------------------------------------------ 写路径（热更新）

    def set(
        self,
        key: str,
        value: Any,
        *,
        tenant_id: int = DEFAULT_TENANT_ID,
        operator: str = "system",
        reason: str = "",
    ) -> ConfigChangeRecord:
        """修改配置（热更新主入口）：校验 → 版本递增 → 历史留痕 → 订阅回调推送。

        校验失败即拒绝且不产生任何副作用（对应 SR §6.4 的 40002/40902/40901 分支）：
          - 类型与注册表不符 → ConfigValidationError(40902)；
          - 超出取值范围/枚举未命中 → ConfigValidationError(40002)；
          - 危险项（dangerous=True）未填写变更原因 → ConfigValidationError(40901)
            （生产形态另要求一次性 confirm_token，本进程内形态以 reason 必填为等价门禁）。

        返回本次变更记录（含全局序号与该 key 新版本），回滚据此留痕。
        """
        schema = self._schema_for(key)  # 未注册 key → 异常（fail fast）
        self._check_writable(schema, value, reason)
        with self._lock:
            record = self._apply(
                (tenant_id, schema.full_key),
                schema=schema,
                new_raw=value,
                action="update",
                operator=operator,
                reason=reason,
            )
            subscribers = list(self._subscribers)
        self._notify(record, subscribers)  # 锁外推送：对应生产「事务提交后才 publish」（SR §6.4）
        return record

    def set_tenant_override(
        self,
        tenant_id: int,
        key: str,
        value: Any,
        *,
        operator: str = "system",
        reason: str = "",
    ) -> ConfigChangeRecord:
        """写入租户覆盖层（P5）：语义等价 ``set(key, value, tenant_id=N)``，显式命名便于阅读。"""
        if tenant_id == DEFAULT_TENANT_ID:
            _logger.warning("租户覆盖层写入 tenant_id=%s 即默认层（P0-P4 单租户口径）", tenant_id)
        return self.set(key, value, tenant_id=tenant_id, operator=operator, reason=reason)

    @staticmethod
    def _check_writable(schema: ConfigSchema, value: Any, reason: str) -> None:
        """写前校验（类型 → 范围/枚举 → 危险项原因），错误码对齐 SR §5。"""
        if not schema.check_type(value):
            raise ConfigValidationError(
                f"类型与注册表不符: {schema.full_key} 期望 {schema.vtype}，实为 {type(value).__name__}",
                code=CFG_ERR_TYPE,
            )
        if not schema.check_value(value):
            raise ConfigValidationError(
                f"超出取值范围: {schema.full_key} = {value!r}", code=CFG_ERR_RANGE
            )
        if schema.dangerous and not reason.strip():
            raise ConfigValidationError(
                f"危险项修改必须填写变更原因: {schema.full_key}（{schema.desc}）", code=CFG_ERR_CONFIRM
            )

    def _apply(
        self,
        cache_key: tuple[int, str],
        *,
        schema: ConfigSchema,
        new_raw: Any,
        action: str,
        operator: str,
        reason: str,
    ) -> ConfigChangeRecord:
        """底层变更应用（须持锁调用）：写层/移除层、双版本递增、追加历史；不触发回调。

        ``new_raw`` 传 ``_MISSING`` 表示移除该租户层（回滚场景：覆盖层恢复为「未写」状态，
        读取侧自动回退默认层——与生产 DELETE 覆盖行语义一致）。
        """
        tenant_id, full_key = cache_key
        layer_existed = cache_key in self._values
        old_effective = self._copy_if_mutable(self._resolve(tenant_id, full_key))  # 变更前生效值
        self._version += 1
        key_version = self._key_versions.get(cache_key, 0) + 1
        self._key_versions[cache_key] = key_version
        if new_raw is _MISSING:
            self._values.pop(cache_key, None)
        else:
            self._values[cache_key] = deepcopy(new_raw)
        new_effective = self._copy_if_mutable(self._resolve(tenant_id, full_key))  # 变更后生效值
        record = ConfigChangeRecord(
            version=self._version,
            action=action,
            scope=schema.scope,
            key=schema.key,
            tenant_id=tenant_id,
            old_value=old_effective,
            new_value=new_effective,
            raw_new_value=None if new_raw is _MISSING else deepcopy(new_raw),
            layer_existed_before=layer_existed,
            layer_present_after=new_raw is not _MISSING,
            key_version=key_version,
            operator=operator,
            reason=reason,
            at=time.time(),
        )
        self._history.append(record)
        return record

    # ------------------------------------------------------------------ 订阅（热更新推送）

    def subscribe(
        self,
        subscriber: ConfigSubscriber,
        *,
        scope: str | None = None,
        key: str | None = None,
    ) -> Callable[[], None]:
        """订阅配置变更（热更新回调注册），返回取消订阅函数。

        - ``scope``/``key`` 可选过滤（key 接受域内短键名或全限定 ``scope.key``）；均缺省订阅全量；
        - 事件在变更生效后同步派发（锁外），对应生产形态 Redis pub/sub 的进程内等价物；
        - 同一订阅者可多次登记（如需同时订阅多个 scope），取消函数逐笔注销。
        """
        sub = _Subscription(subscriber=subscriber, scope=scope, key=key)
        with self._lock:
            self._subscribers.append(sub)

        def unsubscribe() -> None:
            """注销该笔订阅（幂等）。"""
            with self._lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

        return unsubscribe

    def _notify(self, record: ConfigChangeRecord, subscribers: list[_Subscription]) -> None:
        """将变更事件派发给命中过滤条件的订阅者（锁外调用，回调内可安全再读写配置）。

        回调异常隔离：单个订阅者抛异常只记日志（含堆栈），不阻断其他订阅者与变更提交
        ——配置写入已生效，回调属于「通知尽力而为」（生产形态 ACK 通道另有超时告警兜底）。
        """
        event = ConfigChangeEvent(
            scope=record.scope,
            key=record.key,
            tenant_id=record.tenant_id,
            old_value=record.old_value,
            new_value=record.new_value,
            key_version=record.key_version,
            version=record.version,
            action=record.action,
            operator=record.operator,
            reason=record.reason,
        )
        for sub in subscribers:
            if not sub.matches(event):
                continue
            try:
                sub.subscriber.on_config_change(event)
            except Exception:  # noqa: BLE001 —— 有意隔离：异常带堆栈记日志，非静默吞异常
                _logger.exception(
                    "配置变更回调执行异常: %s action=%s version=%s", event.full_key, event.action, event.version
                )

    # ------------------------------------------------------------------ 历史 / 快照 / 回滚

    def history(
        self,
        *,
        scope: str | None = None,
        key: str | None = None,
        tenant_id: int | None = None,
    ) -> list[ConfigChangeRecord]:
        """变更历史查询（含回滚记录，按全局序号升序；对应 ``app_config_history`` 面板数据源）。"""
        with self._lock:
            records = list(self._history)
        if scope is not None:
            records = [r for r in records if r.scope == scope]
        if key is not None:
            records = [r for r in records if r.key == key or r.full_key == key]
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        return records

    def snapshot(self) -> ConfigSnapshot:
        """拍取当前全量快照：全局序号 + 全部层值只读留档（幂等，不产生历史记录）。"""
        with self._lock:
            return ConfigSnapshot(
                version=self._version,
                taken_at=time.time(),
                values=MappingProxyType({k: deepcopy(v) for k, v in self._values.items()}),
            )

    def rollback_to(
        self,
        version: int,
        *,
        operator: str = "system",
        reason: str = "",
    ) -> list[ConfigChangeRecord]:
        """一键回滚到全局变更序号 ``version`` 时点的全部配置状态（SR §2 决策⑤：回滚 = 新变更）。

        语义：
          - 目标序号必须是合法历史序号（0 ≤ version ≤ 当前序号），否则 ConfigRollbackError；
          - 仅恢复 ``version`` 之后被变更过的 (租户, key)：值改回当时值；当时不存在层的
            （如后续新增的租户覆盖）移除层、读取侧回退默认层；
          - 每笔恢复产生一条 ``action='rollback'`` 新历史（旧版本号向后递增，审计链不断），
            并照常触发热更新订阅回调；
          - 幂等：目标态与当前态一致时返回空列表，不产生任何变更。

        返回本次实际产生的回滚记录列表（按 (租户, 键) 字典序，保证行为可复现）。
        """
        with self._lock:
            if not 0 <= version <= self._version:
                raise ConfigRollbackError(
                    f"回滚目标版本不存在: version={version}（合法范围 0..{self._version}）"
                )
            later = [r for r in self._history if r.version > version]
            if not later:
                return []  # 目标即当前态：幂等空回滚
            # 重放 version（含）之前的全部历史，得到目标层状态（层是否存在 + 层内原值）
            target: dict[tuple[int, str], tuple[bool, Any]] = {}
            for r in self._history:
                if r.version > version:
                    break  # history 按序号升序追加，可提前终止
                target[(r.tenant_id, r.full_key)] = (r.layer_present_after, r.raw_new_value)
            touched = {(r.tenant_id, r.full_key) for r in later}
            records: list[ConfigChangeRecord] = []
            for cache_key in sorted(touched):
                tenant_id, full_key = cache_key
                present_now = cache_key in self._values
                raw_now = self._values.get(cache_key, _MISSING)
                want_present, want_raw = target.get(cache_key, (False, _MISSING))
                # 层状态与层内值均已与目标一致 → 无需恢复
                if present_now == want_present and (not want_present or raw_now == want_raw):
                    continue
                records.append(
                    self._apply(
                        cache_key,
                        schema=self._schemas[full_key],
                        new_raw=want_raw if want_present else _MISSING,
                        action="rollback",
                        operator=operator,
                        reason=reason,
                    )
                )
            subscribers = list(self._subscribers)
        for record in records:
            self._notify(record, subscribers)  # 锁外推送回滚事件（与 set 同通道同语义）
        return records
