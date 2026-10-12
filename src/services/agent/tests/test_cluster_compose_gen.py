# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""28 实例 compose override 生成器与产物结构测试（FR-SC-001，SR-SC-001 §6.2/§6.8；Phase 4 TDD 阶段①）。

覆盖（CR A5 修复后口径：与 deploy/compose/agent-runner.yaml 同款双容器拓扑）：
- 生成器单元：绑定清单 28 行（4 主 + 24 子）、单实例渲染（容器名/Cookie 卷/
  网络绑定 proxy-egress-NN/secrets 挂载/健康检查）、宿主机密度 3/台反亲和摊开（§6.7）；
- 产物断言（deploy/compose/cluster.yaml，生成器产物）：可解析、版权头、
  28 组双容器（proxy-egress-NN + agent-runner-NN）齐备、独立卷一一对应、
  代理占位零明文凭证（NFR-SEC-04）、资源配额与 restart: always（§6.7）。

实现方案：docs/07-实现方案/SC/SR-SC-001-Agent集群.md §6.2、§6.7、§6.8
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tools.agent.gen_cluster_compose import (
    DEFAULT_COUNT,
    build_compose,
    instance_plan,
    render_egress,
    render_service,
)

REPO = Path(__file__).resolve().parents[4]
ARTIFACT = REPO / "deploy" / "compose" / "cluster.yaml"


# ───────────────────────── 生成器单元 ─────────────────────────


class TestGeneratorUnit:
    """gen_cluster_compose 生成器单元（NFR-SCA-01：N 数据驱动）。"""

    def test_instance_plan_has_28_rows(self) -> None:
        """绑定清单：28 行、序号 01-28、账号唯一、一账号一 IP。"""
        plan = instance_plan(DEFAULT_COUNT)
        assert len(plan) == 28
        assert [r["seq"] for r in plan] == list(range(1, 29))
        assert len({r["account_id"] for r in plan}) == 28
        assert len({r["proxy_ip"] for r in plan}) == 28

    def test_instance_plan_main_sub_split(self) -> None:
        """账号形态：4 主账号 + 24 子账号（SRS §11.6 口径 4+24=28）。"""
        plan = instance_plan(DEFAULT_COUNT)
        types = [r["account_type"] for r in plan]
        assert types.count("main") == 4
        assert types.count("sub") == 24

    def test_render_service_triple_binding(self) -> None:
        """单实例 runner 渲染：容器名 agent-runner-NN、卷 cookie-NN、账号 env 注入，
        且网络绑定 service:proxy-egress-NN、健康检查与 restart: always 齐备。"""
        row = instance_plan(1)[0]
        svc = render_service(row)
        assert svc["container_name"] == "agent-runner-01"
        assert "cookie-01:/data/cookies" in svc["volumes"]
        assert "spool-01:/data/spool" in svc["volumes"]
        env = "|".join(svc["environment"])
        assert "ACCOUNT_ID=" in env and "ACCOUNT_TYPE=" in env
        assert "HTTP_PROXY=" in env and "HTTPS_PROXY=" in env  # 双保险显式代理
        assert "KB_AGENT_MASTER_KEY_FILE=/run/secrets/vault_master_key" in env
        assert svc["network_mode"] == "service:proxy-egress-01"  # 出口命名空间绑定
        assert svc["restart"] == "always"
        assert svc["depends_on"] == ["proxy-egress-01"]
        hc = svc["healthcheck"]
        assert hc.get("test") and "start_period" in hc  # 业务探针 + 恢复预算

    def test_render_egress_secrets_and_ip_placeholder(self) -> None:
        """单实例出口容器渲染：secrets 挂载代理接入串、出口 IP <占位>、
        出口自检健康检查、NET_ADMIN 最小权限、restart: always（CR A5 同款拓扑）。"""
        row = instance_plan(2)[1]
        egress = render_egress(row)
        assert egress["secrets"] == ["proxy_url_02"]          # 凭证走 secrets 零明文
        assert egress["environment"] == ["EXPECTED_EGRESS_IP=<PROXY-IP-02>"]
        assert egress["cap_add"] == ["NET_ADMIN"]
        assert egress["restart"] == "always"
        assert egress["healthcheck"]["test"] == ["CMD-SHELL", "sh /app/check-egress-ip.sh"]

    def test_density_three_per_host(self) -> None:
        """密度规划：每宿主机 3 容器（4C8G×3，§6.7），10 台摊开 28 实例。"""
        plan = instance_plan(DEFAULT_COUNT)
        hosts = {r["host_node"] for r in plan}
        assert len(hosts) == 10
        first_host = [r for r in plan if r["host_node"] == "runner-01"]
        assert len(first_host) == 3

    def test_build_compose_dumpable_with_metadata(self) -> None:
        """整包构建：x-cluster 元数据 + services/volumes/secrets 齐备，可 safe_load。"""
        data = yaml.safe_load(build_compose(2))
        assert data["x-cluster"]["instance_count"] == 2
        assert set(data["services"]) == {"proxy-egress-01", "agent-runner-01",
                                         "proxy-egress-02", "agent-runner-02"}
        assert set(data["volumes"]) == {"cookie-01", "spool-01", "cookie-02", "spool-02"}
        assert set(data["secrets"]) == {"proxy_url_01", "proxy_url_02", "vault_master_key"}
        # secrets 文件路径不入 Git 的占位形态（现场注入）
        assert data["secrets"]["proxy_url_01"] == {"file": "./secrets/proxy_url_01.txt"}


