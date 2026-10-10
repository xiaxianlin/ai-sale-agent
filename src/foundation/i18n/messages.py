# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""错误码 → 中文可行动文案映射中心（单一事实源，FR-KB-023 / NFR-USA-02，迭代 0.3）。

职责（SR-KB-023 §6.3 错误文案人话化、SR-NFR-USA-02 §6.4 动态文案线）：
- 覆盖 04-接口设计 §1.3 全部错误码（40001/40101/40301/40401/40901/42901/50001/50002，
  含 SR-KB-020 已登记的 v1.1 新增 40401/40901）+ 本仓库已用业务码（同前述全表）；
- 每条文案三件套：``title``（用户文案，说人话）+ ``action``（「该怎么办」动作建议）+
  ``detail``（技术说明，前端折叠、供工程师排查）；
- ``message(code)``：统一取文案入口（未知码回落 DEFAULT 兜底，不抛异常）；
- ``to_hints(code, detail)``：装配统一信封 ``data.hints``（SR-KB-023 §5 通道，
  title/action 文案与 SR-KB-023 §6.3 逐字一致）；
- ``startup_selfcheck(codes)``：缺失码启动自检——各服务新增业务码未在本表登记即
  打印中文告警并返回清单（登记义务由此变成机器可判定）；
- ``TERMS`` / ``UI_STANDARD_TERMS`` / ``ZH_WHITELIST``：与 SRS §1.3 一致的中英
  术语映射与界面用语规范，供文案统一与 tools/usability/zh_scan.py 扫描器共用。

维护规则：新增业务错误码必须同步登记 ``ERROR_MESSAGES``（否则启动自检告警、
 zh_scan 可检出英文兜底文案）；title/action 修改须与 SR-KB-023 §6.3 文稿同步评审。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

#: 成功业务码（04-接口设计 §1.3：code=0 时 message 恒 "ok"，不渲染给用户）
SUCCESS_CODE: int = 0


@dataclass(frozen=True)
class ErrorMessage:
    """单条错误文案：用户文案 + 动作建议 + 技术说明。

    - ``title``：用户文案（说人话，界面弹窗标题）；
    - ``action``：动作建议（「该怎么办」，面向非技术人员必须非空）；
    - ``detail``：技术说明（错误含义 / 排查线索 / 对应 HTTP 状态，前端折叠展示）；
    - ``http_status``：本码惯用的 HTTP 状态（仅参考，网关/服务可按场景微调）。
    """

    code: int
    http_status: int
    title: str
    action: str
    detail: str

    @property
    def user_message(self) -> str:
        """组合后的完整用户文案：title + action（一句人话 + 下一步动作）。"""
        return f"{self.title}。{self.action}"


#: 错误码全表：04-接口设计 §1.3 八码 + 本仓库已用业务码（title/action 与 SR-KB-023 §6.3 逐字一致）
ERROR_MESSAGES: dict[str, ErrorMessage] = {
    "40001": ErrorMessage(
        code=40001,
        http_status=400,
        title="有内容填写不对",
        action="请按表单红字提示修改后，再点一次保存",
        detail="参数校验失败（04 §1.3）：请求参数未通过 Pydantic/业务规则校验，"
        "data.errors 定位到具体字段与原因；对应 HTTP 400。",
    ),
    "40101": ErrorMessage(
        code=40101,
        http_status=401,
        title="登录已过期",
        action="请点「确定」返回登录页重新登录",
        detail="API Key / JWT 无效或过期：凭证缺失、错误或已吊销；不区分具体原因以防密钥探测"
        "（FR-KB-031）；服务端调用方请检查 API Key 配置；对应 HTTP 401。",
    ),
    "40301": ErrorMessage(
        code=40301,
        http_status=403,
        title="您没有该操作权限",
        action="如需开通，请联系管理员在成员管理中授权",
        detail="RBAC 权限不足（04 §1.3），或来源 IP 不在调用白名单（FR-KB-031 IP 策略）；对应 HTTP 403。",
    ),
    "40401": ErrorMessage(
        code=40401,
        http_status=404,
        title="内容不存在或已删除",
        action="请回到列表刷新；30 天内删除的条目可请管理员恢复",
        detail="资源不存在或已软删：知识条目 / 版本 / 导入任务 / 评测任务的 UID 不存在"
        "（SR-KB-020/021/022/016 共用本码）；对应 HTTP 404。",
    ),
    "40901": ErrorMessage(
        code=40901,
        http_status=409,
        title="这条内容刚被同事改过",
        action="请刷新页面，对比最新内容后再修改",
        detail="乐观锁并发冲突：expected_version 与存储当前版本不一致，他人已先保存"
        "（SR-KB-020 §5③ / SR-KB-022 回滚同码）；对应 HTTP 409。",
    ),
    "42901": ErrorMessage(
        code=42901,
        http_status=429,
        title="操作有点频繁",
        action="请稍等几秒再试",
        detail="触发限流：QPS 或日配额超限（FR-KB-031），响应附 Retry-After 头（秒）可按其等待；"
        "对应 HTTP 429。",
    ),
    "50001": ErrorMessage(
        code=50001,
        http_status=500,
        title="系统暂时开小差了",
        action="请稍后重试；若反复失败，请把下方错误编号发给管理员",
        detail="内部错误：未分类异常兜底；请以 trace_id 检索服务日志与 audit_log（NFR-SEC-03）；"
        "对应 HTTP 500。",
    ),
    "50002": ErrorMessage(
        code=50002,
        http_status=503,
        title="AI 服务正忙",
        action="系统会自动处理，您稍后来查看结果即可",
        detail="下游依赖超时/故障：Embedding/向量库/重排等下游不可用，已触发降级策略"
        "（SR-KB-030 §6.2③）；HTTP 状态取网关降级语义 503。",
    ),
}

