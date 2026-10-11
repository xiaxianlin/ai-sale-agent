# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·模板管理与渲染用例（SR-SITE-004 §6.5/§9；TDD 阶段①②）。

覆盖：渲染成功、缺失变量拒绝外发（TemplateVarMissing）、未知变量白名单拒绝、
危险语法沙箱拒绝（防注入）、多语言缺版回落 en、draft 不可用、退订链接内置注入、
预览样例变量、versions 结构校验（五类中的异常/降级族）。
"""
from __future__ import annotations

import pytest

from services.site.core.mailing.templates import (
    TemplateInvalid,
    TemplateNotFoundError,
    TemplateRegistry,
    TemplateVarMissing,
    inject_unsubscribe,
    render_str,
)

from .fakes import MutableClock

BASE_VARS = {"customer_name": "Ace", "email": "a@b.com", "cart_items": "ST-3200 x2",
             "coupon_code": "SAVE5", "quote_no": "Q-1", "store_url": "https://d.com",
             "expiry": "2026-10-20"}


def make_registry(**kw) -> TemplateRegistry:
    """带一条 published 弃购模板的注册表。"""
    reg = TemplateRegistry(clock=MutableClock().now, **kw)
    reg.upsert("cart_recover_remind", {
        "en": {"subject": "Hi {{ customer_name }}", "body_html": "<p>{{ cart_items }}</p>"},
        "zh": {"subject": "您好 {{ customer_name }}", "body_html": "<p>{{ cart_items }}</p>"},
    })
    return reg


class TestRender:
    """渲染与变量校验。"""

    def test_render_substitutes(self) -> None:
        """正常渲染：槽位全命中 → 文本替换成功（等价类）。"""
        assert render_str("Hi {{ customer_name }}!", {"customer_name": "Ace"}) == "Hi Ace!"

    def test_missing_var_rejected(self) -> None:
        """缺失变量：TemplateVarMissing，绝不外发残文（SR §6.5 Undefined 不外发）。"""
        with pytest.raises(TemplateVarMissing):
            render_str("Hi {{ customer_name }}, {{ coupon_code }}", {"customer_name": "Ace"})

    def test_unknown_var_rejected(self) -> None:
        """变量不在白名单（如 price 自由来源）：TemplateInvalid（受控槽位）。"""
        with pytest.raises(TemplateInvalid):
            render_str("{{ price }}", {"price": 9.9})

    @pytest.mark.parametrize("tpl", [
        "{{ ''.__class__ }}",                 # 属性访问/表达式 → 非简单槽位
        "{% for x in y %}{{ x }}{% endfor %}",  # 语句标签
    ])
    def test_dangerous_syntax_sandboxed(self, tpl: str) -> None:
        """沙箱：仅允许 {{ identifier }} 简单槽位，表达式/语句一律 TemplateInvalid（防注入）。"""
        with pytest.raises(TemplateInvalid):
            render_str(tpl, BASE_VARS)

class TestRegistry:
    """模板注册表：多语言 / 状态机 / 预览。"""

    def test_lang_fallback_to_en(self) -> None:
        """客户语言 ko 无版本 → 回落 en 渲染成功（降级；SR-SITE-001 语种对齐）。"""
        reg = make_registry()
        subject, html = reg.render("cart_recover_remind", "ko", {"customer_name": "A", "cart_items": "x"})
        assert subject == "Hi A" and html == "<p>x</p>"

    def test_missing_lang_and_en_not_found(self) -> None:
        """无 en 版且请求语言缺版：TemplateNotFoundError（异常分支）。"""
        reg = TemplateRegistry(clock=MutableClock().now)
        reg.upsert("t", {"zh": {"subject": "s", "body_html": "b"}})
        with pytest.raises(TemplateNotFoundError):
            reg.get("t", "ko")

    def test_draft_not_served(self) -> None:
        """draft 模板不可用于渲染（仅 published；SR §4 mail_template.status CHECK）。"""
        reg = TemplateRegistry(clock=MutableClock().now)
        reg.upsert("t", {"en": {"subject": "s", "body_html": "b"}}, status="draft")
        with pytest.raises(TemplateNotFoundError):
            reg.get("t", "en")
        reg.publish("t")
        assert reg.get("t", "en") is not None

    def test_upsert_validates_versions(self) -> None:
        """versions 结构非法（缺 subject/body_html）：ValueError（入参异常族）。"""
        reg = TemplateRegistry(clock=MutableClock().now)
        with pytest.raises(ValueError):
            reg.upsert("t", {"en": {"subject": "only"}})

    def test_preview_uses_sample_vars(self) -> None:
        """预览接口同链路：variables 缺省取 sample_vars（SR §5①）。"""
        reg = TemplateRegistry(clock=MutableClock().now)
        reg.upsert("t", {"en": {"subject": "Hi {{ customer_name }}", "body_html": "{{ store_url }}"}},
                   sample_vars={"customer_name": "样例客户", "store_url": "https://d.com"})
        subject, html = reg.preview("t", "en")
        assert subject == "Hi 样例客户" and html == "https://d.com"


class TestUnsubscribeInjection:
    """退订合规（每封营销邮件正文强制带退订链接）。"""

    def test_inject_unsubscribe_link(self) -> None:
        """正文末尾注入退订链接（token 形态，URL 不含明文 PII）。"""
        html = inject_unsubscribe("<p>hi</p>", "https://d.com/s/m/u/tok123")
        assert "https://d.com/s/m/u/tok123" in html and html.startswith("<p>hi</p>")

