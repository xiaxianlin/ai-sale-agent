# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""移动端轻应用包（FR-SC-006，SR-SC-006；Phase 4 第二批）。

两层（§2）：
- :mod:`bff`——移动 BFF 核心：双端免登换发（钉钉微应用/微信）、一屏聚合（短缓存）、
  会话审批移动流（confirm 强制 verify_token 二次校验——SRS §7.4 红线端上加严）、
  人工接管透传、推送路由（钉钉工作通知主力 + 微信订阅额度制 + mp_template/sms 降级链）；
- uni-app 页面源码交付 ``web/mobile/src``（H5/MP-WEIXIN 双端条件编译，静态断言保证）。

依据文档：docs/07-实现方案/SC/SR-SC-006-移动端.md；SRS §9.2（FR-SC-006）。
"""

from services.agent.core.mobile.bff import (
    Binding,
    BindingStore,
    DingTalkWork,
    DingTalkWorkFake,
    FakeApprovalSink,
    FakeConversationSink,
    FakeHomeProvider,
    HomeProvider,
    IdentityStub,
    MobileBff,
    MobileError,
    MobileSession,
    MpTemplate,
    MpTemplateFake,
    ApprovalSink,
    ConversationSink,
    PushProviderError,
    PushRouter,
    SmsFake,
    SmsSender,
    VerifyGate,
    WxSubscribe,
    WxSubscribeFake,
)

__all__ = [
    "Binding", "BindingStore", "MobileBff", "MobileError", "MobileSession", "VerifyGate",
    "PushRouter", "PushProviderError", "IdentityStub",
    "HomeProvider", "FakeHomeProvider", "ApprovalSink", "FakeApprovalSink",
    "ConversationSink", "FakeConversationSink",
    "DingTalkWork", "DingTalkWorkFake", "WxSubscribe", "WxSubscribeFake",
    "MpTemplate", "MpTemplateFake", "SmsSender", "SmsFake",
]
