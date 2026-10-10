# 知识产权归甲方所有（COM-IP-01）
"""③ 重排序（FR-KB-015 / SR-KB-015 §6.4）：本地 TEI rerank 客户端 + 降级。

部署形态：bge-reranker-v2-m3 独立 TEI 容器（内网、权重只读挂载、
HF_HUB_OFFLINE=1，NFR-SEC-06 数据不出域），HTTP 契约为本地 TEI /rerank：
    POST {"query": ..., "texts": [...], "top_n": K}
    → [{"index": i, "score": s}, ...]（按 score 降序）

降级口径（§6.1 判定点 D）：250ms 硬超时（asyncio.wait_for 施加，可配）+
传输错误/坏响应 → RRF 序直出（候选按入参序截断 TopK），经 on_degrade 回调
留痕；重排跳过率目标 <1%（P95），超限告警（§6.5）。

契约约定（流水线判定依据）：正常重排必须写回 Hit.rerank_score；降级直出
的候选 rerank_score 保持 None——流水线据此识别「重排未生效」并计入 degrade 链。

注：文件头部先执行标准库 platform 自愈再 import httpx——仓库顶层包目录
与标准库 platform 同名，从仓库根运行 pytest 时 httpx→rich→attrs 链路上的
platform.python_implementation() 会 AttributeError（同 platform/tests/conftest.py
处理手法：把标准库语义回填到已缓存包对象上，子模块导入不受影响）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import sysconfig
from dataclasses import replace
from types import ModuleType
from typing import Callable, Protocol, Sequence, runtime_checkable

from .base import Hit


def _ensure_stdlib_platform() -> None:
    """标准库 platform 语义自愈（幂等）：被仓库同名顶层包遮蔽时回填能力。

    - 已缓存且具备标准库能力（如从仓库外运行）：直接返回；
    - 未缓存：以标准库路径装载注册（防御分支）；
    - 已缓存为仓库空包（pytest 常规路径）：执行标准库源码，把公开属性回填
      到缓存对象上——进程内旧引用同步恢复，platform.core 等子模块不受影响。
    """
    cached: ModuleType | None = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:  # 标准库定位失败属环境损坏，应显式失败
        raise RuntimeError("无法定位标准库 platform 模块（运行环境损坏）")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


_ensure_stdlib_platform()

import httpx  # noqa: E402 —— 必须位于 platform 自愈之后（见模块 docstring）

# 重排硬超时默认值（KB_SEARCH_RERANK_TIMEOUT_MS，判定点 D）
RERANK_TIMEOUT_MS_DEFAULT = 250.0
# 本地 TEI rerank 端点默认地址（deploy/docker-compose.yml tei-reranker 服务）
TEI_RERANK_BASE_URL_DEFAULT = "http://127.0.0.1:8081"

# 降级原因标记（on_degrade 回调入参与流水线 degrade 链 token）
DEGRADE_RERANK_TIMEOUT = "rerank_timeout"
DEGRADE_RERANK_UNREACHABLE = "rerank_unreachable"
DEGRADE_RERANK_BAD_RESPONSE = "rerank_bad_response"


@runtime_checkable
class Reranker(Protocol):
    """重排器契约（SR-KB-015 §5）：Top50 候选 → 精排 TopK。

    实现约定：生效精排须写回 Hit.rerank_score；降级直出不写回（流水线据此
    识别并计入 degrade 链）。
    """

    async def rerank(self, query: str, cands: Sequence[Hit], top_k: int) -> list[Hit]: ...


class PassthroughReranker:
    """直通重排器：按入参序截断 TopK（rerank=false 或 TEI 客户端不可构建时兜底）。"""

    async def rerank(self, query: str, cands: Sequence[Hit], top_k: int) -> list[Hit]:
        """不做精排，直出 RRF 序前 TopK（rerank_score 保持 None）。"""
        return list(cands[:top_k])


class BgeRerankerClient:
    """bge-reranker-v2-m3 本地 TEI /rerank 客户端（httpx，超时/降级内置）。

    构造注入 httpx.AsyncClient（测试注入 MockTransport/自定义传输），缺省
    自建指向本地 TEI 端点的客户端并由本实例持有（aclose 释放）。
    """

    def __init__(
        self,
        base_url: str = TEI_RERANK_BASE_URL_DEFAULT,
        *,
        timeout_ms: float = RERANK_TIMEOUT_MS_DEFAULT,
        client: httpx.AsyncClient | None = None,
        on_degrade: Callable[[str], None] | None = None,
    ) -> None:
        self._timeout_s = timeout_ms / 1000.0
        self._on_degrade = on_degrade
        self._owns_client = client is None
        # 始终请求绝对 URL（注入客户端可不带 base_url，仅提供传输层）
        self._endpoint = f"{base_url.rstrip('/')}/rerank"
        self._client = client if client is not None else httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(self._timeout_s),
        )

    @property
    def timeout_ms(self) -> float:
        """硬超时配置（判定点 D，毫秒）。"""
        return self._timeout_s * 1000.0

    async def aclose(self) -> None:
        """释放自建客户端（注入的客户端由调用方负责生命周期）。"""
        if self._owns_client:
            await self._client.aclose()

    async def rerank(self, query: str, cands: Sequence[Hit], top_k: int) -> list[Hit]:
        """精排候选并截断 TopK；超时/传输错误/坏响应降级为 RRF 序直出。"""
        if top_k <= 0 or not cands:
            return []
        try:
            return await asyncio.wait_for(self._rerank_call(query, cands, top_k), timeout=self._timeout_s)
        except TimeoutError:
            return self._degrade(DEGRADE_RERANK_TIMEOUT, cands, top_k)
        except httpx.TimeoutException:
            return self._degrade(DEGRADE_RERANK_TIMEOUT, cands, top_k)
        except httpx.TransportError:
            return self._degrade(DEGRADE_RERANK_UNREACHABLE, cands, top_k)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return self._degrade(DEGRADE_RERANK_BAD_RESPONSE, cands, top_k)

    async def _rerank_call(self, query: str, cands: Sequence[Hit], top_k: int) -> list[Hit]:
        """实际调用本地 TEI /rerank（异常交由 rerank 统一分类降级）。"""
        texts = [c.snippet or c.title or str(c.chunk_id) for c in cands]
        payload = {"query": query, "texts": texts, "top_n": top_k}
        response = await self._client.post(self._endpoint, json=payload)
        response.raise_for_status()
        items = response.json()
        if not isinstance(items, list) or not items:
            raise ValueError(f"rerank 响应为空或非列表: {items!r}")
        ranked = sorted(items, key=lambda item: float(item["score"]), reverse=True)[:top_k]  # 防御性截断
        reranked: list[Hit] = []
        for item in ranked:
            index = int(item["index"])
            if not 0 <= index < len(cands):
                raise ValueError(f"rerank 响应 index 越界: {index}")
            score = float(item["score"])
            reranked.append(replace(cands[index], score=score, rerank_score=score))
        return reranked

    def _degrade(self, reason: str, cands: Sequence[Hit], top_k: int) -> list[Hit]:
        """降级直出：RRF 序截断 TopK，回调留痕（判定点 D，保 NFR-PER-02）。"""
        if self._on_degrade is not None:
            self._on_degrade(reason)
        return list(cands[:top_k])