#: 未登记码兜底文案（message() 未知码回落于此；zh_scan/启动自检应保证极少走到）
DEFAULT_MESSAGE: ErrorMessage = ErrorMessage(
    code=0,
    http_status=500,
    title="操作未成功",
    action="请重试；若反复失败请联系管理员并附错误编号",
    detail="未登记业务码的兜底文案：请把 code 与 trace_id 一并发给管理员登记到"
    "platform/core/i18n/messages.py 的 ERROR_MESSAGES。",
)

#: 成功文案（code=0：协议 message 恒 "ok" 不渲染给用户，此处仅供内部展示/测试断言）
SUCCESS_MESSAGE: ErrorMessage = ErrorMessage(
    code=SUCCESS_CODE,
    http_status=200,
    title="操作成功",
    action="",
    detail="code=0 成功信封（04 §1.3）：message 协议约定为 ok，前端成功提示一律用自身文案，不渲染本条。",
)

#: 已登记错误码集合（int 形态，供启动自检与测试遍历）
ERROR_CODES: frozenset[int] = frozenset(entry.code for entry in ERROR_MESSAGES.values())


def message(code: int | str) -> ErrorMessage:
    """按业务码取中文文案（统一取文案入口）。

    - int / ``"40001"`` 均可；空白容忍；
    - ``0`` → SUCCESS_MESSAGE（成功文案）；
    - 已登记错误码 → 对应条目；
    - 未登记码 → DEFAULT_MESSAGE 兜底（不抛异常：界面永远有话可说，缺口由
      启动自检/扫描器另行暴露）。
    """
    key = str(code).strip()
    if key == str(SUCCESS_CODE):
        return SUCCESS_MESSAGE
    return ERROR_MESSAGES.get(key, DEFAULT_MESSAGE)


def to_hints(code: int | str, detail: str = "") -> dict[str, str]:
    """装配统一信封 ``data.hints``（SR-KB-023 §5/§6.3：title + action + detail）。

    ``detail`` 入参为本次请求的动态技术细节（如字段定位），缺省回落映射表的
    静态技术说明；前端拦截器优先消费本结构弹中文通知。
    """
    entry = message(code)
    return {"title": entry.title, "action": entry.action, "detail": detail or entry.detail}


def is_registered(code: int | str) -> bool:
    """判定业务码是否已登记（0 为成功码，恒视为已登记）。"""
    key = str(code).strip()
    return key == str(SUCCESS_CODE) or key in ERROR_MESSAGES


def missing_codes(codes: Iterable[int | str]) -> list[str]:
    """筛选未登记的业务码（启动自检的纯函数核）。

    输入容错：非数值/空白片段跳过（日志噪声不入结果）；成功码 0 不参与判定。
    """
    missed: list[str] = []
    for code in codes:
        key = str(code).strip()
        if not key or not key.lstrip("-").isdigit():
            continue
        if not is_registered(key) and key not in missed:
            missed.append(key)
    return missed


