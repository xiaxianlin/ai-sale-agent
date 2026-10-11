# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""JS SDK 源码静态结构断言（FR-SITE-001：源码交付无构建链，以正则检查关键结构，SR-SITE-001 §9）。

静态断言口径（源码级）：加载器 ES5 + 一行接入 + 异步注入 + 单命名空间；
主体 WS 心跳/退避重连/长轮询降级/Shadow DOM/consent 门控/批量上报/i18n。
"""
from __future__ import annotations

import re
from pathlib import Path

#: web/site-sdk 源码目录（tests→site→services→src→repo→web/site-sdk）
SDK_DIR = Path(__file__).resolve().parents[4] / "web" / "site-sdk"


def read(name: str) -> str:
    """读取 SDK 源文件（缺失即断言失败，红/绿共用）。"""
    path = SDK_DIR / name
    assert path.is_file(), f"缺少 SDK 源文件：{path}"
    return path.read_text(encoding="utf-8")


def test_loader_header_and_one_line_data_key() -> None:
    """K1+K2 loader.js：COM-IP-01 头注释；一行接入形态（data-key/data-host/currentScript）。"""
    src = read("loader.js")
    assert "COM-IP-01" in src[:400]
    assert "data-key" in src and "data-host" in src and "currentScript" in src


def test_loader_async_inject_and_namespace() -> None:
    """K3 异步注入不阻塞 + 全局仅一个 __asaChat 命名空间（§2.1 低侵入）。"""
    src = read("loader.js")
    assert re.search(r"\.async\s*=\s*true", src)
    assert "appendChild" in src and "__asaChat" in src


def test_loader_is_es5() -> None:
    """K4 加载器 ES5 门禁：无箭头函数/let/const/模板字符串（§2.3 降级③基线）。"""
    src = read("loader.js")
    assert "=>" not in src, "loader.js 含箭头函数（非 ES5）"
    assert not re.search(r"\b(let|const)\s", src), "loader.js 含块级作用域声明（非 ES5）"
    assert "`" not in src, "loader.js 含模板字符串（非 ES5）"


def test_chat_header_jsdoc_and_ws() -> None:
    """K5+K6 chat.js：COM-IP-01 头 + JSDoc 注解；WS 连接与 4401 鉴权失败处理（§5②）。"""
    src = read("chat.js")
    assert "COM-IP-01" in src[:400]
    assert "@param" in src and ("@returns" in src or "@return" in src)
    assert "new WebSocket(" in src and "4401" in src


def test_chat_backoff_reconnect() -> None:
    """K7 断线指数退避重连：2^n 退避 + setTimeout + 随机抖动（§6.4）。"""
    src = read("chat.js")
    assert re.search(r"Math\.pow\(2", src) or re.search(r"2 \*\* ", src)
    assert "setTimeout" in src and re.search(r"Math\.random\(\)", src), "缺少重连抖动"


def test_chat_longpoll_fallback() -> None:
    """K8 长轮询降级：连续失败 lpAfter 次切 LongPollTransport（§2.3 降级①）。"""
    src = read("chat.js")
    assert "lpAfter" in src
    assert "LongPollTransport" in src and "switchTransport" in src
    assert "/api/v1/site/messages" in src


def test_chat_shadow_dom_isolation() -> None:
    """K9 Shadow DOM 隔离 + asa- 前缀降级（§2.1 样式隔离/§2.3 降级②）。"""
    src = read("chat.js")
    assert "attachShadow" in src and "asa-" in src


def test_chat_heartbeat() -> None:
    """K10 心跳：ping/pong 缺失计数判死链（§6.4 startHeartbeat）。"""
    src = read("chat.js")
    assert re.search(r"type:\s*'ping'", src)
    assert "pongMiss" in src and "setInterval" in src


def test_chat_consent_gate() -> None:
    """K11 consent 门控：asa_consent 三态 granted 判定；未同意禁行为上报与邀聊（§6.6/FR-SITE-007）。"""
    src = read("chat.js")
    assert "asa_consent" in src and "'granted'" in src
    assert re.search(r"(track\.enabled\s*=\s*false|inviteEnabled\s*=\s*false|disable\(\))", src)


def test_chat_tracking_batch_and_beacon() -> None:
    """K12 行为上报：批量缓冲（BATCH_MAX/FLUSH_MS）+ pagehide sendBeacon + events 端点（FR-SITE-002 §6.1）。"""
    src = read("chat.js")
    assert "BATCH_MAX" in src and "FLUSH_MS" in src
    assert "sendBeacon" in src and "pagehide" in src
    assert "/api/v1/site/events" in src


def test_chat_i18n_and_session_call() -> None:
    """K13 多语言与会话初始化：navigator.language 检测 + en 兜底；POST /site/session（§6.2/§6.5）。"""
    src = read("chat.js")
    assert "navigator.language" in src and re.search(r"'en'", src)
    assert "/api/v1/site/session" in src


def test_readme_documents_one_line_integration() -> None:
    """K14 README：一行接入示例 + data 属性说明（交付文档）。"""
    readme = read("README.md")
    assert "sdk.js" in readme and "data-key" in readme
    assert "chat.js" in readme
