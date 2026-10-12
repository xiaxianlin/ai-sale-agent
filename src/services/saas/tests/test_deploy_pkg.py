# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""安装部署包测试（FR-SAAS-004，SR-SAAS-004 §3/§5/§6.4/§6.5/§9；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：同一 tenant-config.yaml → .env + values 双生成、键值映射正确、
  compose 包结构断言（bootstrap/回滚/编排文件/依赖清单）、helm values 全参数映射；
- 边界：指纹 16 位定长、恰等 quota 数值域、sh 包内示例配置双目标指纹一致；
- 异常：schema 校验失败（缺项/类型错/禁 latest）、生成物被手改 → 漂移拦截退出码 2；
- 并发：双生成幂等（重复渲染逐字节一致，无时钟/顺序噪声）；
- 降级：脚本静态可检（bash -n 通过、退出码契约完备：10/11/12/13/14/20/21）。

CR A4/A6/P2 修复后新增断言（按新事实更新，不削弱）：
- 包内 compose.yaml 结构：app/site/nginx upstream 三方对齐、健康检查目标为
  实际存在的服务、凭证零明文（${}/secrets 形态）、数据层版本与单一来源一致；
- install/rollback 脚本：compose 调用一律 -f 指向包内 compose.yaml、迁移口径为
  migrations/ 裸 SQL 的 psql 幂等执行（无 alembic 引用）；
- helm templates/ 最小三类模板：无 helm 环境以 YAML 结构断言替代 helm template，
  values.yaml（除子图表段）叶子参数逐一被模板引用；
- .env.example：DeepSeek 键名与代码消费一致（KB_/TR_DRILL_ 前缀两个键）。

验收口径映射：SRS §16.6「一键部署包在干净环境部署成功（两种形态各验证一次）」。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-004-安装部署包.md §3、§5、§6.4、§6.5、§9
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.saas import gen_config

REPO_ROOT = Path(__file__).resolve().parents[4]
PKG_DIR = REPO_ROOT / "deploy" / "compose-package"
HELM_DIR = REPO_ROOT / "deploy" / "helm"
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "ai-sale.conf"
SOURCE_COMPOSE = REPO_ROOT / "deploy" / "compose" / "compose.yaml"


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
        "secrets": {"site_token_secret": "gen-demo-site-token-secret-0001",
                    "site_sdk_key": "gen-demo-site-sdk-key-0001",
                    "mail_webhook_key": "gen-demo-mail-webhook-key-0001",
                    "saas_token_secret": "gen-demo-saas-token-secret-0001"},
    }


# ───────────────────────── 等价类：包结构 / 双生成映射 / helm 全参数 ─────────────────────────


