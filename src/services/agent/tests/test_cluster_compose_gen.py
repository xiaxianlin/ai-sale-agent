# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""28 实例 compose override 生成器与产物结构测试（FR-SC-001，SR-SC-001 §6.2/§6.8；Phase 4 TDD 阶段①）。

覆盖：
- 生成器单元：绑定清单 28 行（4 主 + 24 子）、单实例渲染三要素（容器名/Cookie 卷/代理 env）、
  宿主机密度 3/台反亲和摊开（§6.7）；
- 产物断言（deploy/compose/cluster.yaml，生成器产物）：可解析、版权头、28 服务齐备、
  独立卷一一对应、代理占位零明文凭证（NFR-SEC-04）、资源配额（§6.7）。

实现方案：docs/07-实现方案/SC/SR-SC-001-Agent集群.md §6.2、§6.7、§6.8
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tools.agent.gen_cluster_compose import (
    DEFAULT_COUNT,
    build_compose,
    instance_plan,
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
        """单实例渲染：容器名 agent-runner-NN、卷 cookie-NN、代理 env 占位注入（渲染即校验）。"""
        row = instance_plan(1)[0]
        svc = render_service(row)
        assert svc["container_name"] == "agent-runner-01"
        assert "cookie-01:/data/openclaw/cookies" in svc["volumes"]
        env = "|".join(svc["environment"])
        assert "ACCOUNT_ID=" in env and "PROXY_URL=" in env
        assert "<PROXY-USER>" in env and "<PROXY-PASS>" in env
    def test_density_three_per_host(self) -> None:
        """密度规划：每宿主机 3 容器（4C8G×3，§6.7），10 台摊开 28 实例。"""
        plan = instance_plan(DEFAULT_COUNT)
        hosts = {r["host_node"] for r in plan}
        assert len(hosts) == 10
        first_host = [r for r in plan if r["host_node"] == "runner-01"]
        assert len(first_host) == 3

    def test_build_compose_dumpable_with_metadata(self) -> None:
        """整包构建：x-cluster 元数据 + services/volumes 三段齐备，可 safe_load。"""
        data = yaml.safe_load(build_compose(2))
        assert data["x-cluster"]["instance_count"] == 2
        assert set(data["services"]) == {"agent-runner-01", "agent-runner-02"}
        assert set(data["volumes"]) == {"cookie-01", "cookie-02"}


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

    def test_artifact_has_28_services(self) -> None:
        """28 服务齐备且命名规范（agent-runner-01…28）。"""
        services = self.load()["services"]
        assert len(services) == 28
        assert set(services) == {f"agent-runner-{n:02d}" for n in range(1, 29)}

    def test_every_service_has_dedicated_cookie_volume(self) -> None:
        """独立 Cookie 空间：每服务挂 cookie-NN 且顶层卷一一对应（禁共享）。"""
        data = self.load()
        for n in range(1, 29):
            svc = data["services"][f"agent-runner-{n:02d}"]
            assert f"cookie-{n:02d}:/data/openclaw/cookies" in svc["volumes"]
            assert f"cookie-{n:02d}" in data["volumes"]

    def test_no_plaintext_credentials(self) -> None:
        """零凭证明文：代理接入串一律 <占位> 形态（scan_secrets 口径）。"""
        text = ARTIFACT.read_text(encoding="utf-8")
        assert "<PROXY-USER>:<PROXY-PASS>@" in text
        assert "<PROXY-IP-01>" in text  # 出口 IP 占位（现场调研后经 .env/secrets 注入）

    def test_resource_limits_and_restart_policy(self) -> None:
        """资源配额与自愈：单容器 1.0 CPU/2g、restart=unless-stopped（§6.2 模板）。"""
        services = self.load()["services"]
        svc = services["agent-runner-14"]
        assert svc["restart"] == "unless-stopped"
        assert svc["deploy"]["resources"]["limits"]["cpus"] == "1.0"
        assert svc["deploy"]["resources"]["limits"]["memory"] == "2g"
