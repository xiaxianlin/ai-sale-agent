# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""内容防同质守卫（FR-SC-004-③，SR-SC-004 §6.3；跨账号内容互斥，demo 进程内形态）。

职责（多账号关联风险的内容维解耦，§17 R-06）：
- **指纹基元**：simhash-64（纯 Python + hashlib 分桶，NFR-SEC-06 无重型本地依赖）+
  汉明距离；近相似判定阈值 3bit（恰等 3 判相似、4 起放行）；
- **发送前守卫**（§6.3 guard）：完全相同内容走 sha256 快路径、近相似走 simhash——
  命中即**强制重写**（钩子协议注入 rewriter callable，重写产物复检）→ 过检登记
  （reason=ok:rewritten）；重写次数耗尽仍相似 → 拒发（content_homog，不登记指纹）；
- **窗口与本账号排除**：指纹 24h 滚动窗口（恰过期不再命中，prune 裁剪控容量）；
  仅跨账号比对（本账号自身历史排除，不与自己旧内容互斥）；
- **幂等**：同 msg_uid 重复 guard 回放首次结论（allow 相同），指纹库不重复计数。

demo 形态边界：指纹库由内存列表承载（生产 = Redis ag:uniq:{sha/sim} 分桶 + 过期
策略）；「强制重写」为钩子协议——生产注入 LLM 改写器，demo/测试注入确定性桩。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

#: 近相似判定阈值（汉明距离 ≤ 3bit 判相似；恰等 3 相似、4 起放行——SR §9 边界口径）
SIMHASH_THRESHOLD = 3

#: 强制重写次数上限（SR §7：耗尽仍相似 → 拒发，绝不带同质内容上线）
REWRITE_MAX_ATTEMPTS = 2

#: 指纹滚动窗口时长（小时，SR §4：同质判定只看近 24h 跨账号内容）
FINGERPRINT_WINDOW_H = 24.0


def _text_features(text: str) -> list[str]:
    """指纹特征抽取：字符二元组滑窗（CJK 友好；单字符文本降级为单特征）。"""
    if len(text) < 2:
        return [text] if text else ["<empty>"]
    return [text[i:i + 2] for i in range(len(text) - 1)]


def simhash64(text: str) -> int:
    """simhash-64：特征哈希按位加权分桶累加，符号位拼装指纹（纯 Python，不装包）。"""
    weights = [0] * 64
    for feature in _text_features(text):
        digest = hashlib.md5(feature.encode("utf-8")).digest()  # noqa: S324 —— 非加密用途
        value = int.from_bytes(digest[:8], "big")
        for bit in range(64):
            weights[bit] += 1 if (value >> bit) & 1 else -1
    return sum(1 << bit for bit in range(64) if weights[bit] > 0)


def hamming_distance(a: int, b: int) -> int:
    """汉明距离：异或按位计数（近相似度量的唯一口径）。"""
    return bin(a ^ b).count("1")


def _norm_text(text: str) -> str:
    """sha 快路径归一：去空白 + casefold（「ST-3200」与「st-3200」归一后同文）。"""
    return "".join(text.split()).casefold()


def _sha256(text: str) -> str:
    """内容 sha256（完全相同快路径；对归一文本计算——大小写/空白差异不改变同文判定）。"""
    return hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GuardResult:
    """守卫结论（发送闸门消费：allow=False → BLOCK 语义，绝不静默放行）。"""

    allow: bool
    reason: str                     # ok | ok:rewritten | content_homog | duplicate_msg
    text_out: str = ""              # 最终放行文本（重写后为改写产物）
    rewrites: int = 0               # 实际重写次数（恰耗尽 = rewrite_max_attempts）
    similar_to: tuple[int, ...] = ()  # 命中的在先账号（明细留痕）


@dataclass
class _Fingerprint:
    """指纹条目：sha 快路径 + simhash 近相似 + 归属与时刻（窗口裁剪粒度）。"""

    sha: str
    sim: int
    account_id: int
    at: datetime


