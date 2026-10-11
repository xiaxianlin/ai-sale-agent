# 知识产权归甲方所有（COM-IP-01）
"""TDD 补充测试（平台域）：SEC-05 传输加密——Nginx TLS 基线配置静态校验。

TDD 新规口径：deploy/nginx/ai-sale.conf 无单元测试（部署面），按平台域 TDD 补齐
约定增加配置静态校验（不启动 nginx，仅解析配置文本做基线断言）：
- ssl_protocols 仅 TLSv1.2/TLSv1.3：TLSv1.2/TLSv1.3 必须存在，SSLv3/TLSv1/TLSv1.1
  不得以任何形式出现（SR-NFR-SEC-05 §2 偏差①：下界收紧，sslscan 零弱协议）；
- HSTS 必须存在：每个 443 server 均携带 max-age=31536000 + always（含 location
  内 add_header 覆盖 server 级继承的重复声明）；
- TLS 基线配套项：仅 AEAD 前向保密套件（无 CBC/RC4/NULL/DES）、会话票证关闭、
  SNI 纪律（未登记域名拒绝握手）、80 端口仅 301 跳转 HTTPS。

KPI 映射：SEC-05 → NFR-SEC-05（对外接口 HTTPS/TLS 覆盖率 100%、弱协议暴露面=0、
HSTS 覆盖全部 443 server）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "ai-sale.conf"


def _conf_text() -> str:
    """读取被测配置（缺失即失败：部署基线文件不得移位）。"""
    assert NGINX_CONF.is_file(), f"TLS 基线配置缺失：{NGINX_CONF}"
    return NGINX_CONF.read_text(encoding="utf-8")


def _ssl_protocols_tokens(text: str) -> set[str]:
    """提取全部 ssl_protocols 指令的协议令牌集合。"""
    tokens: set[str] = set()
    for match in re.finditer(r"(?m)^\s*ssl_protocols\s+(.+?);", text):
        tokens.update(match.group(1).split())
    return tokens


def test_tls_protocols_only_12_13() -> None:
    """【边界/异常】协议基线：TLSv1.2/TLSv1.3 存在；SSLv3/TLSv1/TLSv1.1 零出现。

    KPI 映射：NFR-SEC-05 弱协议暴露面=0（sslscan 仅 1.2/1.3 的配置前提）。
    """
    text = _conf_text()
    tokens = _ssl_protocols_tokens(text)
    assert tokens == {"TLSv1.2", "TLSv1.3"}, f"ssl_protocols 基线漂移：{tokens}"
    # 弱协议不得以任何令牌形态混入任何 ssl_protocols 指令
    forbidden = {"SSLv3", "SSLv2", "TLSv1", "TLSv1.0", "TLSv1.1"}
    assert not (tokens & forbidden), f"发现禁用协议：{tokens & forbidden}"


def test_hsts_on_every_https_server() -> None:
    """【等价类】HSTS 覆盖：≥3 处 add_header Strict-Transport-Security
    （app/train、api、chat 三个 443 server），且每处均含一年 max-age 与 always；
    chat /sdk.js location 内的重复声明（nginx 继承规则补偿）同样计入。

    KPI 映射：NFR-SEC-05 HSTS 覆盖率=100%（全部 443 server 强制浏览器走 HTTPS）。
    """
    text = _conf_text()
    hsts_lines = [
        line for line in text.splitlines()
        if "add_header" in line and "Strict-Transport-Security" in line
    ]
    assert len(hsts_lines) >= 3, f"443 server 的 HSTS 声明不足：{len(hsts_lines)} 处"
    for line in hsts_lines:
        assert "max-age=31536000" in line, f"HSTS 缺一年 max-age：{line!r}"
        assert "always" in line, f"HSTS 未声明 always（非 2xx 也须携带）：{line!r}"
    # 明文 80 端口 server 无 HSTS 之外的业务内容（唯一职责：ACME 挑战 + 301）
    assert text.count("return 301 https://$host$request_uri;") == 1


def test_tls_baseline_companion_settings() -> None:
    """【异常/降级】TLS 基线配套：仅 AEAD 前向保密套件（无 CBC/RC4/NULL/DES）、
    会话票证关闭（票证密钥轮换难控）、SNI 纪律（未登记域名 ssl_reject_handshake）。

    KPI 映射：NFR-SEC-05 套件白名单零弱密码件、防降级协商、默认证书不兜底。
    """
    text = _conf_text()
    cipher_matches = re.findall(r"(?m)^\s*ssl_ciphers\s+(\S+);", text)
    assert cipher_matches, "缺少 ssl_ciphers 套件白名单"
    for ciphers in cipher_matches:
        upper = ciphers.upper()
        for weak in ("CBC", "RC4", "NULL", "DES", "MD5", "SHA1"):
            assert weak not in upper, f"套件白名单混入弱密码件 {weak}: {ciphers}"
        assert all(suite.startswith("ECDHE") for suite in ciphers.split(":")), (
            f"非前向保密套件混入：{ciphers}"
        )
    assert re.search(r"(?m)^\s*ssl_session_tickets\s+off\s*;", text), "会话票证未关闭"
    assert re.search(r"(?m)^\s*ssl_reject_handshake\s+on\s*;", text), "SNI 兜底未拒绝握手"
    # 服务端优选套件开启（防客户端降级协商）
    assert re.search(r"(?m)^\s*ssl_prefer_server_ciphers\s+on\s*;", text)
