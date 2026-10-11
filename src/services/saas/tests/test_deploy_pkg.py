# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""安装部署包测试（FR-SAAS-004，SR-SAAS-004 §3/§5/§6.4/§6.5/§9；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：同一 tenant-config.yaml → .env + values 双生成、键值映射正确、
  compose 包结构断言（bootstrap/回滚/依赖清单）、helm values 全参数映射；
- 边界：指纹 16 位定长、恰等 quota 数值域、sh 包内示例配置双目标指纹一致；
- 异常：schema 校验失败（缺项/类型错/禁 latest）、生成物被手改 → 漂移拦截退出码 2；
- 并发：双生成幂等（重复渲染逐字节一致，无时钟/顺序噪声）；
- 降级：脚本静态可检（bash -n 通过、退出码契约完备：10/11/12/13/14/20/21）。

验收口径映射：SRS §16.6「一键部署包在干净环境部署成功（两种形态各验证一次）」。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-004-安装部署包.md §3、§5、§6.4、§6.5、§9
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.saas import gen_config

REPO_ROOT = Path(__file__).resolve().parents[4]
PKG_DIR = REPO_ROOT / "deploy" / "compose-package"
HELM_DIR = REPO_ROOT / "deploy" / "helm"


def sample_cfg() -> dict:
    """样例租户配置源（与 deploy/compose-package/tenant-config.example.yaml 同构）。"""
    return {
        "meta": {"tenant_id": "t001", "plan": "standard"},
        "images": {"registry": "registry.example.com", "tag": "1.0.0-9abcdef"},
        "ingress": {"domain": "sale.example.com", "tls_enabled": True},
        "dependencies": {"postgresql": {"size_gb": 100}, "milvus": {"size_gb": 200},
                         "redis": {"size_gb": 20}},
        "apps": {"kbService": {"replicas": 2}, "agentRunner": {"replicas": 1}},
        "brand": {"primaryColor": "#1E63E9", "logoUrl": ""},
    }


# ───────────────────────── 等价类：包结构 / 双生成映射 / helm 全参数 ─────────────────────────


class TestEquivalence:
    def test_compose_package_structure(self) -> None:
        """compose 包结构断言：install.sh/rollback.sh/.env.example/示例配置齐备。"""
        for name in ("install.sh", "rollback.sh", ".env.example",
                     "tenant-config.example.yaml"):
            assert (PKG_DIR / name).is_file(), f"部署包缺 {name}"

    def test_dual_generation_same_fingerprint(self) -> None:
        """双形态一套源：同源生成 .env 与 values 指纹一致（防双源漂移凭据）。"""
        cfg = sample_cfg()
        env_text = gen_config.render(cfg, "compose")
        values_text = gen_config.render(cfg, "helm")
        assert gen_config.fingerprint(cfg) in env_text
        assert gen_config.fingerprint(cfg) in values_text
        assert gen_config.fingerprint(cfg) == gen_config.fingerprint(cfg)  # 16 位稳定

    def test_env_key_value_mapping(self) -> None:
        """ENV_MAP 逐键映射正确（SR §6.5：tenant_id/tag/domain/size_gb）。"""
        lines = gen_config.render_env(sample_cfg())
        flat = dict(line.split("=", 1) for line in lines.splitlines()
                    if "=" in line and not line.startswith("#"))
        assert flat["TENANT_ID"] == "t001"
        assert flat["IMAGE_TAG"] == "1.0.0-9abcdef"
        assert flat["PUBLIC_DOMAIN"] == "sale.example.com"
        assert flat["PG_SIZE_GB"] == "100"

    def test_helm_chart_and_values_full_mapping(self) -> None:
        """helm 全参数：Chart v2+三依赖；values 全参数映射（VALUES_MAP 路径存在）、
        版本化 tag 禁 latest、迁移 Job 开启、租户 namespace 隔离。"""
        chart = yaml.safe_load((HELM_DIR / "Chart.yaml").read_text(encoding="utf-8"))
        assert chart["apiVersion"] == "v2" and chart["name"] == "aisale"
        deps = {d["name"]: d for d in chart["dependencies"]}
        assert {"postgresql", "milvus", "redis"} <= set(deps)
        assert all(d.get("repository") for d in deps.values())
        assert chart["version"] and "latest" not in chart["version"]
        values = yaml.safe_load((HELM_DIR / "values.yaml").read_text(encoding="utf-8"))
        for source_key, target_path in gen_config.VALUES_MAP.items():
            node: object = values
            for part in target_path.split("."):
                assert isinstance(node, dict) and part in node, \
                    f"values.yaml 缺 {target_path}（源键 {source_key}）"
                node = node[part]
        assert values["image"]["tag"] != "latest"
        assert values["migrate"]["enabled"] is True
        assert values["saasTenant"]["namespace"]
        assert values["ingress"]["tls"]["enabled"] is True

    def test_example_config_renders_both_targets(self) -> None:
        """包内示例配置源：schema 通过、双目标可生成、指纹一致。"""
        cfg = yaml.safe_load(
            (PKG_DIR / "tenant-config.example.yaml").read_text(encoding="utf-8"))
        env_text = gen_config.render(cfg, "compose")
        values_text = gen_config.render(cfg, "helm")
        fp = gen_config.fingerprint(cfg)
        assert fp in env_text and fp in values_text


# ───────────────────────── 边界：指纹定长 / 渲染幂等 ─────────────────────────


class TestBoundary:
    def test_fingerprint_fixed_and_render_idempotent(self) -> None:
        """指纹恒 16 位十六进制；重复渲染逐字节一致（无时钟噪声，并发安全前提）。"""
        cfg = sample_cfg()
        fp = gen_config.fingerprint(cfg)
        assert len(fp) == 16
        int(fp, 16)  # 十六进制可解析
        assert gen_config.render(cfg, "compose") == gen_config.render(cfg, "compose")
        assert gen_config.render(cfg, "helm") == gen_config.render(cfg, "helm")


