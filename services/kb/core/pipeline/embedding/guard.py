# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""Embedding 端点出域防护（FR-KB-013 / NFR-SEC-06 数据不出域，代码层断言）。

三层出入域防护的第一层（SR-KB-013 §6.1①）：仅内网 / 回环地址合法，
配置了外部公网端点即拒绝启动（fail-fast），从代码层面阻断知识块文本
被发送至外部 Embedding API 的可能。

判定规则（fail-closed：无法证明为内网即一律判外）：

1. scheme 必须为 http——内网 TEI 走明文 http，https 端点一律视为外部；
2. 主机名命中白名单（默认 tei-embedding / localhost / 127.0.0.1）直接放行；
3. 主机名为 IP 字面量：回环地址放行，其余比对私网 CIDR 白名单；
4. 其余域名：解析出的全部 IP 逐一比对私网段，任一公网 IP 即拒绝；
   域名解析失败同样拒绝（防「公网 IP 直写域名」绕过断言）。

实现方案：docs/07-实现方案/KB/SR-KB-013-向量化服务.md §6.1、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from typing import Callable
from urllib.parse import urlparse

#: 出域白名单主机名（SR-KB-013 §7 KB_EMBED_ALLOWED_HOSTS 默认值）
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"tei-embedding", "localhost", "127.0.0.1", "::1"}
)

#: 出域白名单私网段（SR-KB-013 §7 KB_EMBED_ALLOWED_CIDRS 默认值）
DEFAULT_ALLOWED_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

#: 域名解析器类型：主机名 -> IP 字符串列表（可注入以便单元测试，避免真实 DNS）
EndpointResolver = Callable[[str], list[str]]


class EmbeddingEgressError(RuntimeError):
    """配置了外部 Embedding 端点：FR-KB-013 / NFR-SEC-06 红线，进程启动即失败。"""


def default_resolve_host(host: str) -> list[str]:
    """默认域名解析：socket.getaddrinfo；失败返回空列表（由上层按 fail-closed 拒绝）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return []
    # 去重排序保证判定稳定；剥离 IPv6 zone scope（如 fe80::1%lo0）
    return sorted({info[4][0].split("%", 1)[0] for info in infos})


def _parse_networks(cidrs: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """把 CIDR 字符串列表解析为网络对象（非法配置直接抛 ValueError 暴露问题）。"""
    return [ipaddress.ip_network(c) for c in cidrs]


def _ip_allowed(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """单个 IP 是否合法：回环地址或落在私网白名单段内。"""
    return addr.is_loopback or any(addr in net for net in nets)


def is_allowed_embedding_endpoint(
    endpoint: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allowed_cidrs: Iterable[str] | None = None,
    resolver: EndpointResolver | None = None,
) -> bool:
    """判定 Embedding 端点是否为允许的内网 / 回环地址（NFR-SEC-06）。

    参数：
        endpoint:       完整端点 URL（如 http://tei-embedding:8080）；
        allowed_hosts:  主机名白名单，缺省用 DEFAULT_ALLOWED_HOSTS；
        allowed_cidrs:  私网段白名单，缺省用 DEFAULT_ALLOWED_CIDRS；
        resolver:       域名解析器，缺省走真实 DNS（单元测试可注入假解析器）。

    返回：True=内网/回环合法；False=外部端点或无法证明为内网（fail-closed）。
    """
    hosts = frozenset(
        h.lower() for h in (allowed_hosts if allowed_hosts is not None else DEFAULT_ALLOWED_HOSTS)
    )
    nets = _parse_networks(allowed_cidrs if allowed_cidrs is not None else DEFAULT_ALLOWED_CIDRS)
    resolve = resolver if resolver is not None else default_resolve_host

    try:
        parsed = urlparse(endpoint.strip())
    except ValueError:
        return False
    if parsed.scheme != "http":  # 内网 TEI 走明文 http；https 端点一律视为外部
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in hosts:
        return True

    # IP 字面量：回环放行，其余比对私网段（无需 DNS）
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return _ip_allowed(literal, nets)

    # 域名：解析出的全部 IP 均须落在私网段；解析失败 / 含公网 IP 即拒绝
    resolved = [ip for ip in resolve(host) if ip]
    if not resolved:
        return False
    for raw in resolved:
        try:
            addr = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            return False
        if not _ip_allowed(addr, nets):
            return False
    return True


def assert_allowed_embedding_endpoint(
    endpoint: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allowed_cidrs: Iterable[str] | None = None,
    resolver: EndpointResolver | None = None,
) -> None:
    """出域断言（fail-fast）：端点不合法即抛 EmbeddingEgressError，拒绝启动。

    TeiEmbeddingClient 构造时调用（配置加载即校验），确保配置了外部公网
    Embedding 地址的进程根本无法完成初始化（SR-KB-013 §6.1①）。
    """
    if not is_allowed_embedding_endpoint(
        endpoint, allowed_hosts=allowed_hosts, allowed_cidrs=allowed_cidrs, resolver=resolver
    ):
        raise EmbeddingEgressError(
            f"禁止外部 Embedding 端点（NFR-SEC-06 数据不出域，"
            f"知识块文本仅允许内网向量化）：{endpoint}"
        )