class TestEquivalence:
    def test_compose_package_structure(self) -> None:
        """compose 包结构断言：install.sh/rollback.sh/.env.example/compose.yaml/
        示例配置齐备（CR A4 修复：包内补真实编排文件，-f 断链消除）。"""
        for name in ("install.sh", "rollback.sh", ".env.example", "compose.yaml",
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


class TestSecretProvisioning:
    """密钥下发（CR P0-5 修复）：四个服务密钥经配置源 secrets 段进入 .env。"""

    def test_env_renders_four_service_secret_keys(self) -> None:
        """.env 增补下发：KB_SITE_TOKEN_SECRET/KB_SITE_SDK_KEY/KB_MAIL_WEBHOOK_KEY/
        KB_SAAS_TOKEN_SECRET 四键非空（服务侧缺密钥即启动失败的配套供给链）。"""
        lines = gen_config.render_env(sample_cfg())
        flat = dict(line.split("=", 1) for line in lines.splitlines()
                    if "=" in line and not line.startswith("#"))
        for env_key in gen_config.SECRET_ENV_KEYS.values():
            assert flat.get(env_key), f".env 缺密钥下发 {env_key}"

    def test_missing_secrets_schema_error(self) -> None:
        """异常：配置源缺 secrets 段 → SchemaError（密钥必填，拒绝无密钥配置出包）。"""
        cfg = sample_cfg()
        del cfg["secrets"]
        with pytest.raises(gen_config.SchemaError) as ei:
            gen_config.render(cfg, "compose")
        assert "secrets." in str(ei.value)

    def test_generate_secret_values_random_and_distinct(self) -> None:
        """生成器：四键齐备、非空且两次生成互不相同（高熵随机，非可预测占位）。"""
        first = gen_config.generate_secret_values()
        second = gen_config.generate_secret_values()
        assert set(first) == {"site_token_secret", "site_sdk_key",
                              "mail_webhook_key", "saas_token_secret"}
        assert all(first.values()) and first != second


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

    def test_install_sh_compose_invocations_use_f(self) -> None:
        """CR A4：全部 docker compose 调用经 -f 指向包内 compose.yaml
        （统一 dc() 入口），不再出现无 -f 的 --env-file 裸调用（三断链之一消除）。"""
        text = (PKG_DIR / "install.sh").read_text(encoding="utf-8")
        assert 'COMPOSE_FILE="${PKG_DIR}/compose.yaml"' in text
        assert "docker compose -f" in text            # dc() 统一入口带 -f
        assert "docker compose --env-file" not in text  # 无 -f 裸调用残留

    def test_install_sh_migrate_is_bare_sql_psql(self) -> None:
        """CR A4：迁移口径 = migrations/ 裸 SQL 的 psql 幂等执行（ON_ERROR_STOP），
        全文无 alembic 引用（仓库无 alembic 迁移链，此前属断链）。"""
        text = (PKG_DIR / "install.sh").read_text(encoding="utf-8")
        assert "alembic" not in text
        assert "migrations" in text and "psql" in text
        assert "ON_ERROR_STOP=1" in text              # 单文件失败即中断（非静默跳过）
        assert "pg_dump" in text                      # 迁移前快照（NFR-REL-04）

    def test_rollback_sh_contract(self) -> None:
        """回滚契约：--to 目标版本、20 目标不存在、21 未备份拒绝、--skip-db、留痕；
        CR A4：compose 调用带 -f、DB 回退为快照恢复（无 alembic downgrade）。"""
        text = (PKG_DIR / "rollback.sh").read_text(encoding="utf-8")
        assert "--to" in text and "--skip-db" in text
        assert "exit 20" in text and "exit 21" in text
        assert "rolled_back" in text
        assert "docker compose -f" in text
        assert "docker compose --env-file" not in text
        assert "alembic" not in text

    def test_env_example_dependency_pins(self) -> None:
        """CR P2：.env.example 依赖钉版与单一来源 deploy/compose/compose.yaml 一致
        （postgres:16 / redis:7 / milvus v2.5.6 / TEI cpu-1.8.0，无 latest）+
        密钥占位（NFR-SEC-04）+ DeepSeek 键名与代码消费一致（CR A6-b）。"""
        text = (PKG_DIR / ".env.example").read_text(encoding="utf-8")
        source = SOURCE_COMPOSE.read_text(encoding="utf-8")
        assert "latest" not in text
        # 单一来源声明在案（防再漂移的双源教训固化）
        assert "deploy/compose/compose.yaml" in text and "单一来源" in text
        for pin in ("postgres:16", "redis:7", "milvusdb/milvus:v2.5.6",
                    "text-embeddings-inference:cpu-1.8.0"):
            assert pin in text, f"依赖清单缺钉版 {pin}"
            assert pin in source, f"钉版 {pin} 与单一来源 compose.yaml 不一致"
        # 漂移旧值不得残留（16.4 / 7.2 / v2.4.17 为漂移前版本）
        for drifted in ("postgres:16.4", "redis:7.2", "milvusdb/milvus:v2.4.17"):
            assert drifted not in text
        # DeepSeek 键名与代码消费一致（brain/intent.py、tutor/llm.py、drill/scoring.py）
        assert "DEEPSEEK_API_KEY=CHANGE_ME" not in re.sub(
            r"(?:KB_|TR_DRILL_)DEEPSEEK_API_KEY", "OK", text)  # 无裸 DEEPSEEK_API_KEY
        assert "KB_DEEPSEEK_API_KEY=CHANGE_ME_CUSTOMER_PROVIDED" in text
        assert "TR_DRILL_DEEPSEEK_API_KEY=CHANGE_ME_CUSTOMER_PROVIDED" in text
        assert "CHANGE_ME" in text or "GENERATED" in text  # 密钥占位符


class TestPackageCompose:
    """CR A4：包内 compose.yaml 结构断言（-f 目标真实存在且三方对齐）。"""

    def load_pkg_compose(self) -> dict:
        """解析包内编排文件（缺失即失败——install.sh 的 -f 目标必须随包交付）。"""
        path = PKG_DIR / "compose.yaml"
        assert path.is_file(), f"缺包内编排文件：{path}"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    def test_app_and_site_services_defined(self) -> None:
        """app（uvicorn services.kb.app）/ site 服务齐备：nginx upstream 与
        install.sh 操作目标均有真实服务定义（断链消除）。"""
        data = self.load_pkg_compose()
        services = data["services"]
        assert {"postgres", "redis", "etcd", "milvus", "app", "site"} <= set(services)
        app_cmd = " ".join(str(c) for c in services["app"]["command"])
        site_cmd = " ".join(str(c) for c in services["site"]["command"])
        assert "uvicorn" in app_cmd and "services.kb.app" in app_cmd
        assert "uvicorn" in site_cmd and "services.site.api.app" in site_cmd
        # app 镜像 tag 取 .env（版本化不可变口径由 gen-config 指纹守卫）
        assert "${IMAGE_TAG" in services["app"]["image"]

    def test_healthcheck_targets_actually_exist(self) -> None:
        """健康检查目标 = 实际存在且带 healthcheck 的常驻服务
        （postgres/redis/etcd/milvus/app/site 共 6 个；tei 为可选 profile）；
        与 install.sh 第⑥步的等待清单注释一致（6/6）。"""
        data = self.load_pkg_compose()
        with_hc = {name for name, svc in data["services"].items()
                   if isinstance(svc.get("healthcheck"), dict) and not svc.get("profiles")}
        assert with_hc == {"postgres", "redis", "etcd", "milvus", "app", "site"}
        assert data["services"]["tei"].get("profiles") == ["tei"]  # 可选不计入
        install = (PKG_DIR / "install.sh").read_text(encoding="utf-8")
        assert "postgres/redis/etcd/milvus/app/site" in install

    def test_credentials_all_placeholder_or_secrets(self) -> None:
        """凭证零明文（NFR-SEC-04）：PG/Redis 口令走 Docker Secrets 文件
        （bootstrap 第②步生成 .secrets/），应用层密钥一律 ${} 占位注入。"""
        data = self.load_pkg_compose()
        text = (PKG_DIR / "compose.yaml").read_text(encoding="utf-8")
        pg_env = data["services"]["postgres"]["environment"]
        assert pg_env.get("POSTGRES_PASSWORD_FILE") == "/run/secrets/pg_password"
        assert "/run/secrets/redis_password" in data["services"]["redis"]["command"]
        assert set(data.get("secrets", {})) == {"pg_password", "redis_password", "jwt_salt"}
        for secret_name, decl in data["secrets"].items():
            assert decl["file"].startswith("./.secrets/")  # bootstrap 产物目录
        for banned in ("kb_local_dev", "minioadmin", "password: changeme"):
            assert banned not in text
        assert "${" in text  # 应用层密钥 ${} 占位

    def test_data_layer_images_match_single_source(self) -> None:
        """CR P2：包内数据层镜像与单一来源 deploy/compose/compose.yaml 逐一致
        （防第三版本源出现）。"""
        pkg = self.load_pkg_compose()["services"]
        src = yaml.safe_load(SOURCE_COMPOSE.read_text(encoding="utf-8"))["services"]
        for name in ("postgres", "redis", "etcd", "milvus", "tei"):
            assert pkg[name]["image"] == src[name]["image"], \
                f"{name} 镜像与单一来源不一致：{pkg[name]['image']} vs {src[name]['image']}"

    def test_nginx_upstream_cross_reference(self) -> None:
        """CR A4-b：nginx upstream（app:8000 / site:8000）与包内同名服务对齐，
        且 conf 注释指明服务定义位置（demo 单机形态拓扑口径统一）。"""
        conf = NGINX_CONF.read_text(encoding="utf-8")
        assert "deploy/compose-package/compose.yaml" in conf
        data = self.load_pkg_compose()
        for upstream_host in ("app", "site"):
            assert f"server {upstream_host}:8000;" in conf
            assert upstream_host in data["services"]


class TestHelmTemplates:
    """CR A4-c：helm templates/ 最小三类模板（deployment/service/configmap）。

    本机无 helm 二进制 → 以「占位替换后 YAML 结构断言」替代 helm template：
    ① 三文件齐备且多文档均可解析、Kind 正确；② values.yaml（除子图表段）的
    叶子参数逐一被模板引用（全参数化，防 values 与模板漂移）。
    """

    TPL_DIR = HELM_DIR / "templates"
    #: 由子图表（Chart.yaml dependencies）提供的段，不要求本模板引用
    SUBCHART_SECTIONS = {"postgresql", "milvus", "redis"}

    def tpl_text(self) -> str:
        """拼接三类模板全文（缺失即失败）。"""
        parts: list[str] = []
        for name in ("deployment.yaml", "service.yaml", "configmap.yaml"):
            path = self.TPL_DIR / name
            assert path.is_file(), f"缺 helm 模板：{path}"
            parts.append(path.read_text(encoding="utf-8"))
        return "\n".join(parts)

    @staticmethod
    def _structural_render(text: str) -> str:
        """无 helm 环境的结构渲染：{{ ... }} 一律替换为占位标量 X。

        模板刻意保持最小无控制流（{{- if }}/range 等不出现），故替换后即为
        合法 YAML——结构可解析即等价 helm template 的基本 sanity。
        """
        assert "{{- if" not in text and "{{- range" not in text  # 模板纪律自检
        return re.sub(r"\{\{[^{}]*\}\}", "X", text)

    def test_minimal_templates_structure(self) -> None:
        """三类模板齐备：deployment（4 个 Deployment）/ service（Service）/
        configmap（ConfigMap），占位渲染后全部可 safe_load、Kind 与名称规范。"""
        kinds: list[str] = []
        for name in ("deployment.yaml", "service.yaml", "configmap.yaml"):
            rendered = self._structural_render(
                (self.TPL_DIR / name).read_text(encoding="utf-8"))
            for doc in rendered.split("\n---\n"):
                if not doc.strip():
                    continue
                parsed = yaml.safe_load(doc)  # 不可解析即失败（helm template 替代口径）
                assert isinstance(parsed, dict), f"{name} 含非映射文档"
                assert parsed.get("apiVersion") and parsed.get("kind"), f"{name} 缺 Kind"
                kinds.append(parsed["kind"])
                assert parsed["metadata"]["name"], f"{name} 缺 metadata.name"
                assert parsed["metadata"]["namespace"]  # 租户 namespace 隔离
        assert kinds.count("Deployment") == 4  # kb / agent-core / agent-runner / saas
        assert kinds.count("Service") == 2
        assert kinds.count("ConfigMap") == 1

    def test_values_leaves_all_referenced(self) -> None:
        """全参数引用：values.yaml 除子图表段外每个叶子路径都被某模板引用
        （.Values.<path> 出现），防「values 有参、模板不消费」的参数漂移。"""
        values = yaml.safe_load((HELM_DIR / "values.yaml").read_text(encoding="utf-8"))
        leaves: list[str] = []

        def walk(node: dict, prefix: str) -> None:
            for key, child in node.items():
                dotted = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(child, dict):
                    walk(child, dotted)
                else:
                    leaves.append(dotted)

        walk({k: v for k, v in values.items() if k not in self.SUBCHART_SECTIONS}, "")
        text = self.tpl_text()
        missing = [path for path in leaves if f".Values.{path} " not in text
                   and f".Values.{path}}}" not in text]
        assert not missing, f"values 叶子参数未被模板引用：{missing}"

    @pytest.mark.skipif(shutil.which("helm") is None, reason="本机无 helm（以结构断言替代）")
    def test_helm_template_renders(self) -> None:
        """本机装有 helm 时跑真实渲染（结构性断言的增强复核，非替代）。"""
        proc = subprocess.run(
            ["helm", "template", "aisale", str(HELM_DIR)],
            capture_output=True, text=True)
        assert proc.returncode == 0, f"helm template 失败：{proc.stderr}"
        assert "kind: Deployment" in proc.stdout
