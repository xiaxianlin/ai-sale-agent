# 知识产权归甲方所有（COM-IP-01）
"""配置模式（Schema）定义——声明式配置注册表的登记单元（NFR-MAI-02 配置外置）。

依据：docs/07-实现方案/NFR/SR-NFR-MAI-02-配置外置.md §6.1（声明式注册表）。

设计要点（SR §2 决策②「注册表即文档」）：
  - 每个配置项以 ConfigSchema 声明：scope（配置域）/ key（域内键）/ vtype（类型）/
    default（默认值）/ minimum-maximum（数值区间）或 choices（枚举）/ desc（中文说明）；
  - 管理面板表单直接由本 Schema 渲染，界面校验与代码写路径共用同一 check_value 函数，
    杜绝「界面放行、代码崩溃」；
  - 默认值在构造期即自校验（非法默认值禁止登记，防带病注册）；
  - dangerous=True 的危险项（风控阈值等）修改时必须填写变更原因（对应 REST 错误码 40901）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 配置值类型枚举（与 SR-NFR-MAI-02 §6.1 vtype 口径一致）：
#: int / float / bool / str / json（json 指 dict 或 list 结构化值）
ValueType = Literal["int", "float", "bool", "str", "json"]


class ConfigSchema(BaseModel):
    """单个配置项的声明式模式（不可变对象，登记后不允许篡改）。

    用法示例（与 SR §6.1 示例登记对齐）::

        ConfigSchema(
            scope="risk", key="limits.max_ops_per_day", vtype="int",
            default=150, minimum=30, maximum=400,
            desc="单账号每日操作量上限（超限排队次日）", dangerous=True,
        )
    """

    # 冻结模型：Schema 是注册表内的共享只读对象，禁止运行期改字段
    model_config = ConfigDict(frozen=True)

    scope: str = Field(min_length=1, description="配置域：model/strategy/risk/security 等（SRS §1.5 前缀口径）")
    key: str = Field(min_length=1, description="域内短键名，可含点层级（如 limits.max_ops_per_day），不得再带 scope 前缀")
    vtype: ValueType = Field(description="值类型：int/float/bool/str/json")
    default: Any = Field(description="默认值（单一事实来源：与各 SR §7 权威默认一致，构造期即校验）")
    minimum: int | float | None = Field(default=None, description="数值下界（含端点，仅 int/float 类型适用）")
    maximum: int | float | None = Field(default=None, description="数值上界（含端点，仅 int/float 类型适用）")
    choices: tuple[Any, ...] | None = Field(default=None, description="枚举约束：合法值集合（值必须命中其一）")
    desc: str = Field(default="", description="中文说明（管理面板直接展示，非技术人员可读）")
    dangerous: bool = Field(default=False, description="危险项（风控阈值/敏感清单等）：修改必须填写变更原因并二次确认")

    @property
    def full_key(self) -> str:
        """全限定键 ``scope.key``（ConfigCenter.get/set 的键形式，如 risk.limits.max_ops_per_day）。"""
        return f"{self.scope}.{self.key}"

    def check_type(self, value: Any) -> bool:
        """类型校验：值类型必须与注册表 vtype 一致。

        注意：Python 中 bool 是 int 的子类，必须先判 bool 再判 int/float，
        防止 True/False 被当作 1/0 放行（历史踩坑点，禁止调整判断顺序）。
        """
        if self.vtype == "bool":
            return isinstance(value, bool)
        if self.vtype == "int":
            return isinstance(value, int) and not isinstance(value, bool)
        if self.vtype == "float":
            # float 类型宽容接受 int（数值语义等价，如 0 与 0.0）
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if self.vtype == "str":
            return isinstance(value, str)
        # json 类型：结构化值（dict 或 list）
        return isinstance(value, (dict, list))

    def check_value(self, value: Any) -> bool:
        """完整校验：类型 + 取值范围（min-max 或枚举）。

        表单前置校验与服务端写路径强校验共用本函数（SR §6.4 决策②）。
        """
        if not self.check_type(value):
            return False
        # 数值区间校验（含端点）
        if self.vtype in ("int", "float"):
            if self.minimum is not None and value < self.minimum:
                return False
            if self.maximum is not None and value > self.maximum:
                return False
        # 枚举校验（主要用于 str/数值离散枚举）
        if self.choices is not None and value not in self.choices:
            return False
        return True

    @model_validator(mode="after")
    def _validate_schema_self(self) -> "ConfigSchema":
        """构造期自校验：默认值必须通过自身校验；边界声明必须合法。

        默认值非法 → pydantic ValidationError（登记即失败，防带病注册，SR §6.1 register 断言同义）。
        """
        # 先做结构性校验（边界声明本身必须合法），再校验默认值——否则 min>max 时
        # 报错只会落在「默认值非法」，真正的配置错误（区间颠倒）被掩盖
        if self.vtype not in ("int", "float") and (self.minimum is not None or self.maximum is not None):
            raise ValueError(f"minimum/maximum 仅适用于 int/float 类型: {self.full_key}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"取值范围颠倒（minimum > maximum）: {self.full_key}")
        if not self.check_value(self.default):
            raise ValueError(f"默认值非法: {self.full_key} = {self.default!r}（不满足类型/取值范围约束）")
        return self
