# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""知识库集群调用池（FR-SC-002，SR-SC-002 §6.2/§6.5；调用方侧集群检索客户端，demo 进程内形态）。

职责（agent-core 集群形态统一检索出口的三级削峰）：
- **查询指纹**（§6.2）：query 归一化（去空白/标点、小写）+ 参数排序后 SHA-256——
  同语义查询命中同键（kb_types 集合无关顺序，top_k 等参数参与指纹）；
- **单飞合并**（singleflight）：同指纹并发请求合并为一次回源——进程内 Future/Event
  表 + 锁选主（生产形态另加 Redis SETNX 跨实例选主，本模块协议不变）；
- **两级缓存**：L1 结果缓存（TTL 内直返）+ 热点 L2（触达 ≥ 阈值晋升，TTL 更长，
  服务 L1 过期后的热点查询——FR-KB-017 增量可见上限由 l1_ttl 约束）；
- **降级链**（§6.5：先缓存降级、再桩兜底，NFR-PER-03 不雪崩）：Milvus 不可用 →
  缓存（degraded=stale_cache）→ BM25 桩（degraded=bm25_stub）→ 全部失效上抛
  KbUnavailableError（上层走 FR-AG-009 兜底转人工，fail-safe 不装成功）。

demo 形态边界：Redis ag:l0:kbf / ag:sf:* 键与 httpx 回源由生产承载；本类以内存缓存 +
RetrievalBackend Protocol 注入等价实现，search() 出参契约（hits + meta）与生产一致。
线程安全：50 线程同指纹合并为单次回源、结果零竞态（并发压测为 SR §9 验收项）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

#: 查询文本归一化：去除空白/中英文标点/连字符（SR §6.2 _fingerprint 同语义归一口径；
#: 连字符剔除使「ST-3200」与「st3200」同指纹；全角逗号「，」等中英文标点同权剔除，
#: 型号书写/标点差异不改变查询语义）
_NORMALIZE_PAT = re.compile(
    r"[\s，、。；：？！《》（）‘’“”·…,.;:?!'\-‐‑–—]+", re.UNICODE)


class BackendUnavailableError(RuntimeError):
    """主检索后端（Milvus）不可用（触发降级链）。"""


class KbUnavailableError(RuntimeError):
    """全部降级层级失效（上层须走 FR-AG-009 兜底转人工，绝不吞错装成功）。"""


def normalize_query(text: str) -> str:
    """查询文本归一化：去空白/标点 + 小写（同语义查询 → 同指纹）。"""
    return _NORMALIZE_PAT.sub("", text).lower()


def query_fingerprint(req: dict) -> str:
    """查询指纹：规范化 query + 参数排序后 SHA-256（64 hex 字符，可作缓存键）。

    kb_types 视为集合（排序后参与）——顺序差异不改变语义；top_k/filters 等其余参数
    排序序列化，任何语义差异都会改变指纹（互不误合并）。
    """
    normalized = {**req, "query": normalize_query(str(req.get("query", "")))}
    if isinstance(normalized.get("kb_types"), (list, tuple, set)):
        normalized["kb_types"] = sorted(normalized["kb_types"])
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PoolConfig:
    """调用池参数（app_config scope='sc' 的冻结快照形态，NFR-MAI-02 配置外置）。"""

    l1_ttl_s: float = 60.0     # L1 结果缓存 TTL（FR-KB-017 增量可见延迟上限）
    l2_ttl_s: float = 600.0    # 热点 L2 保留 TTL（降级链的可吃缓存深度）
    hot_threshold: int = 3     # 热点晋升触达阈值（≥ 阈值的指纹进 L2）


class RetrievalBackend(Protocol):
    """检索后端协议（生产 = 统一检索 API 回源；demo = Milvus/BM25 桩注入）。"""

    def search(self, req: dict) -> dict: ...


