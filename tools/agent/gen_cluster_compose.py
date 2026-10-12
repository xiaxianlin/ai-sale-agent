# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""28 实例 compose override 生成器（FR-SC-001，SR-SC-001 §6.2/§6.7/§6.8）。

职责（NFR-SCA-01：N 数据驱动——实例数是参数不是代码）：
- **绑定清单**（instance_plan）：seq 01-28 ↔ account（4 主 + 24 子，SRS §11.6 口径）
  ↔ 独立 proxy_ip（一账号一 IP，R-04）↔ 宿主机按 3 容器/台反亲和摊开（§6.7 密度）；
- **单实例渲染**（CR A5 修复：与 deploy/compose/agent-runner.yaml 同款双容器拓扑）：
  每账号一组「proxy-egress-NN（出口容器，secrets 挂载代理接入串 + 出口 IP 自检）
  + agent-runner-NN（network_mode: service:proxy-egress-NN 共享出口命名空间、
  独立 Cookie/spool 卷、业务健康检查、restart: always）」——凭证与出口 IP 一律
  <占位> 形态（NFR-SEC-04 零明文，现场调研后经 .env/Docker secrets 注入）；
- **整包构建**（build_compose）：x-cluster 元数据 + services/volumes/secrets 三段
  齐备，可 yaml.safe_load；产物落 deploy/compose/cluster.yaml（与既有 compose 并列，
  不改既有文件），入库前须由本生成器产出（生成物可复现，禁手改）。