def startup_selfcheck(
    codes: Iterable[int | str], *, log: Callable[[str], None] | None = None
) -> list[str]:
    """缺失码启动自检（告警函数）：新增业务码未登记即打印中文告警。

    各服务在启动装配时把自己会吐出的业务码集合传入（如检索路由的
    ``[40001, 40101, 40301, 42901, 50002]``）；返回未登记清单（空即通过），
    默认经 ``log``（缺省 print）输出告警，CI 可断言返回值为空。
    """
    missed = missing_codes(codes)
    emit = log if log is not None else print
    for key in missed:
        emit(
            f"[文案自检告警] 业务码 {key} 未登记中文文案：请到 "
            f"platform/core/i18n/messages.py 的 ERROR_MESSAGES 补登记（FR-KB-023 / NFR-USA-02）"
        )
    return missed


# --------------------------------------------------------------------------- 术语表常量（SRS §1.3 口径）

#: 中英术语映射（解释与 SRS §1.3「术语与缩略语」逐字一致，供全部界面/文案统一用语）
TERMS: dict[str, str] = {
    "OpenClaw": "开源 AI 浏览器自动化框架，通过视觉理解 + 浏览器控制实现「AI 像人一样操作网页」",
    "RAG": "Retrieval-Augmented Generation，检索增强生成",
    "MCP": "Model Context Protocol，模型上下文协议",
    "P0/P1/P2": "需求优先级：P0=必须实现（缺失即验收不通过）；P1=重要；P2=可选",
    "UAT": "User Acceptance Test，用户验收测试",
    "VLM": "Visual Language Model，视觉语言模型",
    "Embedding": "将文本转换为向量表示的技术",
    "BGE-M3": "开源多语言 Embedding 模型，本项目要求本地部署",
    "Milvus": "开源向量数据库",
    "Qdrant": "开源向量数据库",
    "DeepSeek-V3": "项目指定的大语言模型（AI 大脑核心模型）",
    "Inquiry": "询盘：买家通过阿里国际站等渠道发起的购买咨询会话",
    "Standalone Site": "独立站：企业自有品牌电商网站（区别于第三方平台店铺）",
    "Sub-account": "子账号：阿里国际站主账号下开通的运营/客服子账号",
    "Main Account": "主账号：阿里国际站付费开通的顶级账号，子账号由其开通管理",
    "White Label": "白标（White Label）：支持替换 Logo/域名/配色的贴牌产品化交付",
    "Multi-tenant": "多租户：单套系统支撑多家企业独立使用、数据隔离的 SaaS 架构",
}

#: 界面统一用语规范（SR-NFR-USA-02 §6.7）：推荐用语 → 不应混用的近义词
UI_STANDARD_TERMS: dict[str, tuple[str, ...]] = {
    "条目": ("文档",),   # 知识「条目」，不用「文档」
    "清理": ("清洗",),   # 面向用户称「数据清理」，「清洗」仅限内部流水线用语
    "评测": ("测试",),   # 检索质量「评测」，避免与 UAT「测试」混淆
}

#: 界面允许出现的英文专有名词/缩写白名单（SR-NFR-USA-02 §6.7 + 迭代 0.3 增补 UUID/trace_id 等；
#: zh_scan 剔除白名单词后再判英文残留；新增白名单须在术语表登记理由并评审）
ZH_WHITELIST: frozenset[str] = frozenset({
    # SR §6.7 基线清单
    "SKU", "MOQ", "API", "URL", "ID", "TopK", "CSV", "Excel", "PDF", "OCR", "ASR", "JWT",
    "RBAC", "RAG", "LLM", "SaaS", "Logo", "Webhook", "UTC", "G", "MB", "GB", "TB", "ki",
    "Agent", "DeepSeek", "Milvus", "Qdrant", "BGE-M3", "OpenClaw",
    # 迭代 0.3 增补（本仓库协议/字段/报告用词）
    "UUID", "UID", "trace_id", "request_id", "job_id", "idempotency", "MCP", "UAT", "VLM",
    "QPS", "P95", "CRUD", "DDL", "HTTP", "HTTPS", "Markdown", "xlsx", "ISO", "X-Trace-Id",
    "X-Actor", "Retry-After", "Bearer", "Embedding",
})