class Bm25StubBackend:
    """BM25 稀疏检索桩（降级链最后一级兜底；demo 返回确定性占位命中）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def search(self, req: dict) -> dict:
        if self.fail:
            raise BackendUnavailableError("bm25 stub unavailable")
        query = normalize_query(str(req.get("query", ""))) or "query"
        return {"hits": [{"chunk_id": f"bm25:{hashlib.sha1(query.encode()).hexdigest()[:8]}",
                          "score": 0.5, "text": f"[BM25 兜底] {req.get('query', '')}"}],
                "latency_ms": 5.0}


@dataclass
class _CacheEntry:
    """缓存条目：负载 + 写入时刻 + 热点旗标（单存储，按 age 与 hot 分级服务）。"""

    payload: dict
    stored_at: float
    hot: bool = False


@dataclass
class _Inflight:
    """在途合并条目：选主者回源，等待者共享 Event 结果（进程内 Future 表）。"""

    event: threading.Event = field(default_factory=threading.Event)
    payload: dict | None = None


class ClusterRetrievalPool:
    """集群检索调用池：指纹合并 → 两级缓存 → 降级链（agent-core 统一检索出口）。

    :param backend: 主检索后端（Milvus 形态；可注入故障桩）；
    :param bm25: 降级链最后一级 BM25 桩（None = 无兜底，直接上抛）；
    :param config: 缓存参数（PoolConfig）；
    :param clock: 单调时钟注入（测试可控推进；默认 time.monotonic）。
    """

    def __init__(
        self,
        backend: RetrievalBackend,
        *,
        bm25: RetrievalBackend | None = None,
        config: PoolConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.backend = backend
        self.bm25 = bm25
        self.config = config or PoolConfig()
        self._clock = clock if clock is not None else time.monotonic
        self._cache: dict[str, _CacheEntry] = {}
        self._touch_count: dict[str, int] = {}
        self._inflight: dict[str, _Inflight] = {}
        self._lock = threading.Lock()
        self.stats: dict[str, int] = {
            "backend_calls": 0, "l1_hits": 0, "l2_hits": 0, "merges": 0,
            "stale_served": 0, "bm25_fallbacks": 0,
        }

    # ---- 对外：统一检索出口 ----

    def search(self, req: dict, *, requester: str = "") -> dict:
        """集群统一检索：L1/L2 缓存 → 同指纹合并 → 回源 → 降级链。

        :param req: 检索请求（query/kb_types/top_k，契约同 POST /api/v1/knowledge/search）；
        :param requester: 调用方实例标识（runner-NN，观测用）；
        :returns: {"hits": [...], "meta": {fingerprint, source, degraded, latency_ms}}。
        """
        t0 = time.perf_counter()
        fp = query_fingerprint(req)
        with self._lock:  # 热度计数（L2 晋升依据）
            self._touch_count[fp] = self._touch_count.get(fp, 0) + 1
        # ① 缓存检查（L1 新鲜域 / 热点 L2 域；后端探活不可用 → 降级吃缓存并打 stale 标记）
        cached = self._lookup(fp)
        if cached is not None:
            payload, source, degraded = cached
            return self._respond(payload, fp, source, degraded, t0)
        # ② 单飞合并：抢到主者回源，其余等待共享结果
        inflight: _Inflight | None
        with self._lock:
            inflight = self._inflight.get(fp)
            if inflight is None:
                inflight = _Inflight()
                self._inflight[fp] = inflight
                leader = True
            else:
                leader = False
        if not leader:
            inflight.event.wait(timeout=10.0)
            with self._lock:
                payload = inflight.payload
            if payload is None:  # 选主者异常兜底：自行回源（退化为不合并）
                return self._fetch_and_cache(req, fp, t0)
            with self._lock:
                self.stats["merges"] += 1
            return self._respond(payload, fp, "merged", [], t0)
        shared: dict | None = None
        try:
            result = self._fetch_and_cache(req, fp, t0)
            shared = {"hits": result["hits"]}  # 等待者共享的负载（与主者结果一致）
            return result
        finally:
            with self._lock:  # 唤醒等待者并摘除在途表项（finally 保证不悬死）
                entry = self._inflight.pop(fp, None)
                if entry is not None:
                    entry.payload = shared
                    entry.event.set()

    # ---- 内部：回源与降级链 ----

    def _fetch_and_cache(self, req: dict, fp: str, t0: float) -> dict:
        """回源主后端；不可用走降级链（缓存 → BM25 桩）；成功回填两级缓存。"""
        try:
            data = self.backend.search(req)
            with self._lock:
                self.stats["backend_calls"] += 1
            self._store(fp, data)
            return self._respond(data, fp, "milvus", [], t0)
        except BackendUnavailableError:
            degraded = self._degrade(req, fp)
            if degraded is not None:
                payload, source, flag = degraded
                return self._respond(payload, fp, source, [flag], t0)
            raise KbUnavailableError(
                "检索不可用：Milvus 不可达且无缓存/BM25 兜底（走 FR-AG-009 转人工）") from None

    def _degrade(self, req: dict, fp: str) -> tuple[dict, str, str] | None:
        """降级链：① 任一留存缓存（≤l2_ttl，含 L1 过期的 stale）→ ② BM25 桩。"""
        with self._lock:
            entry = self._cache.get(fp)
            age = self._clock() - entry.stored_at if entry else None
            if entry is not None and age <= self.config.l2_ttl_s:
                self.stats["stale_served"] += 1
                return entry.payload, "stale_cache", "stale_cache"
        if self.bm25 is not None:
            try:
                data = self.bm25.search(req)
                with self._lock:
                    self.stats["bm25_fallbacks"] += 1
                return data, "bm25_stub", "bm25_stub"
            except BackendUnavailableError:
                pass  # 桩也不可用：继续上抛路径
        return None

    # ---- 内部：缓存读写 ----

    def _backend_down(self) -> bool:
        """后端健康快照（demo = 后端 fail 旗标属性；生产 = /healthz 探活结果的短 TTL 缓存）。

        缓存命中路径据此决定是否转入降级语义：后端不可用时**任一留存缓存**（含 L1
        新鲜域）都以 stale_cache 降级旗标服务——不发起注定失败的回源，先缓存后桩不雪崩。
        """
        return bool(getattr(self.backend, "fail", False))

    def _lookup(self, fp: str) -> tuple[dict, str, list[str]] | None:
        """缓存命中判定：age ≤ l1_ttl → L1；热点条目 age ≤ l2_ttl → L2（读时晋升）。

        后端探活不可用 → 缓存降级路径：留存条目（≤ l2_ttl）一律服务并标记 stale_cache。
        """
        with self._lock:
            entry = self._cache.get(fp)
            if entry is None:
                return None
            if not entry.hot and self._touch_count.get(fp, 0) >= self.config.hot_threshold:
                entry.hot = True  # 热点读时晋升（触达计数达标即入 L2 服务域）
            age = self._clock() - entry.stored_at
            if self._backend_down():  # 降级①：Milvus 不可用 → 缓存顶上（先缓存后桩）
                if age <= self.config.l2_ttl_s:
                    self.stats["stale_served"] += 1
                    source = "cache:l1" if age <= self.config.l1_ttl_s else "cache:l2"
                    return entry.payload, source, ["stale_cache"]
                return None  # 缓存也过深过期：交由回源路径走 BM25 桩兜底
            if age <= self.config.l1_ttl_s:
                self.stats["l1_hits"] += 1
                return entry.payload, "cache:l1", []
            if entry.hot and age <= self.config.l2_ttl_s:
                self.stats["l2_hits"] += 1
                # 超出 L1 新鲜域的热点服务本身即降级语义（新鲜度 SLA 由 l1_ttl 承诺）
                return entry.payload, "cache:l2", ["stale_cache"]
            return None  # 过期且（非热点或超 L2 域）：冷查询重回源

    def _store(self, fp: str, data: dict) -> None:
        """回填缓存：触达 ≥ 阈值的指纹标记热点（写时定格，供 L2 分级服务）。"""
        with self._lock:
            hot = self._touch_count.get(fp, 0) >= self.config.hot_threshold
            self._cache[fp] = _CacheEntry(payload=data, stored_at=self._clock(), hot=hot)

    # ---- 内部：出参组装 ----

    def _respond(self, payload: dict, fp: str, source: str, degraded: list[str],
                 t0: float) -> dict:
        """出参契约：hits + meta（指纹截 12 位 / 来源 / 降级旗标 / 时延）。"""
        return {
            "hits": payload.get("hits", []),
            "meta": {
                "fingerprint": fp[:12],
                "source": source,
                "degraded": degraded,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            },
        }