用法（仓库根）：
  python3 tools/agent/gen_cluster_compose.py [--count 28] [--out deploy/compose/cluster.yaml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# 仓库根自举：直接以文件路径执行时补齐 tools 包导入路径
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 默认实例数（SRS §11.6 容量基线：4 主账号 + 24 子账号 = 28）
DEFAULT_COUNT = 28

#: 每宿主机容器密度（§6.7：4C8G×3，10 台摊开 28 实例）
HOST_DENSITY = 3

#: 主账号数（清单前 4 个序号为主账号形态，OI-06 口径）
MAIN_ACCOUNT_COUNT = 4

#: runner 镜像（版本化 tag，回滚依据）
RUNNER_IMAGE = "ai-sale/openclaw-runner:0.1.0"

#: 出口容器镜像（与 deploy/compose/agent-runner.yaml 同款，CR A5 拓扑统一）
EGRESS_IMAGE = "ai-sale/proxy-egress:0.1.0"

#: 版权头（产物前 6 行内含 COM-IP-01 声明，扫描口径同 scan_secrets）
HEADER_LINES = (
    "# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。",
    "# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。",
    "# 本文件由 tools/agent/gen_cluster_compose.py 生成，请勿手改；重新生成命令：",
    "#   python3 tools/agent/gen_cluster_compose.py",
)

#: 代理接入占位模板（零明文：凭证/出口 IP/端口现场经 .env/Docker secrets 注入；
#: 仅出现于 x-cluster 元数据作形态说明，不进入任何服务 environment）
PROXY_URL_TEMPLATE = "http://<PROXY-USER>:<PROXY-PASS>@<PROXY-IP-{seq:02d}>:<PORT>"


def instance_plan(count: int = DEFAULT_COUNT) -> list[dict]:
    """绑定清单：seq/account/proxy_ip/host_node 一一对应（4 主 + 24 子、一账号一 IP）。"""
    rows: list[dict] = []
    for seq in range(1, count + 1):
        rows.append({
            "seq": seq,
            "account_id": 7000 + seq,
            "account_type": "main" if seq <= MAIN_ACCOUNT_COUNT else "sub",
            "proxy_ip": f"10.8.{seq // 256}.{seq % 256}",  # 每实例独立出口 IP（规划值）
            "host_node": f"runner-{((seq - 1) // HOST_DENSITY) + 1:02d}",
            "container_name": f"agent-runner-{seq:02d}",
            "cookie_volume": f"cookie-{seq:02d}",
            "spool_volume": f"spool-{seq:02d}",
            "egress_service": f"proxy-egress-{seq:02d}",
            "proxy_secret": f"proxy_url_{seq:02d}",
        })
    return rows


def render_egress(row: dict) -> dict:
    """单实例出口容器渲染（CR A5 修复：与 agent-runner.yaml 同款拓扑）。

    要素：secrets 挂载代理接入串（不落 env 不入 Git）+ 期望出口 IP 占位 +
    出口 IP 自检健康检查（漂移即告警暂停）+ NET_ADMIN 最小权限 + restart: always。
    """
    seq = row["seq"]
    return {
        "restart": "always",
        "image": EGRESS_IMAGE,
        "cap_add": ["NET_ADMIN"],                       # 仅为本命名空间配置 NAT 规则
        "environment": [
            # 期望出口 IP（<占位>，非凭证；现场经 .env/secrets 注入）
            f"EXPECTED_EGRESS_IP=<PROXY-IP-{seq:02d}>",
        ],
        "secrets": [row["proxy_secret"]],               # 代理接入串：secrets 注入零明文
        "healthcheck": {
            "test": ["CMD-SHELL", "sh /app/check-egress-ip.sh"],
            "interval": "30s",
            "timeout": "10s",
            "retries": 3,
        },
    }


def render_service(row: dict) -> dict:
    """单实例 runner 渲染：容器名/独立卷/网络绑定/健康检查/资源配额（渲染即校验）。

    出口绑定双重强制：network_mode 共享 proxy-egress-NN 命名空间（透明代理）+
    HTTP(S)_PROXY 显式代理（双保险）；主密钥经 secrets 文件注入（vault _FILE 分支）。
    """
    seq = row["seq"]
    return {
        "restart": "always",                            # 崩溃退出自动拉起（NFR-REL-03）
        "image": RUNNER_IMAGE,
        "container_name": f"agent-runner-{seq:02d}",
        "network_mode": f"service:{row['egress_service']}",  # 全部流量强制走本账号出口
        "environment": [
            f"ACCOUNT_ID={row['account_id']}",
            f"ACCOUNT_TYPE={row['account_type']}",
            f"HOST_NODE={row['host_node']}",
            "HTTP_PROXY=http://127.0.0.1:6152",         # 应用层显式代理（双保险）
            "HTTPS_PROXY=http://127.0.0.1:6152",
            "NO_PROXY=localhost,127.0.0.1,app,postgres,redis,milvus,tei,nginx",
            "KB_OPENCLAW_ENDPOINT=http://127.0.0.1:9223",
            # 主密钥经 secrets 文件注入（CR A6：vault 支持 KB_AGENT_MASTER_KEY_FILE）
            "KB_AGENT_MASTER_KEY_FILE=/run/secrets/vault_master_key",
        ],
        "volumes": [
            f"{row['cookie_volume']}:/data/cookies",   # 独立 Cookie 卷（一账号一卷）
            f"{row['spool_volume']}:/data/spool",      # 崩溃上下文卷（错误栈落盘）
        ],
        "secrets": ["vault_master_key"],                # 主密钥文件挂载（_FILE 通道消费）
        "depends_on": [row["egress_service"]],
        "healthcheck": {                                # 业务探针（L2 僵死检测）
            "test": ["CMD", "curl", "-fsS", "http://127.0.0.1:9223/healthz"],
            "interval": "15s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "90s",                      # 恢复协议预算（RTO <2min）
        },
        "labels": ["autoheal=true"],                    # unhealthy → autoheal 旁路重启
        "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "2g"}}},
    }


def build_compose(count: int = DEFAULT_COUNT) -> str:
    """整包构建：x-cluster 元数据 + services/volumes/secrets，yaml 文本（可复核）。"""
    plan = instance_plan(count)
    services: dict[str, dict] = {}
    for row in plan:  # 每账号一组：先出口容器后 runner（depends_on 指向已定义服务）
        services[row["egress_service"]] = render_egress(row)
        services[f"agent-runner-{row['seq']:02d}"] = render_service(row)
    document = {
        "x-cluster": {
            "instance_count": count,
            "topology": "proxy-egress-NN + agent-runner-NN 双容器/账号"
                        "（CR A5 修复：与 deploy/compose/agent-runner.yaml 同款）",
            "account_split": {
                "main": min(count, MAIN_ACCOUNT_COUNT),
                "sub": max(0, count - MAIN_ACCOUNT_COUNT),
            },
            "host_density": HOST_DENSITY,
            "hosts": sorted({row["host_node"] for row in plan}),
            "proxy_note": "出口 IP 与凭证均为占位（NFR-SEC-04），现场调研后经 .env/Docker secrets 注入",
            "proxy_url_template": PROXY_URL_TEMPLATE.format(seq=1),
            "generated_by": "tools/agent/gen_cluster_compose.py",
        },
        "services": services,
        "volumes": {
            **{row["cookie_volume"]: {} for row in plan},
            **{row["spool_volume"]: {} for row in plan},
        },
        "secrets": {
            **{row["proxy_secret"]: {"file": f"./secrets/{row['proxy_secret']}.txt"}
               for row in plan},
            "vault_master_key": {"file": "./secrets/vault_master_key.txt"},
        },
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120)


def main(argv: list[str] | None = None) -> int:
    """生成产物 deploy/compose/cluster.yaml（版权头 + yaml 正文，交付物入库形态）。"""
    parser = argparse.ArgumentParser(description="28 实例 compose override 生成器")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help="实例数（默认 28）")
    parser.add_argument("--out", default="deploy/compose/cluster.yaml", help="产物路径")
    args = parser.parse_args(argv)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = _REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(HEADER_LINES) + "\n" + build_compose(args.count)
    out_path.write_text(text, encoding="utf-8")
    yaml.safe_load(text)  # 生成即复核：产物必须可解析
    print(f"[gen_cluster_compose] 已生成 {out_path}（{args.count} 实例，双容器拓扑）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