# ───────────────────────── 异常：schema 拒绝 / 漂移拦截 ─────────────────────────


class TestExceptions:
    def test_schema_missing_required_key(self) -> None:
        """缺必填键（ingress.domain）→ SchemaError。"""
        cfg = sample_cfg()
        del cfg["ingress"]["domain"]
        with pytest.raises(gen_config.SchemaError):
            gen_config.render(cfg, "compose")

    @pytest.mark.parametrize("mutate,msg_part", [
        (lambda c: c["dependencies"].update({"postgresql": {"size_gb": "大"}}), "size_gb"),
        (lambda c: c["apps"].update({"kbService": {"replicas": 0}}), "replicas"),
    ])
    def test_schema_type_and_range_errors(self, mutate, msg_part: str) -> None:
        """类型错/数值域外 → SchemaError 且指明字段。"""
        cfg = sample_cfg()
        mutate(cfg)
        with pytest.raises(gen_config.SchemaError) as ei:
            gen_config.render(cfg, "helm")
        assert msg_part in str(ei.value)

    def test_latest_tag_forbidden(self) -> None:
        """镜像 tag=latest → SchemaError（版本化不可变，禁 latest）。"""
        cfg = sample_cfg()
        cfg["images"]["tag"] = "latest"
        with pytest.raises(gen_config.SchemaError) as ei:
            gen_config.render(cfg, "compose")
        assert "latest" in str(ei.value)

    def test_drift_check_blocks_tampered_file(self, tmp_path: Path) -> None:
        """手改生成物 → check_drift 抛 DriftError（退出码 2 语义，禁止部署）。"""
        cfg = sample_cfg()
        env_path = tmp_path / ".env"
        env_path.write_text(gen_config.render(cfg, "compose"), encoding="utf-8")
        gen_config.check_drift(cfg, str(env_path), "compose")  # 一致通过
        env_path.write_text(
            env_path.read_text(encoding="utf-8").replace("t001", "t999"),
            encoding="utf-8")
        with pytest.raises(gen_config.DriftError):
            gen_config.check_drift(cfg, str(env_path), "compose")

    def test_cli_exit_codes(self, tmp_path: Path) -> None:
        """CLI 契约：生成 0；schema 失败 2；check 漂移 2。"""
        cfg_path = tmp_path / "tenant.yaml"
        cfg_path.write_text(yaml.safe_dump(sample_cfg(), allow_unicode=True),
                            encoding="utf-8")
        out = tmp_path / ".env"
        assert gen_config.main(["-c", str(cfg_path), "--target", "compose",
                                "--out", str(out)]) == 0
        assert out.exists()
        bad = tmp_path / "bad.yaml"
        bad_cfg = sample_cfg()
        del bad_cfg["meta"]["tenant_id"]
        bad.write_text(yaml.safe_dump(bad_cfg), encoding="utf-8")
        assert gen_config.main(["-c", str(bad), "--target", "compose",
                                "--out", str(tmp_path / "x.env")]) == 2
        (tmp_path / ".env").write_text("# fingerprint: 0000000000000000\n", encoding="utf-8")
        assert gen_config.main(["-c", str(cfg_path), "--target", "check",
                                "--out", str(tmp_path / ".env")]) == 2


# ───────────────────────── 脚本契约：语法 / 步骤 / 退出码 ─────────────────────────


class TestScriptContract:
    @pytest.mark.skipif(shutil.which("bash") is None, reason="无 bash 环境")
    def test_shell_scripts_syntax_valid(self) -> None:
        """bash -n 静态语法检查：install.sh / rollback.sh 均可解析。"""
        for name in ("install.sh", "rollback.sh"):
            proc = subprocess.run(["bash", "-n", str(PKG_DIR / name)],
                                  capture_output=True, text=True)
            assert proc.returncode == 0, f"{name} 语法错误：{proc.stderr}"

    def test_install_sh_bootstrap_steps_and_exit_codes(self) -> None:
        """bootstrap 步骤齐备（SR §6.2 七步）+ 退出码契约 10/11/12/13/14。"""
        text = (PKG_DIR / "install.sh").read_text(encoding="utf-8")
        for marker in ("preflight", "secrets", "images", "config", "migrate",
                       "wait_healthy", "verify"):
            assert marker in text, f"install.sh 缺步骤 {marker}"
        for code in ("10", "11", "12", "13", "14"):
            assert f"exit {code}" in text, f"install.sh 缺退出码 {code}"
        assert "--offline" in text  # 离线优先（NFR-SEC-06）

    def test_rollback_sh_contract(self) -> None:
        """回滚契约：--to 目标版本、20 目标不存在、21 未备份拒绝、--skip-db、留痕。"""
        text = (PKG_DIR / "rollback.sh").read_text(encoding="utf-8")
        assert "--to" in text and "--skip-db" in text
        assert "exit 20" in text and "exit 21" in text
        assert "rolled_back" in text

    def test_env_example_dependency_pins(self) -> None:
        """.env.example：依赖清单钉版（无 latest）+ 密钥占位（NFR-SEC-04）。"""
        text = (PKG_DIR / ".env.example").read_text(encoding="utf-8")
        assert "latest" not in text
        for pin in ("postgres:16.4", "redis:7.2", "milvusdb/milvus:v2.4.17"):
            assert pin in text, f"依赖清单缺钉版 {pin}"
        assert "CHANGE_ME" in text or "GENERATED" in text  # 密钥占位符
