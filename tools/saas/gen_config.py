# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户配置单一源生成器（FR-SAAS-004，SR-SAAS-004 §6.5）。

「双形态一套源」：Compose 与 Helm 共用 tenant-config.yaml 单一配置源，由本生成器
分别产出 ``.env``（Compose 形态）与 ``values-<租户>.yaml``（Helm 形态）。生成物首行
带配置源指纹（SHA256 截 16 位），部署前强制漂移校验（--target check）——手改生成物
即指纹不一致，退出码 2 禁止部署（防双源漂移硬门禁）。

新增可调参数只改 SCHEMA 与 ENV_MAP/VALUES_MAP，禁止手改生成物（SR §6.5）。

密钥供给（CR P0-5 修复）：配置源 ``secrets`` 段必填四个服务密钥（site/saas 令牌与
SDK、邮件回执入口），接入时经 ``generate_secret_values()`` 随机生成写入配置源，
由本生成器统一下发进 ``.env``（KB_SITE_TOKEN_SECRET/KB_SITE_SDK_KEY/
KB_MAIL_WEBHOOK_KEY/KB_SAAS_TOKEN_SECRET）——服务侧缺密钥即启动失败（fail-closed）。

用法（CLI）：
  python3 tools/saas/gen_config.py -c tenant.yaml --target compose --out .env
  python3 tools/saas/gen_config.py -c tenant.yaml --target helm   --out values-t001.yaml
  python3 tools/saas/gen_config.py -c tenant.yaml --target check  --out .env
退出码：0 成功/一致；2 schema 校验失败或漂移（SR §5 CLI 契约）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets as _secrets
import sys
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------- 映射与 schema

#: 配置源 → .env 键（SR §6.5 ENV_MAP，含依赖清单容量项）
ENV_MAP: dict[str, str] = {
    "meta.tenant_id": "TENANT_ID",
    "meta.plan": "TENANT_PLAN",
    "images.registry": "IMAGE_REGISTRY",
    "images.tag": "IMAGE_TAG",
    "ingress.domain": "PUBLIC_DOMAIN",
    "dependencies.postgresql.size_gb": "PG_SIZE_GB",
    "dependencies.milvus.size_gb": "MILVUS_SIZE_GB",
    "dependencies.redis.size_gb": "REDIS_SIZE_GB",
    # 服务密钥下发（CR P0-5 修复）：配置源 secrets 段 → .env；服务侧缺密钥即启动失败，
    # 本生成器负责「生成（generate_secret_values）与下发（render_env）」的供给链闭环
    "secrets.site_token_secret": "KB_SITE_TOKEN_SECRET",
    "secrets.site_sdk_key": "KB_SITE_SDK_KEY",
    "secrets.mail_webhook_key": "KB_MAIL_WEBHOOK_KEY",
    "secrets.saas_token_secret": "KB_SAAS_TOKEN_SECRET",
}

#: 密钥段 → 环境变量（CR P0-5 修复；与 ENV_MAP 的 secrets 子集同源，供测试/校验消费）
SECRET_ENV_KEYS: dict[str, str] = {
    "site_token_secret": "KB_SITE_TOKEN_SECRET",
    "site_sdk_key": "KB_SITE_SDK_KEY",
    "mail_webhook_key": "KB_MAIL_WEBHOOK_KEY",
    "saas_token_secret": "KB_SAAS_TOKEN_SECRET",
}

#: 配置源 → Helm values 路径（SR §6.5 VALUES_MAP；目标路径必须在 values.yaml 存在）
VALUES_MAP: dict[str, str] = {
    "images.tag": "image.tag",
    "ingress.domain": "ingress.domain",
    "ingress.tls_enabled": "ingress.tls.enabled",
    "apps.kbService.replicas": "apps.kbService.replicas",
    "apps.agentRunner.replicas": "apps.agentRunner.replicas",
    "brand.primaryColor": "brand.primaryColor",
    "brand.logoUrl": "brand.logoUrl",
    "dependencies.postgresql.size_gb": "postgresql.primary.persistence.size",
    "dependencies.milvus.size_gb": "milvus.persistence.size",
}

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: 数值域（闭区间；对齐 SR §6.4 values 全参数示例）
_INT_RANGES: dict[str, tuple[int, int]] = {
    "dependencies.postgresql.size_gb": (10, 2000),
    "dependencies.milvus.size_gb": (20, 4000),
    "dependencies.redis.size_gb": (5, 1000),
    "apps.kbService.replicas": (1, 20),
    "apps.agentRunner.replicas": (1, 28),  # 对齐 4 主 + 24 子账号上限（§2 阶段口径）
}

#: 必填字符串键（非空）
_REQUIRED_STR: tuple[str, ...] = ("meta.tenant_id", "meta.plan", "images.registry",
                                  "images.tag", "ingress.domain",
                                  # 服务密钥必填（CR P0-5 修复：拒绝无密钥配置出包）
                                  "secrets.site_token_secret", "secrets.site_sdk_key",
                                  "secrets.mail_webhook_key", "secrets.saas_token_secret")


def generate_secret_values() -> dict[str, str]:
    """生成四个服务密钥的随机值（CR P0-5 修复；接入新租户时写入 tenant-config.yaml
    的 secrets 段，再由 render_env 下发进 .env——生成一次、确定性渲染、指纹可对账）。

    密钥经 ``secrets.token_urlsafe(32)`` 高熵随机生成（非可预测占位）；重复调用
    产出互不相同，接入方生成后固化于配置源（单一源，禁止手改生成物）。
    """
    return {name: _secrets.token_urlsafe(32) for name in SECRET_ENV_KEYS}


