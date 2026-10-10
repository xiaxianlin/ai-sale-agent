# 知识产权归甲方所有（COM-IP-01）
"""去重模块：全量 md5 精确去重 + simhash 64 位 4 段查表近似去重（SR-KB-011 §6.2 / §6.3）。

- 全量去重：规范化文本（去首尾空白）的 md5 为权威键，存储侧 md5 唯一键兜底并发；
- 近似去重：字符 3-gram 词频加权 simhash 64 位指纹，按 16 位 × 4 段建索引查表——
  鸽笼原理保证汉明距离 ≤3 时至少一段完全相等，段查表不漏检（阈值上限因此锁定 3）；
- 指纹存储抽象为 FingerprintStore 协议：单元测试 / 单机演示用进程内实现，
  生产实现为 PG kb_dedup_fingerprint 表（md5 唯一键 + seg0~seg3 复合索引，§4 DDL）；
- 判重一律排除本文件指纹：任务 at-least-once 重放不自判重（§6.6 幂等）。

依据文档：docs/07-实现方案/KB/SR-KB-011-数据清洗.md §6.2、§6.3、§7 配置项；
         docs/03-技术文档/02-开发规范.md §3。
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Protocol


def normalize_for_md5(text: str) -> str:
    """md5 规范化口径：仅去首尾空白（SR-KB-011 §6.2）。"""
    return text.strip()


def md5_fingerprint(text: str) -> str:
    """全量去重权威键：规范化文本的 md5 十六进制摘要。"""
    return hashlib.md5(normalize_for_md5(text).encode("utf-8")).hexdigest()


def hamming_distance(a: int, b: int) -> int:
    """两个 64 位 simhash 的汉明距离（不同位的个数）。"""
    return (a ^ b).bit_count()


def simhash_segments(value: int, parts: int = 4, bits: int = 16) -> list[int]:
    """64 位指纹按 16 位 × 4 段切分：汉明距离 ≤3 时至少一段相等（鸽笼原理，§6.3）。"""
    mask = (1 << bits) - 1
    return [(value >> (bits * i)) & mask for i in range(parts)]


class SimHash:
    """字符 3-gram 词频加权 simhash（SR-KB-011 §6.3 原始算法，进程内确定性计算）。

    以 blake2b（8 字节摘要）作为每个 gram 的 64 位散列，词频为权重对 64 个位投票：
    各位票和 >0 取 1、否则取 0，合成 64 位指纹。空文本指纹为 0。
    """

    __slots__ = ("value",)

    def __init__(self, text: str) -> None:
        feats: Counter[str] = Counter(text[i : i + 3] for i in range(max(0, len(text) - 2)))
        votes = [0] * 64
        for gram, weight in feats.items():
            h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
            for b in range(64):
                votes[b] += weight if (h >> b) & 1 else -weight
        self.value: int = sum(1 << b for b in range(64) if votes[b] > 0)

    def segments(self) -> list[int]:
        """当前指纹的 4 段 16 位分段值（段查表索引）。"""
        return simhash_segments(self.value)

    def distance_to(self, other: "SimHash | int") -> int:
        """与另一指纹（SimHash 或 64 位整数）的汉明距离。"""
        return hamming_distance(self.value, other.value if isinstance(other, SimHash) else int(other))


@dataclass
class Fingerprint:
    """去重指纹记录：kb_dedup_fingerprint 表的内存形态（SR-KB-011 §4 DDL 对应）。"""

    md5: str  # 规范化文本 md5（全量去重权威键，唯一）
    simhash: int  # 64 位 simhash
    kb_type: str  # 所属知识库（近似去重仅同库判重，防跨库误杀）
    source_file: str  # 来源文件（自排除 + §12.2 回溯锚点）
    block_index: int  # 来源块序号


class FingerprintStore(Protocol):
    """指纹存储协议：生产实现为 PG kb_dedup_fingerprint（md5 唯一键 + 4 段复合索引）。"""

    def get(self, md5: str) -> Fingerprint | None:
        """按 md5 精确查指纹（全量去重复核）。"""
        ...

    def add(self, fp: Fingerprint) -> None:
        """插入指纹（ON CONFLICT DO NOTHING 语义：md5 冲突时忽略，保并发幂等）。"""
        ...

    def remove(self, md5: str) -> bool:
        """按 md5 删除指纹（纠错复查判重后回滚本块指纹）。"""
        ...

    def find_by_segment(self, kb_type: str, seg_no: int, seg_value: int) -> list[Fingerprint]:
        """按 (kb_type, segN) 段索引查候选指纹（保持插入序，保证 first-keep 判定确定性）。"""
        ...


class InMemoryFingerprintStore:
    """进程内指纹存储：单元测试与单机演示用；语义对齐 PG 表（md5 唯一 + 4 段索引）。"""

    def __init__(self) -> None:
        self._by_md5: dict[str, Fingerprint] = {}  # dict 保序 → 候选序 = 插入序
        self._seg_index: dict[tuple[str, int, int], list[str]] = defaultdict(list)

    def __len__(self) -> int:
        return len(self._by_md5)

    def get(self, md5: str) -> Fingerprint | None:
        return self._by_md5.get(md5)

    def add(self, fp: Fingerprint) -> None:
        if fp.md5 in self._by_md5:  # md5 唯一键：重放 / 并发插入幂等（ON CONFLICT DO NOTHING）
            return
        self._by_md5[fp.md5] = fp
        for seg_no, seg_value in enumerate(simhash_segments(fp.simhash)):
            self._seg_index[(fp.kb_type, seg_no, seg_value)].append(fp.md5)

    def remove(self, md5: str) -> bool:
        fp = self._by_md5.pop(md5, None)
        if fp is None:
            return False
        for seg_no, seg_value in enumerate(simhash_segments(fp.simhash)):
            bucket = self._seg_index.get((fp.kb_type, seg_no, seg_value))
            if bucket and md5 in bucket:
                bucket.remove(md5)
        return True

    def find_by_segment(self, kb_type: str, seg_no: int, seg_value: int) -> list[Fingerprint]:
        """段索引查候选：返回插入序指纹列表（已删除的 md5 自动跳过）。"""
        md5s = self._seg_index.get((kb_type, seg_no, seg_value), [])
        return [self._by_md5[m] for m in md5s if m in self._by_md5]


class ExactDeduper:
    """全量去重器：md5 精确判重；排除本文件指纹防任务重放自判重（SR-KB-011 §6.2）。"""

    def __init__(self, store: FingerprintStore) -> None:
        self.store = store

    def check(self, text: str, self_file: str) -> tuple[bool, str]:
        """判重：返回 (是否重复, 本文本 md5)。

        同文件已存在同指纹不算重复（at-least-once 重放安全）；
        文件内部的真正重复块由纠错后复查阶段按 first-keep 补判（§6.6 _recheck_exact）。
        """
        md5 = md5_fingerprint(text)
        fp = self.store.get(md5)
        return (fp is not None and fp.source_file != self_file), md5


class NearDeduper:
    """近似去重器：4 段查表召回候选 → 整值汉明距离复核（SR-KB-011 §6.3）。

    hamming_max 上限 3：4×16 位分段下鸽笼原理仅对 ≤3 成立，>3 会漏检；
    需支持更大阈值时应改 8 段 × 8 位方案（§6.3 注释）。阈值可配（§7 取值范围 1~3）。
    """

    def __init__(self, store: FingerprintStore, hamming_max: int = 3) -> None:
        if not 1 <= hamming_max <= 3:
            raise ValueError("hamming_max 取值范围 1~3：4 段 16 位鸽笼窗口上限（SR-KB-011 §7）")
        self.store = store
        self.hamming_max = hamming_max

    def find_dup(self, kb_type: str, sim: "SimHash | int", self_file: str) -> tuple[str | None, int]:
        """查找近似重复：返回 (命中指纹 md5, 汉明距离) 或 (None, -1)。

        仅同库（kb_type）判重、排除本文件指纹；多候选取距离最小者，
        距离相同时取先入库者（first-keep：保留先入库块）。
        """
        value = sim.value if isinstance(sim, SimHash) else int(sim)
        candidates: dict[str, Fingerprint] = {}
        # 4 个 (kb_type, segN) 索引查表合并候选集（鸽笼原理保证 ≤3 必被召回）
        for seg_no, seg_value in enumerate(simhash_segments(value)):
            for fp in self.store.find_by_segment(kb_type, seg_no, seg_value):
                if fp.source_file != self_file:  # 自排除：重放不自判重
                    candidates.setdefault(fp.md5, fp)
        best_md5: str | None = None
        best_dist = self.hamming_max + 1  # 超过阈值即视为不重复
        for md5, fp in candidates.items():  # 候选集极小，逐个整值汉明复核
            dist = hamming_distance(value, fp.simhash)
            if dist <= self.hamming_max and dist < best_dist:
                best_md5, best_dist = md5, dist
        if best_md5 is None:
            return None, -1
        return best_md5, best_dist
