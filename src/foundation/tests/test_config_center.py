# 知识产权归甲方所有（COM-IP-01）
"""ConfigCenter 单元测试（NFR-MAI-02 配置外置）。

依据：docs/07-实现方案/NFR/SR-NFR-MAI-02-配置外置.md §9（测试要点）。
覆盖：注册与默认值取值、未注册 key 拒绝（启动校验 + 运行期 fail fast）、越界/类型/枚举
拒绝（错误码对齐 REST 40002/40902/40901）、危险项变更原因强制、热更新订阅回调、
版本与历史留痕、快照与一键回滚（含租户覆盖层移除）、租户双层解析（P5 预留）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from foundation.config import (
    CFG_ERR_CONFIRM,
    CFG_ERR_RANGE,
    CFG_ERR_TYPE,
    ConfigBootstrapError,
    ConfigCenter,
    ConfigChangeEvent,
    ConfigRegistrationError,
    ConfigRollbackError,
    ConfigSchema,
    UnknownConfigKeyError,
    ConfigValidationError,
)

_UNSUBSCRIBE = Callable[[], None]


class _Recorder:
    """测试用订阅者：按到达顺序记录收到的事件（含异常计数场景）。"""

    def __init__(self) -> None:
        self.events: list[ConfigChangeEvent] = []

    def on_config_change(self, event: ConfigChangeEvent) -> None:
        """实现 ConfigSubscriber 协议：仅记录事件。"""
        self.events.append(event)


class _CrashingSubscriber:
    """测试用异常订阅者：验证单个回调异常不阻断变更提交与其他订阅者。"""

    def __init__(self) -> None:
        self.raised: bool = False

    def on_config_change(self, event: ConfigChangeEvent) -> None:
        """实现 ConfigSubscriber 协议：总是抛异常。"""
        self.raised = True
        raise RuntimeError("订阅者回调故意崩溃（隔离性测试）")


@pytest.fixture()
def center() -> ConfigCenter:
    """每个用例独立的配置中心：预登记覆盖 int/float/bool/str/json 五种类型的示例配置。

    示例登记与 SR-NFR-MAI-02 §6.1（risk/model/strategy 示例）及
    SR-NFR-SEC-05 §7（security.tls.* 键）保持同一口径。
    """
    c = ConfigCenter()
    c.register(ConfigSchema(
        scope="risk", key="limits.max_ops_per_day", vtype="int",
        default=150, minimum=30, maximum=400,
        desc="单账号每日操作量上限（超限排队次日）", dangerous=True,
    ))
    c.register(ConfigSchema(
        scope="model", key="llm.temperature", vtype="float",
        default=0.3, minimum=0.0, maximum=2.0, desc="回复生成温度",
    ))
    c.register(ConfigSchema(
        scope="model", key="llm.stream", vtype="bool", default=True, desc="是否流式输出",
    ))
    c.register(ConfigSchema(
        scope="strategy", key="followup.cadence", vtype="json",
        default=[1, 3, 7, 14], desc="跟进节奏间隔天数序列",
    ))
    c.register(ConfigSchema(
        scope="security", key="tls.renew_alert_days", vtype="int",
        default=14, minimum=7, maximum=30, desc="证书到期兜底告警提前天数",
    ))
    c.register(ConfigSchema(
        scope="common", key="ui.lang", vtype="str",
        default="zh-CN", choices=("zh-CN", "en-US"), desc="界面语言",
    ))
    return c


# ---------------------------------------------------------------- 注册与取值


def test_register_and_get_default(center: ConfigCenter) -> None:
    """登记后未修改时，get 返回注册表默认值（int/float/bool/str/json 五类型全覆盖）。"""
    assert center.get("risk.limits.max_ops_per_day") == 150
    assert center.get("model.llm.temperature") == pytest.approx(0.3)
    assert center.get("model.llm.stream") is True
    assert center.get("common.ui.lang") == "zh-CN"
    assert center.get("strategy.followup.cadence") == [1, 3, 7, 14]
    # 注册表清单可枚举（供启动校验报告与面板使用）
    assert "risk.limits.max_ops_per_day" in center.registered_keys()


def test_register_duplicate_rejected(center: ConfigCenter) -> None:
    """重复登记同一 scope.key → ConfigRegistrationError（注册表唯一事实来源）。"""
    with pytest.raises(ConfigRegistrationError, match="重复注册"):
        center.register(ConfigSchema(
            scope="risk", key="limits.max_ops_per_day", vtype="int",
            default=100, minimum=30, maximum=400, desc="重复项",
        ))


def test_schema_default_must_be_valid() -> None:
    """默认值不过自身校验 → Schema 构造即失败（登记期拦截，防带病注册）。"""
    with pytest.raises(ValidationError, match="默认值非法"):
        ConfigSchema(scope="risk", key="x.bad_default", vtype="int",
                     default=9999, minimum=30, maximum=400, desc="非法默认值")
    # 范围颠倒同样在构造期拒绝
    with pytest.raises(ValidationError, match="取值范围颠倒"):
        ConfigSchema(scope="risk", key="x.bad_range", vtype="int",
                     default=10, minimum=400, maximum=30, desc="区间颠倒")


# ---------------------------------------------------------------- 未注册 key 拒绝


def test_get_unregistered_key_rejected(center: ConfigCenter) -> None:
    """运行期读取未注册 key → UnknownConfigKeyError（fail fast，防拼写错配）。"""
    with pytest.raises(UnknownConfigKeyError, match="未注册"):
        center.get("risk.not.exists")


def test_set_unregistered_key_rejected(center: ConfigCenter) -> None:
    """运行期写入未注册 key → UnknownConfigKeyError，且不产生任何副作用。"""
    with pytest.raises(UnknownConfigKeyError):
        center.set("typo.llm.temperature", 0.5)
    assert center.history() == []


def test_startup_load_rejects_unregistered(center: ConfigCenter) -> None:
    """启动装载校验（SR §6.2 校验一）：外部配置含未注册 key → ConfigBootstrapError 拒绝启动。"""
    with pytest.raises(ConfigBootstrapError, match="未注册的配置项"):
        center.startup_load(
            {"risk.limits.max_ops_per_day": 120, "typo.llm.temp": 0.5},
            service_name="agent-runner",
        )
    # 拒绝后缓存未被部分污染（整体原子拒收）
    assert center.get("risk.limits.max_ops_per_day") == 150


def test_startup_load_rejects_bad_value(center: ConfigCenter) -> None:
    """启动装载校验（SR §6.2 校验二）：值超出取值范围 → ConfigBootstrapError。"""
    with pytest.raises(ConfigBootstrapError, match="超出取值范围"):
        center.startup_load({"security.tls.renew_alert_days": 99})


def test_startup_load_seeds_values(center: ConfigCenter) -> None:
    """启动装载合法配置：铺底进缓存且幂等（重复装载不覆盖已写入层）。"""
    center.startup_load({"model.llm.temperature": 0.7}, service_name="kb-service")
    assert center.get("model.llm.temperature") == pytest.approx(0.7)
    center.startup_load({"model.llm.temperature": 1.9})  # 已存在 → 不覆盖（ON CONFLICT 语义）
    assert center.get("model.llm.temperature") == pytest.approx(0.7)


# ---------------------------------------------------------------- 写校验（越界/类型/枚举/危险项）


def test_set_out_of_range_rejected(center: ConfigCenter) -> None:
    """越界值拒绝（SR §9 test_put_range_rejected 对应）：改 9999 → 40002，值与缓存均不变。"""
    with pytest.raises(ConfigValidationError) as exc_info:
        center.set("risk.limits.max_ops_per_day", 9999, reason="压测放宽")
    assert exc_info.value.code == CFG_ERR_RANGE
    assert center.get("risk.limits.max_ops_per_day") == 150
    assert center.get_version("risk.limits.max_ops_per_day") == 0
    assert center.history() == []  # 拒绝的写入不进历史


def test_set_type_mismatch_rejected(center: ConfigCenter) -> None:
    """类型不符拒绝（40902）：字符串温度、bool 冒充 int 均拒绝。"""
    with pytest.raises(ConfigValidationError) as exc_info:
        center.set("model.llm.temperature", "hot")
    assert exc_info.value.code == CFG_ERR_TYPE
    # bool 是 int 子类，但不是合法 int 配置值（防 True 被当 1 放行）
    with pytest.raises(ConfigValidationError):
        center.set("risk.limits.max_ops_per_day", True, reason="类型陷阱")
    assert center.get("model.llm.temperature") == pytest.approx(0.3)


def test_set_choices_rejected(center: ConfigCenter) -> None:
    """枚举约束：非枚举值 → 40002 拒绝。"""
    with pytest.raises(ConfigValidationError) as exc_info:
        center.set("common.ui.lang", "fr-FR")
    assert exc_info.value.code == CFG_ERR_RANGE
    assert center.get("common.ui.lang") == "zh-CN"


def test_set_dangerous_needs_reason(center: ConfigCenter) -> None:
    """危险项二次确认门禁（SR §9 test_put_dangerous_needs_confirm）：缺变更原因 → 40901。"""
    with pytest.raises(ConfigValidationError) as exc_info:
        center.set("risk.limits.max_ops_per_day", 120)
    assert exc_info.value.code == CFG_ERR_CONFIRM
    # 填写原因后放行
    record = center.set("risk.limits.max_ops_per_day", 120, operator="admin", reason="大促前收紧")
    assert center.get("risk.limits.max_ops_per_day") == 120
    assert record.operator == "admin" and record.reason == "大促前收紧"


# ---------------------------------------------------------------- 版本 / 历史 / 热更新回调


def test_set_bumps_version_and_history(center: ConfigCenter) -> None:
    """合法写入：该 key 版本 +1、历史追加一条 update 记录（旧值→新值留痕）。"""
    v0 = center.get_version("model.llm.temperature")
    center.set("model.llm.temperature", 0.5, operator="dev", reason="调优")
    assert center.get_version("model.llm.temperature") == v0 + 1
    records = center.history(key="llm.temperature")
    assert len(records) == 1
    assert records[0].action == "update"
    assert records[0].old_value == pytest.approx(0.3)
    assert records[0].new_value == pytest.approx(0.5)
    assert records[0].tenant_id == 1  # 默认层


def test_hot_update_callback(center: ConfigCenter) -> None:
    """热更新订阅回调：set 后订阅者收到事件（旧值/新值/动作/操作人齐全）。"""
    recorder = _Recorder()
    unsubscribe: _UNSUBSCRIBE = center.subscribe(recorder)
    center.set("model.llm.temperature", 0.9, operator="dev", reason="热更新验证")
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.full_key == "model.llm.temperature"
    assert event.old_value == pytest.approx(0.3)
    assert event.new_value == pytest.approx(0.9)
    assert event.action == "update" and event.operator == "dev"
    # 取消订阅后不再收到推送
    unsubscribe()
    unsubscribe()  # 幂等注销
    center.set("model.llm.temperature", 1.1)
    assert len(recorder.events) == 1


def test_subscribe_scope_and_key_filter(center: ConfigCenter) -> None:
    """订阅过滤：scope 过滤只收同域事件；key 过滤只收同键事件（短键/全限定键均可）。"""
    scope_rec, key_rec = _Recorder(), _Recorder()
    center.subscribe(scope_rec, scope="model")
    center.subscribe(key_rec, key="security.tls.renew_alert_days")
    center.set("model.llm.temperature", 0.9, reason="t1")
    center.set("security.tls.renew_alert_days", 21, reason="t2")
    center.set("common.ui.lang", "en-US", reason="t3")
    assert [e.full_key for e in scope_rec.events] == ["model.llm.temperature"]
    assert [e.full_key for e in key_rec.events] == ["security.tls.renew_alert_days"]


def test_callback_exception_isolated(center: ConfigCenter) -> None:
    """回调隔离：单个订阅者抛异常不影响变更生效与其他订阅者（变更已提交，通知尽力而为）。"""
    crashing, normal = _CrashingSubscriber(), _Recorder()
    center.subscribe(crashing)
    center.subscribe(normal)
    center.set("model.llm.temperature", 1.2, reason="隔离测试")
    assert crashing.raised
    assert center.get("model.llm.temperature") == pytest.approx(1.2)  # 写入已生效
    assert len(normal.events) == 1  # 其他订阅者照常收到


# ---------------------------------------------------------------- 快照与回滚


def test_rollback_to_restores_value(center: ConfigCenter) -> None:
    """回滚主链路（SR §9 test_rollback_chain 对应）：改错 → 回滚 → 值恢复、历史与回调完整。"""
    center.set("model.llm.temperature", 0.5, operator="a", reason="第一次调优")
    target_version = center.snapshot().version
    center.set("model.llm.temperature", 1.5, operator="b", reason="改错")
    assert center.get("model.llm.temperature") == pytest.approx(1.5)

    recorder = _Recorder()
    center.subscribe(recorder, key="llm.temperature")
    rolled = center.rollback_to(target_version, operator="admin", reason="一键恢复")
    # 值恢复到目标时点，且回滚产生了新版本（回滚 = 新变更，版本继续递增）
    assert center.get("model.llm.temperature") == pytest.approx(0.5)
    assert rolled and rolled[0].action == "rollback"
    assert any(e.action == "rollback" and e.new_value == pytest.approx(0.5) for e in recorder.events)
    actions = [r.action for r in center.history(key="llm.temperature")]
    assert actions == ["update", "update", "rollback"]  # 审计链不断：update+update+rollback


def test_rollback_multiple_keys_and_selective(center: ConfigCenter) -> None:
    """回滚只动目标时点之后变更过的 key；目标时点之前的既有变更保持不变。"""
    center.set("model.llm.temperature", 0.5, reason="早期保留")   # v1：保留
    keep_version = center.snapshot().version
    center.set("common.ui.lang", "en-US", reason="后改")          # v2：回滚对象
    center.set("security.tls.renew_alert_days", 30, reason="后改")  # v3：回滚对象
    rolled = center.rollback_to(keep_version, operator="admin", reason="批量回滚")
    assert {r.full_key for r in rolled} == {"common.ui.lang", "security.tls.renew_alert_days"}
    assert center.get("model.llm.temperature") == pytest.approx(0.5)      # 早期变更保留
    assert center.get("common.ui.lang") == "zh-CN"                        # 恢复默认层旧值
    assert center.get("security.tls.renew_alert_days") == 14


def test_rollback_invalid_version_rejected(center: ConfigCenter) -> None:
    """非法版本号回滚 → ConfigRollbackError。"""
    with pytest.raises(ConfigRollbackError, match="不存在"):
        center.rollback_to(99)


def test_rollback_to_current_is_noop(center: ConfigCenter) -> None:
    """回滚到当前态：幂等空回滚，不产生新历史与事件。"""
    center.set("model.llm.temperature", 0.8, reason="t")
    before = center.history()
    assert center.rollback_to(center.snapshot().version) == []
    assert center.history() == before


def test_rollback_removes_tenant_override_layer(center: ConfigCenter) -> None:
    """回滚覆盖层新增：租户层恢复为「未写」，读取侧回退默认层（对应生产 DELETE 覆盖行语义）。"""
    base_version = center.snapshot().version
    center.set_tenant_override(7, "common.ui.lang", "en-US", operator="ops", reason="租户要求英文")
    assert center.tenant_override(7, "common.ui.lang") == "en-US"
    rolled = center.rollback_to(base_version, operator="admin", reason="清退覆盖")
    assert rolled and rolled[0].layer_present_after is False
    assert center.tenant_override(7, "common.ui.lang") == "zh-CN"  # 覆盖层已移除，回退默认


# ---------------------------------------------------------------- 租户覆盖双层解析（P5 预留）


def test_tenant_override_two_layer_resolution(center: ConfigCenter) -> None:
    """双层解析（SR §6.3）：租户覆盖值优先；未覆盖租户与默认层不受影响。"""
    # 默认层调整（P0-P4 的常态写法：tenant_id 恒为 1）
    center.set("strategy.followup.cadence", [1, 7], operator="ops", reason="默认层调整")
    # 租户 7 写覆盖层（P5 形态，读取路径零改造）
    center.set_tenant_override(7, "strategy.followup.cadence", [2, 5, 10], operator="ops", reason="大客户差异化")
    assert center.tenant_override(7, "strategy.followup.cadence") == [2, 5, 10]  # 覆盖值生效
    assert center.tenant_override(8, "strategy.followup.cadence") == [1, 7]      # 未覆盖租户回退默认层
    assert center.get("strategy.followup.cadence") == [1, 7]                     # 默认层不受覆盖影响


def test_tenant_override_falls_back_to_schema_default(center: ConfigCenter) -> None:
    """默认层未显式写过时，租户解析最终回退注册表默认值（永不抛缺省异常）。"""
    assert center.tenant_override(42, "risk.limits.max_ops_per_day") == 150
    assert center.get_version("risk.limits.max_ops_per_day", tenant_id=42) == 0  # 层未写过


def test_json_value_isolated_by_copy(center: ConfigCenter) -> None:
    """json 值按值返回：调用方修改返回值不影响内部存储与注册表默认值。"""
    got: Any = center.get("strategy.followup.cadence")
    got.append(99)
    assert center.get("strategy.followup.cadence") == [1, 3, 7, 14]


def test_get_typed_enforces_type(center: ConfigCenter) -> None:
    """get<T> 泛型读取：类型相符返回，不符抛 40902（防注册表改型后消费方静默错值）。"""
    assert center.get_typed(int, "risk.limits.max_ops_per_day") == 150
    assert center.get_typed(bool, "model.llm.stream") is True
    with pytest.raises(ConfigValidationError) as exc_info:
        center.get_typed(str, "risk.limits.max_ops_per_day")
    assert exc_info.value.code == CFG_ERR_TYPE