class SchemaError(Exception):
    """源配置不合 schema（缺项/类型错/数值域外/禁用值，CLI 退出码 2）。"""


class DriftError(Exception):
    """生成物与配置源指纹不一致（CLI 退出码 2，禁止部署）。"""


# --------------------------------------------------------------------------- 工具


def get_path(cfg: dict, dotted: str) -> Any:
    """按点路径取值（缺失返回哨兵 _MISSING）。"""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


_MISSING: Any = object()


def set_path(cfg: dict, dotted: str, value: Any) -> None:
    """按点路径写值（中间层缺字典则补建）。"""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def fingerprint(cfg: dict) -> str:
    """规范化序列化取 SHA256 前 16 位——双源一致性对账凭据（SR §6.5）。"""
    canonical = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


# --------------------------------------------------------------------------- schema 校验


def schema_validate(cfg: dict) -> None:
    """出参前先过 schema：缺项/类型错/数值域外/禁 latest/色值即 SchemaError。"""
    for key in _REQUIRED_STR:
        value = get_path(cfg, key)
        if value is _MISSING:
            raise SchemaError(f"缺少必填键：{key}")
        if not isinstance(value, str) or not value.strip():
            raise SchemaError(f"{key} 须为非空字符串")
    tag = cfg["images"]["tag"]
    if tag == "latest" or tag.endswith(":latest") or "-latest" in tag:
        raise SchemaError(f"镜像 tag 禁止 latest（版本化不可变，SR §6.6）：{tag}")
    if not _DOMAIN_RE.match(str(cfg["ingress"]["domain"])):
        raise SchemaError(f"ingress.domain 域名格式非法：{cfg['ingress']['domain']}")
    for key, (low, high) in _INT_RANGES.items():
        value = get_path(cfg, key)
        if value is _MISSING:
            raise SchemaError(f"缺少必填键：{key}")
        if not isinstance(value, int) or isinstance(value, bool) or not (low <= value <= high):
            raise SchemaError(f"{key} 须为 {low}-{high} 整数，实为 {value!r}")
    color = get_path(cfg, "brand.primaryColor")
    if color is not _MISSING and not (isinstance(color, str) and _HEX_RE.match(color)):
        raise SchemaError(f"brand.primaryColor 须为 #RRGGBB：{color!r}")


# --------------------------------------------------------------------------- 渲染


def render_env(cfg: dict) -> str:
    """按 ENV_MAP 逐键映射为 KEY=VALUE 行（键序随 MAP，稳定可幂等）。"""
    lines: list[str] = []
    for source_key, env_key in ENV_MAP.items():
        value = get_path(cfg, source_key)
        if value is not _MISSING:
            lines.append(f"{env_key}={value}")
    return "\n".join(lines) + "\n"


def render_values(cfg: dict) -> str:
    """按 VALUES_MAP 生成租户覆写层（values-<租户>.yaml，叠加 Chart 默认 values）。"""
    overrides: dict[str, Any] = {}
    for source_key, target_path in VALUES_MAP.items():
        value = get_path(cfg, source_key)
        if value is not _MISSING:
            set_path(overrides, target_path, value)
    return yaml.safe_dump(overrides, allow_unicode=True, sort_keys=False)


def render(cfg: dict, target: str) -> str:
    """生成带指纹头的产物文本（target ∈ compose|helm）；出参前先过 schema。"""
    if target not in ("compose", "helm"):
        raise SchemaError(f"未知生成目标：{target}")
    schema_validate(cfg)
    body = render_env(cfg) if target == "compose" else render_values(cfg)
    header = (f"# fingerprint: {fingerprint(cfg)}  ← 本文件由 gen-config 生成，禁止手改"
              f"（源：tenant-config.yaml，目标：{target}）\n")
    return header + body


# --------------------------------------------------------------------------- 漂移校验


def check_drift(cfg: dict, path: str, target: str) -> None:
    """CI 与 bootstrap 第④步调用：生成物与配置源全文本比对，不一致即 DriftError。"""
    expected = render(cfg, target)
    actual = Path(path).read_text(encoding="utf-8")
    if actual != expected:
        expected_fp, actual_fp = fingerprint(cfg), expected.splitlines()[0]
        raise DriftError(
            f"配置漂移：{path} 与源不一致（期望 {expected_fp[:30]}…，"
            f"文件头 {actual.splitlines()[0][:30]}…），请重新生成，禁止部署")


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """CLI 契约（SR §5）：生成 compose|helm 或 check 漂移；0 成功，2 失败。"""
    parser = argparse.ArgumentParser(description="租户配置单一源 → .env / values 双目标生成器")
    parser.add_argument("-c", "--config", required=True, help="tenant-config.yaml 路径")
    parser.add_argument("--target", required=True, choices=("compose", "helm", "check"),
                        help="compose=.env / helm=values / check=漂移校验")
    parser.add_argument("--out", required=True, help="输出文件路径（check 为被校验文件）")
    args = parser.parse_args(argv)
    try:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise SchemaError(f"配置源须为 YAML 映射：{args.config}")
        if args.target == "check":
            target = "helm" if "values" in Path(args.out).name else "compose"
            check_drift(cfg, args.out, target)
        else:
            Path(args.out).write_text(render(cfg, args.target), encoding="utf-8")
    except (SchemaError, DriftError) as exc:
        print(f"[gen-config] 退出码 2：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # 直接执行入口（仓库根运行）
    raise SystemExit(main())