# ───────────────────────── 产物断言 ─────────────────────────


class TestGeneratedArtifact:
    """deploy/compose/cluster.yaml 产物结构（与既有 compose 并列、不改既有文件）。"""

    def load(self) -> dict:
        """解析产物（缺失即失败——交付物必须由生成器产出后入库）。"""
        assert ARTIFACT.is_file(), f"缺少生成器产物：{ARTIFACT}（运行 tools/agent/gen_cluster_compose.py）"
        text = ARTIFACT.read_text(encoding="utf-8")
        assert any("知识产权归甲方所有" in line for line in text.splitlines()[:6])
        data = yaml.safe_load(text)
        assert isinstance(data, dict)
        return data

    def test_artifact_has_28_service_pairs(self) -> None:
        """28 组双容器齐备且命名规范（proxy-egress-01…28 + agent-runner-01…28）。"""
        services = self.load()["services"]
        assert len(services) == 56
        assert set(services) == (
            {f"proxy-egress-{n:02d}" for n in range(1, 29)}
            | {f"agent-runner-{n:02d}" for n in range(1, 29)}
        )

    def test_every_runner_binds_egress_and_dedicated_volumes(self) -> None:
        """出口绑定与独立 Cookie 空间：每 runner 挂 cookie-NN+spool-NN 卷且
        network_mode 绑定本账号 proxy-egress-NN，顶层卷一一对应（禁共享）。"""
        data = self.load()
        for n in range(1, 29):
            svc = data["services"][f"agent-runner-{n:02d}"]
            assert f"cookie-{n:02d}:/data/cookies" in svc["volumes"]
            assert f"spool-{n:02d}:/data/spool" in svc["volumes"]
            assert f"cookie-{n:02d}" in data["volumes"]
            assert f"spool-{n:02d}" in data["volumes"]
            assert svc["network_mode"] == f"service:proxy-egress-{n:02d}"
            assert svc["depends_on"] == [f"proxy-egress-{n:02d}"]
            assert isinstance(svc.get("healthcheck"), dict)  # 业务探针必在
            egress = data["services"][f"proxy-egress-{n:02d}"]
            assert egress["secrets"] == [f"proxy_url_{n:02d}"]  # 凭证 secrets 挂载
            assert egress["restart"] == "always"
            assert isinstance(egress.get("healthcheck"), dict)  # 出口自检必在
            assert f"proxy_url_{n:02d}" in data["secrets"]
        assert "vault_master_key" in data["secrets"]  # 主密钥文件（vault _FILE 通道）

    def test_no_plaintext_credentials(self) -> None:
        """零凭证明文：出口 IP/代理接入串一律 <占位> 形态（scan_secrets 口径）。"""
        text = ARTIFACT.read_text(encoding="utf-8")
        assert "<PROXY-USER>:<PROXY-PASS>@" in text  # 占位模板仅存于元数据说明
        assert "<PROXY-IP-01>" in text  # 出口 IP 占位（现场调研后经 .env/secrets 注入）
        for banned in ("PROXY_PASS=", "password:", "socks5://user:"):
            assert banned not in text, f"出现明文凭证形态：{banned}"

    def test_resource_limits_and_restart_policy(self) -> None:
        """资源配额与自愈：单容器 1.0 CPU/2g、restart=always（§6.2 模板，
        CR A5 对齐 agent-runner.yaml 口径）。"""
        services = self.load()["services"]
        svc = services["agent-runner-14"]
        assert svc["restart"] == "always"
        assert svc["deploy"]["resources"]["limits"]["cpus"] == "1.0"
        assert svc["deploy"]["resources"]["limits"]["memory"] == "2g"
        assert services["proxy-egress-14"]["restart"] == "always"
