# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# test_i18n_messages.py —— 错误码中文映射中心元测试（FR-KB-023 §6.3 / SR-NFR-USA-02 §6.4 / §9.1）：
# 全表覆盖、文案质量（说人话 + 动作建议非空）、message()/to_hints() 行为、启动自检告警、术语一致性。
"""映射中心自身可信性测试：文案缺口与术语漂移在这里被拦截，不依赖人工评审。"""

import re

import pytest

from foundation.i18n.messages import (
    DEFAULT_MESSAGE,
    ERROR_CODES,
    ERROR_MESSAGES,
    SUCCESS_MESSAGE,
    TERMS,
    UI_STANDARD_TERMS,
    ZH_WHITELIST,
    is_registered,
    message,
    missing_codes,
    startup_selfcheck,
    to_hints,
)

#: 04-接口设计 §1.3 错误码全表（含 SR-KB-020 登记的 v1.1 新增 40401/40901）
SPEC_CODES = (40001, 40101, 40301, 40401, 40901, 42901, 50001, 50002)

#: 汉字判定（文案质量：title/action/detail 必须含中文）
_RE_HAN = re.compile(r"[\u4e00-\u9fff]")


# ─────────────────────── 全表覆盖与文案质量 ───────────────────────


def test_错误码全表覆盖() -> None:
    """04 §1.3 八个错误码全部登记（一个不漏），ERROR_CODES 与表逐项一致。"""
    for code in SPEC_CODES:
        assert str(code) in ERROR_MESSAGES, f"错误码 {code} 未登记中文文案"
        assert code in ERROR_CODES
    assert set(ERROR_CODES) == set(SPEC_CODES), "映射表存在规格外的多余错误码"


def test_每条文案三件套齐全且为中文() -> None:
    """title（用户文案）/action（该怎么办）/detail（技术说明）非空且含中文。"""
    for key, entry in ERROR_MESSAGES.items():
        assert entry.code == int(key), f"{key} 键与条目 code 不一致"
        for field_name in ("title", "action", "detail"):
            value = getattr(entry, field_name)
            assert value and value.strip(), f"{key}.{field_name} 为空"
            assert _RE_HAN.search(value), f"{key}.{field_name} 不含中文：{value!r}"
        assert entry.http_status in range(200, 600)


def test_动作建议非空且可行动() -> None:
    """每条 action 必须给出「该怎么办」（含请/您等面向用户的引导词），不许只报错不指路。

    50002（AI 服务正忙）按 SR-KB-023 §6.3 逐字口径为「系统会自动处理，您稍后…」，
    无「请」但有「您」，故两者取其一。"""
    for key, entry in ERROR_MESSAGES.items():
        assert any(marker in entry.action for marker in ("请", "您")), (
            f"{key} 动作建议缺少可行动指引：{entry.action!r}"
        )
        assert entry.user_message.startswith(entry.title)


def test_标题文案与SR文稿逐字一致() -> None:
    """title 口径锚定 SR-KB-023 §6.3（两文档文案不允许各自演化，抽查全部八码标题）。"""
    expected_titles = {
        40001: "有内容填写不对",
        40101: "登录已过期",
        40301: "您没有该操作权限",
        40401: "内容不存在或已删除",
        40901: "这条内容刚被同事改过",
        42901: "操作有点频繁",
        50001: "系统暂时开小差了",
        50002: "AI 服务正忙",
    }
    for code, title in expected_titles.items():
        assert message(code).title == title


def test_下游降级码HTTP状态为503() -> None:
    """50002 沿用检索 API 冻结口径：网关降级语义 HTTP 503（非 500）。"""
    assert ERROR_MESSAGES["50002"].http_status == 503
    assert ERROR_MESSAGES["50001"].http_status == 500


# ─────────────────────── message() / to_hints() 行为 ───────────────────────


def test_message接受int与str() -> None:
    """int / 字符串两种形态等价取文案。"""
    assert message(40901) is ERROR_MESSAGES["40901"]
    assert message("40901") is ERROR_MESSAGES["40901"]
    assert message(" 42901 ") is ERROR_MESSAGES["42901"]


