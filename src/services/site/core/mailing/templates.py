# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·模板管理与沙箱渲染（SR-SITE-004 §6.5，Phase 3 第二批）。

模块职责：
- :class:`TemplateRegistry`——多语言模板 CRUD（mail_template 形态：draft/published 状态机、
  versions 多语言、sample_vars 预览样例），缺语言回落 en（与站点语种对齐，SR-SITE-001）；
- :func:`render_str`——沙箱渲染：仅允许 ``{{ identifier }}`` 简单变量槽位，表达式/属性访问/
  语句标签一律 :class:`TemplateInvalid`（防注入，对齐 Jinja2 SandboxedEnvironment 语义）；
  变量白名单（受控槽位，价格/交期禁自由来源）；缺失变量 :class:`TemplateVarMissing`
  ——调度器捕获后拒绝外发，绝不发出残文；
- :func:`inject_unsubscribe`——每封营销邮件正文强制注入退订链接（退订合规，FR-SITE-007 联动）。

demo 未引入 jinja2 依赖（COM-IP-05 依赖审查）：以受控槽位正则实现同等的
「沙箱 + 白名单 + 缺变量即拒」三防线；生产如需富模板，替换 render_str 内核、协议不变。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

#: 变量白名单（受控槽位；SR §6.5 ALLOWED_VARS）
ALLOWED_VARS: frozenset[str] = frozenset({
    "customer_name", "cart_items", "coupon_code", "quote_no", "store_url", "expiry", "email",
})

#: 简单标识符（沙箱唯一允许的槽位形态）
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: 模板槽位 {{ expr }}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


class TemplateError(Exception):
    """模板域错误基类（对外映射 40001；notfound 映射 40404）。"""

    code = 40001


class TemplateInvalid(TemplateError):
    """非法模板：危险语法/表达式或白名单外变量（沙箱拒绝）。"""


class TemplateVarMissing(TemplateError):
    """缺失变量：渲染结果不完整，拒绝外发残文（SR §6.5 Undefined 不外发）。"""


class TemplateNotFoundError(TemplateError):
    """模板不存在 / 未发布 / 语言缺失且无 en 回落（40404 族）。"""

    code = 40404


@dataclass(frozen=True)
class TplVer:
    """单语言模板版本。"""

    lang: str
    subject: str
    body_html: str


def render_str(template: str, variables: dict) -> str:
    """沙箱渲染：三防线依次执行——语法沙箱 → 变量白名单 → 缺失即拒；通过后才替换。"""
    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if not _IDENTIFIER_RE.fullmatch(expr):  # 防线①：仅允许简单标识符（禁表达式/属性访问）
            raise TemplateInvalid(f"模板含非法表达式：{expr!r}（沙箱仅允许 {{{{ 变量 }}}} 简单槽位）")
        if expr not in ALLOWED_VARS:  # 防线②：变量白名单（受控槽位）
            raise TemplateInvalid(f"未知变量 {expr!r}（白名单外，禁自由来源）")
        if expr not in variables:  # 防线③：缺失变量 → 拒绝外发残文
            raise TemplateVarMissing(f"缺失变量 {expr!r}，拒绝外发残文")
        return str(variables[expr])

    rendered = _PLACEHOLDER_RE.sub(_sub, template)
    if "{{" in rendered or "}}" in rendered or "{%" in rendered or "%}" in rendered:
        # 未闭合槽位或语句标签残留：一律视为非法模板（不猜意图）
        raise TemplateInvalid("模板含未闭合槽位或语句标签（{% %}），沙箱拒绝")
    return rendered


def inject_unsubscribe(html: str, unsub_url: str) -> str:
    """正文末尾注入退订链接（token 形态 URL，不含明文 PII；与 List-Unsubscribe 头配套）。"""
    return (f'{html}\n<p style="color:#888;font-size:12px">'
            f'若不想再收到此类邮件，可<a href="{unsub_url}">点此退订 unsubscribe</a>。</p>')


class TemplateRegistry:
    """模板注册表（mail_template 内存形态；管理后台 CRUD 入口经 API 层暴露）。"""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._data: dict[str, dict] = {}

    def upsert(self, template_key: str, versions: dict[str, dict], sample_vars: dict | None = None,
               status: str = "published", updated_by: str = "demo") -> None:
        """创建/更新模板：versions 形如 {lang: {subject, body_html}}，结构非法即 ValueError。"""
        parsed: dict[str, TplVer] = {}
        for lang, ver in versions.items():
            if not isinstance(ver, dict) or "subject" not in ver or "body_html" not in ver:
                raise ValueError(f"模板版本结构非法：{template_key}/{lang} 须含 subject 与 body_html")
            parsed[lang] = TplVer(lang=lang, subject=ver["subject"], body_html=ver["body_html"])
        if not parsed:
            raise ValueError(f"模板 {template_key} 至少需要一个语言版本")
        self._data[template_key] = {"versions": parsed, "sample_vars": dict(sample_vars or {}),
                                    "status": status, "updated_by": updated_by,
                                    "updated_at": self.clock()}

    def set_status(self, template_key: str, status: str) -> None:
        """状态流转（draft/published/archived）。"""
        self._data[template_key]["status"] = status

    def publish(self, template_key: str) -> None:
        """发布（draft → published）。"""
        self.set_status(template_key, "published")

    def get(self, template_key: str, lang: str = "en") -> TplVer:
        """取模板版本：仅 published；缺语言回落 en；两者皆缺 → TemplateNotFoundError。"""
        entry = self._data.get(template_key)
        if entry is None or entry["status"] != "published":
            raise TemplateNotFoundError(f"模板不可用：{template_key}（不存在或未发布）")
        versions: dict[str, TplVer] = entry["versions"]
        ver = versions.get(lang) or versions.get("en")
        if ver is None:
            raise TemplateNotFoundError(f"模板 {template_key} 缺 {lang} 版且无 en 回落")
        return ver

    def render(self, template_key: str, lang: str, variables: dict) -> tuple[str, str]:
        """渲染（get + render_str 组合）；调用方随后注入退订链接。"""
        ver = self.get(template_key, lang)
        return render_str(ver.subject, variables), render_str(ver.body_html, variables)

    def preview(self, template_key: str, lang: str, variables: dict | None = None) -> tuple[str, str]:
        """预览同链路：variables 缺省取 sample_vars（管理后台非技术人员预览，SR §5①）。"""
        merged = {**self._data[template_key]["sample_vars"], **(variables or {})}
        return self.render(template_key, lang, merged)