class ContentUniqueness:
    """跨账号内容防同质守卫：比对 → 强制重写 → 过检/拒发（线程安全）。

    :param rewriter: 强制重写钩子 callable(text, account_id, attempt) -> str
      （协议注入：生产 = LLM 改写器；测试 = 确定性桩）；
    :param clock: 时钟注入（返回 datetime；测试可控推进验证窗口边界）；
    :param window_h: 指纹滚动窗口（小时）；
    :param rewrite_max_attempts: 重写次数上限（耗尽仍相似 → 拒发）；
    :param simhash_threshold: 近相似汉明距离阈值。
    """

    def __init__(
        self,
        *,
        rewriter,
        clock=None,
        window_h: float = FINGERPRINT_WINDOW_H,
        rewrite_max_attempts: int = REWRITE_MAX_ATTEMPTS,
        simhash_threshold: int = SIMHASH_THRESHOLD,
    ) -> None:
        self._rewriter = rewriter
        self._clock = clock if clock is not None else datetime.now
        self._window = timedelta(hours=window_h)
        self._rewrite_max = rewrite_max_attempts
        self._threshold = simhash_threshold
        self._lock = threading.RLock()
        self._prints: list[_Fingerprint] = []       # 指纹库（窗口滚动 + prune 裁剪）
        self._seen: dict[str, GuardResult] = {}     # msg_uid → 首次结论（幂等回放）

    # ---- 对外：发送前守卫 ----

    def guard(self, account_id: int, msg_uid: str, text: str) -> GuardResult:
        """发送前防同质检查：跨账号比对命中即强制重写，耗尽拒发（结论按 msg_uid 幂等）。"""
        with self._lock:
            first = self._seen.get(msg_uid)
            if first is not None:  # 重放：回放首次结论，不重复登记/计数/重写
                return GuardResult(allow=first.allow, reason="duplicate_msg",
                                   text_out=first.text_out, rewrites=first.rewrites,
                                   similar_to=first.similar_to)
            result = self._check(account_id, text)
            self._seen[msg_uid] = result
            return result

    def fingerprint_count(self) -> int:
        """当前指纹库容量（登记/裁剪对账用）。"""
        with self._lock:
            return len(self._prints)

    def prune(self, *, now: datetime | None = None) -> int:
        """显式裁剪过期指纹（容量受控），返回清除条数。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            stale = [p for p in self._prints if moment - p.at > self._window]
            self._prints = [p for p in self._prints if moment - p.at <= self._window]
            return len(stale)

    # ---- 内部：比对与登记 ----

    def _similar_owners(self, account_id: int, text: str, now: datetime) -> list[int]:
        """跨账号相似命中：sha 快路径（完全相同）∨ simhash ≤ 阈值（近相似）。

        排除：本账号自身历史（不与自己旧内容互斥）+ 窗口外指纹（恰过期不再命中）。
        """
        sha, sim = _sha256(text), simhash64(text)
        return [p.account_id for p in self._prints
                if p.account_id != account_id
                and now - p.at <= self._window
                and (p.sha == sha or hamming_distance(p.sim, sim) <= self._threshold)]

    def _check(self, account_id: int, text: str) -> GuardResult:
        """主判定（须持锁）：首发过检登记；命中强制重写复检；耗尽拒发不登记。"""
        now = self._clock()
        owners = self._similar_owners(account_id, text, now)
        if not owners:
            self._register(account_id, text, now)
            return GuardResult(allow=True, reason="ok", text_out=text)
        similar = tuple(owners)
        for attempt in range(1, self._rewrite_max + 1):  # 强制重写（钩子协议注入）
            candidate = self._rewriter.rewrite(text, account_id, attempt)
            if not self._similar_owners(account_id, candidate, now):
                self._register(account_id, candidate, now)
                return GuardResult(allow=True, reason="ok:rewritten", text_out=candidate,
                                   rewrites=attempt, similar_to=similar)
        return GuardResult(allow=False, reason="content_homog", text_out=text,
                           rewrites=self._rewrite_max, similar_to=similar)

    def _register(self, account_id: int, text: str, now: datetime) -> None:
        """指纹登记（放行即登记；拒发不登记——须持锁调用）。"""
        self._prints.append(_Fingerprint(sha=_sha256(text), sim=simhash64(text),
                                         account_id=account_id, at=now))
