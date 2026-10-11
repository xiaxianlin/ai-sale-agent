# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""移动端 uni-app 页面源码静态断言（FR-SC-006 双端可移植，SR-SC-006 §2/§6.1；P4 第二批 TDD）。

web/mobile/ 为 uni-app 页面源码交付（无构建链）：本测试以静态断言保证——
- pages.json 合法（去条件编译注释后可 JSON 解析）、三页面（告警/审批/看板）注册
  且导航标题中文化（NFR-USA-02）；
- 双端条件编译标记齐备（#ifdef H5 / #ifdef MP-WEIXIN，OI-08 不锁定单一形态）；
- 审批页含 verify_token 二次校验流（SRS §7.4 红线的端上加严）；
- 无任何硬编码凭证（占位符口径，NFR-SEC-04）。

实现方案：docs/07-实现方案/SC/SR-SC-006-移动端.md §2、§6.1、§9（集成③双端可移植）
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MOBILE_SRC = Path(__file__).resolve().parents[4] / "web" / "mobile" / "src"
PAGES = ("alert/center", "approval/list", "dashboard/index")


def strip_conditional_comments(text: str) -> str:
    """去掉 pages.json 内的条件编译注释（uni-app 允许 // #ifdef 形态注释）。"""
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def test_pages_json_valid_and_registered() -> None:
    """pages.json 去注释后可解析；三页面注册且导航标题中文（NFR-USA-02）。"""
    raw = (MOBILE_SRC / "pages.json").read_text(encoding="utf-8")
    data = json.loads(strip_conditional_comments(raw))
    paths = [p["path"] for p in data["pages"]]
    assert set(paths) == set(PAGES)
    for page in data["pages"]:
        title = page["style"]["navigationBarTitleText"]
        assert re.search(r"[\u4e00-\u9fff]", title), f"{page['path']} 标题未中文化"


def test_double_platform_conditional_marks() -> None:
    """双端条件编译：pages.json 与三个 .vue 均含 H5（钉钉微应用）与 MP-WEIXIN 标记。"""
    pages_raw = (MOBILE_SRC / "pages.json").read_text(encoding="utf-8")
    assert "// #ifdef H5" in pages_raw and "// #ifdef MP-WEIXIN" in pages_raw
    for rel in PAGES:
        vue = (MOBILE_SRC / "pages" / f"{rel}.vue").read_text(encoding="utf-8")
        assert "#ifdef H5" in vue and "#ifdef MP-WEIXIN" in vue, f"{rel} 缺双端条件编译标记"
        assert re.search(r"[\u4e00-\u9fff]", vue), f"{rel} 界面文案未中文化"


def test_approval_verify_flow_and_no_secrets() -> None:
    """审批页含 verify_token 二次校验流（/mobile/verify + confirm）；全目录无硬编码凭证。"""
    approval = (MOBILE_SRC / "pages" / "approval" / "list.vue").read_text(encoding="utf-8")
    assert "verify_token" in approval and "/api/v1/mobile/verify" in approval
    assert "confirm" in approval and "reject" in approval
    for vue in MOBILE_SRC.rglob("*.vue"):
        text = vue.read_text(encoding="utf-8")
        for secret in ("app_secret", "password =", "Bearer hard"):
            assert secret not in text, f"{vue.name} 疑似硬编码凭证（NFR-SEC-04 占位符口径）"