def test_message成功码与未知码兜底() -> None:
    """0 → 成功文案；未登记码 → DEFAULT 兜底（界面永远有话可说，缺口由自检暴露）。"""
    assert message(0) is SUCCESS_MESSAGE
    assert message("0") is SUCCESS_MESSAGE
    fallback = message(99999)
    assert fallback is DEFAULT_MESSAGE
    assert fallback.title and fallback.action  # 兜底也必须带动作建议
    assert is_registered(0) and is_registered("40001")
    assert not is_registered(99999)


def test_to_hints装配与detail覆盖() -> None:
    """to_hints 产出 data.hints 三键；动态 detail 优先，缺省回落静态技术说明。"""
    hints = to_hints(40001)
    assert set(hints) == {"title", "action", "detail"}
    assert hints["title"] == "有内容填写不对"
    assert hints["detail"] == ERROR_MESSAGES["40001"].detail
    assert to_hints(50001, detail="trace_id=abc")["detail"] == "trace_id=abc"


# ─────────────────────── 启动自检（新增码未登记即告警） ───────────────────────


def test_启动自检_未登记码告警() -> None:
    """混入未登记码 → 返回缺失清单并逐条输出中文告警（含登记路径指引）。"""
    warnings: list[str] = []
    missed = startup_selfcheck([40001, 99999, "88888"], log=warnings.append)
    assert missed == ["99999", "88888"]
    assert len(warnings) == 2
    assert all("99999" in w or "88888" in w for w in warnings)
    assert all("messages.py" in w for w in warnings), "告警必须指明登记位置"


def test_启动自检_全登记通过() -> None:
    """全部已登记（含成功码 0）→ 空清单零告警；噪声输入（空串/非数值）被忽略。"""
    warnings: list[str] = []
    assert startup_selfcheck([*SPEC_CODES, 0], log=warnings.append) == []
    assert warnings == []
    assert missing_codes(["", "abc", None]) == []  # type: ignore[list-item]


def test_missing_codes去重保序() -> None:
    """重复未登记码只报一次，按出现顺序返回。"""
    assert missing_codes([99999, 40001, 99999, "77701"]) == ["99999", "77701"]


# ─────────────────────── 术语表一致性（SRS §1.3 口径） ───────────────────────


def test_术语表覆盖SRS术语() -> None:
    """SRS §1.3 关键术语全部入表，且中文解释含汉字（中英映射供文案统一）。"""
    required = {
        "OpenClaw", "RAG", "MCP", "UAT", "VLM", "Embedding", "BGE-M3", "Milvus", "Qdrant",
        "DeepSeek-V3", "Inquiry", "Standalone Site", "Sub-account", "White Label", "Multi-tenant",
    }
    assert required <= set(TERMS), f"缺术语：{required - set(TERMS)}"
    for english, chinese in TERMS.items():
        assert _RE_HAN.search(chinese), f"术语 {english} 的解释不含中文：{chinese!r}"


def test_界面统一用语规范() -> None:
    """条目/清理/评测三个推荐用语及其禁用近义词齐备（SR-NFR-USA-02 §6.7）。"""
    assert set(UI_STANDARD_TERMS) >= {"条目", "清理", "评测"}
    assert UI_STANDARD_TERMS["条目"] == ("文档",)
    assert UI_STANDARD_TERMS["清理"] == ("清洗",)
    assert UI_STANDARD_TERMS["评测"] == ("测试",)


def test_白名单包含必备专有名词() -> None:
    """扫描器白名单：SR §6.7 基线词 + 任务指定的 UUID/trace_id 均在单一事实源内。"""
    required = {"SKU", "MOQ", "API", "URL", "ID", "CSV", "Excel", "PDF", "JWT", "SaaS",
                "UUID", "trace_id", "DeepSeek", "Milvus", "Qdrant", "BGE-M3", "OpenClaw"}
    assert required <= set(ZH_WHITELIST)


@pytest.mark.parametrize("code", SPEC_CODES)
def test_全表逐码parametrized冒烟(code: int) -> None:
    """逐码冒烟：message() 可取、文案非空、已登记（把全表钉进测试证据链）。"""
    entry = message(code)
    assert entry.code == code
    assert entry.title and entry.action and entry.detail
    assert is_registered(code)
