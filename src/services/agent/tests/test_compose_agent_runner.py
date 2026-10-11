# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""agent-runner 编排片段结构断言（FR-AG-001，SR-AG-001 §6.1/§9）。

覆盖（deploy/compose/agent-runner.yaml，独立文件不改既有 compose.yaml）：
- 文件存在、可解析、版权头注释；
- agent-runner-01：restart=always、独立 Cookie 卷（一账号一卷）、spool 卷；
- 出口代理环境变量三件套（HTTP_PROXY/HTTPS_PROXY/NO_PROXY）；
- 业务健康检查（healthcheck 存在且含 start_period 恢复预算）；
- proxy-egress-01 出口容器与网络命名空间绑定（network_mode）；
- 零凭证明文：代理/账号接入串一律 ${...} 占位（scan_secrets 口径）。

实现方案：docs/07-实现方案/AG/SR-AG-001-基础设施.md §6.1、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
COMPOSE_PATH = REPO / "deploy" / "compose" / "agent-runner.yaml"
LEGACY_COMPOSE_PATH = REPO / "deploy" / "compose" / "compose.yaml"


def load_compose() -> dict:
    """解析 agent-runner.yaml（结构断言入口）。"""
    assert COMPOSE_PATH.is_file(), f"缺少编排片段：{COMPOSE_PATH}"
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def runner_service() -> dict:
    """取 agent-runner-01 服务定义。"""
    services = load_compose().get("services", {})
    assert "agent-runner-01" in services, "缺少 agent-runner-01 服务定义"
    return services["agent-runner-01"]


def env_map(service: dict) -> dict[str, str]:
    """environment 列表（K=V 字符串）转映射，便于断言。"""
    result: dict[str, str] = {}
    for item in service.get("environment", []):
        key, _, value = str(item).partition("=")
        result[key] = value
    return result


def test_compose_file_exists_with_header() -> None:
    """文件存在、YAML 可解析、版权头（COM-IP-01）注释在文件头 5 行内。"""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    load_compose()
    assert any("知识产权归甲方所有" in line for line in text.splitlines()[:5])


def test_runner_restart_policy_always() -> None:
    """restart=always：进程崩溃退出自动拉起（NFR-REL-03 防线②底座）。"""
    assert runner_service().get("restart") == "always"


def test_runner_has_dedicated_cookie_volume() -> None:
    """独立 Cookie 卷：一账号一卷（/data/cookies），另配崩溃栈 spool 卷。"""
    volumes = [str(v) for v in runner_service().get("volumes", [])]
    assert any(v.startswith("runner01_cookies:") and v.endswith(":/data/cookies") for v in volumes)
    assert any("spool" in v for v in volumes)
    top_volumes = load_compose().get("volumes", {})
    assert "runner01_cookies" in top_volumes


def test_runner_proxy_environment() -> None:
    """出口代理环境变量三件套：HTTP_PROXY/HTTPS_PROXY 显式代理 + NO_PROXY 内网直连。"""
    env = env_map(runner_service())
    assert env.get("HTTP_PROXY") == "http://127.0.0.1:6152"
    assert env.get("HTTPS_PROXY") == "http://127.0.0.1:6152"
    no_proxy = env.get("NO_PROXY", "")
    for internal in ("postgres", "redis", "localhost"):
        assert internal in no_proxy


def test_runner_healthcheck_with_start_period() -> None:
    """业务健康检查：healthcheck 存在，含 start_period 恢复预算（RTO 预算覆盖）。"""
    hc = runner_service().get("healthcheck")
    assert isinstance(hc, dict) and hc.get("test")
    assert "start_period" in hc
    assert "interval" in hc and "retries" in hc


def test_runner_binds_proxy_egress_namespace() -> None:
    """出口绑定：network_mode 共享 proxy-egress-01 命名空间（全部流量强制走住宅 IP）。"""
    svc = runner_service()
    assert svc.get("network_mode") == "service:proxy-egress-01"
    services = load_compose().get("services", {})
    assert "proxy-egress-01" in services, "缺少出口容器定义"
    assert services["proxy-egress-01"].get("restart") == "always"


def test_compose_has_no_plaintext_credentials() -> None:
    """零凭证明文：代理接入串/口令一律 ${...} 占位（与 scan_secrets 门禁同口径）。"""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for banned in ("socks5://user:", "PROXY_PASS=", "password:"):
        assert banned not in text
    assert "${" in text  # 期望 IP / 端点等非凭证项走 .env 占位注入


def test_existing_compose_untouched() -> None:
    """既有 compose.yaml 不被本迭代改动：不含 agent-runner 服务（独立增量文件）。"""
    assert LEGACY_COMPOSE_PATH.is_file()
    legacy = yaml.safe_load(LEGACY_COMPOSE_PATH.read_text(encoding="utf-8"))
    assert "agent-runner-01" not in legacy.get("services", {})
