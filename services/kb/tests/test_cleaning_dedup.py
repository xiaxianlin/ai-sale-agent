# 知识产权归甲方所有（COM-IP-01）
"""去重模块单元测试：全量 md5 判重、simhash 鸽笼判重边界、段查表召回与豁免（SR-KB-011 §9）。"""
from __future__ import annotations

import pytest

from services.kb.core.pipeline.cleaning.dedup import (
    ExactDeduper,
    Fingerprint,
    InMemoryFingerprintStore,
    NearDeduper,
    SimHash,
    hamming_distance,
    md5_fingerprint,
    simhash_segments,
)


def _seed(
    store: InMemoryFingerprintStore,
    md5: str,
    simhash: int,
    kb_type: str = "product",
    source: str = "旧文件.docx",
    block: int = 0,
) -> None:
    """向指纹库预置一条指纹（模拟先入库的他文件块）。"""
    store.add(Fingerprint(md5=md5, simhash=simhash, kb_type=kb_type, source_file=source, block_index=block))


class TestExactDedup:
    """§9 test_exact_dup_crossfile：md5 规范化口径 + 跨文件判重 + 自排除。"""

    def test_md5_normalize_and_difference(self) -> None:
        # 规范化 = 去首尾空白：首尾空白不改变指纹，内容差异改变指纹
        assert md5_fingerprint("  相同文本  ") == md5_fingerprint("相同文本")
        assert md5_fingerprint("相同文本") != md5_fingerprint("相同文本。")

    def test_crossfile_dup_and_self_exclusion(self) -> None:
        store = InMemoryFingerprintStore()
        text = "这是一段用于全量去重测试的重复商务文本，长度不影响判重结论。"
        fp = Fingerprint(md5_fingerprint(text), SimHash(text).value, "product", "旧文件.docx", 0)
        store.add(fp)
        dedup = ExactDeduper(store)
        is_dup, md5 = dedup.check(text, "新文件.docx")
        assert is_dup and md5 == fp.md5  # 跨文件同文本 → 判重，dup_of 指向先入库指纹
        is_dup, _ = dedup.check(text, "旧文件.docx")
        assert not is_dup  # 同文件自排除：at-least-once 重放不自判重（§6.6）
        is_dup, _ = dedup.check("完全不同的另一段文本", "新文件.docx")
        assert not is_dup  # 新文本不判重

    def test_store_conflict_do_nothing(self) -> None:
        # md5 唯一键：重复插入被忽略（ON CONFLICT DO NOTHING 语义），保留先入库记录
        store = InMemoryFingerprintStore()
        store.add(Fingerprint("m1", 1, "product", "a.docx", 0))
        store.add(Fingerprint("m1", 999, "product", "b.docx", 3))
        assert len(store) == 1
        assert store.get("m1").source_file == "a.docx"


class TestSimHash:
    """simhash 指纹确定性与分段数学性质。"""

    def test_deterministic(self) -> None:
        assert SimHash("报价单含FOB价格条款").value == SimHash("报价单含FOB价格条款").value

    def test_segments_math(self) -> None:
        # 4 段 × 16 位：每段各置 1 位 → 分段值应为 [1, 1, 1, 1]
        value = (1 << 0) | (1 << 16) | (1 << 32) | (1 << 48)
        assert simhash_segments(value) == [1, 1, 1, 1]

    def test_close_text_small_hamming(self) -> None:
        # 长文本局部微调（3-gram 扰动有限）→ 汉明距离应远小于 64
        base = "本报价基于2026年第一季度汇率与海运费测算，有效期三十天，逾期请重新确认价格与交期安排。" * 4
        mutated = base.replace("三十天", "三十天，", 1)
        assert hamming_distance(SimHash(base).value, SimHash(mutated).value) <= 16


class TestNearDedupBoundary:
    """§9 test_simhash_pigeonhole：汉明距离 1/2/3 判重，4 判新（不漏不误）。"""

    @staticmethod
    def _deduper_with_seed(seed_value: int) -> NearDeduper:
        store = InMemoryFingerprintStore()
        _seed(store, "seed-md5", seed_value)
        return NearDeduper(store)

    @pytest.mark.parametrize("dist", [1, 2, 3])
    def test_within_threshold_found(self, dist: int) -> None:
        seed = 0x0F0F_0F0F_0F0F_0F0F
        dedup = self._deduper_with_seed(seed)
        flip = (1 << dist) - 1  # 低 dist 位翻转 → 与种子汉明距离恰为 dist
        md5, d = dedup.find_dup("product", seed ^ flip, "本文件.docx")
        assert md5 == "seed-md5" and d == dist  # ≤3 阈值内 → 段查表召回 + 整值复核命中

    def test_beyond_threshold_all_segments_differ(self) -> None:
        # 每段各翻 1 位：汉明距离 4 且 4 段全不同 → 段索引召回不到任何候选
        dedup = self._deduper_with_seed(0)
        value = (1 << 0) | (1 << 16) | (1 << 32) | (1 << 48)
        assert dedup.find_dup("product", value, "本文件.docx") == (None, -1)

    def test_beyond_threshold_recalled_but_rejected(self) -> None:
        # 距离 4 但翻转集中在 3 段（bit0/1/16/32）→ seg2 段相等可召回候选，但 4 > 阈值 3 → 判新
        dedup = self._deduper_with_seed(0)
        value = (1 << 0) | (1 << 1) | (1 << 16) | (1 << 32)
        assert dedup.find_dup("product", value, "本文件.docx") == (None, -1)

    def test_threshold_configurable(self) -> None:
        # 汉明阈值可配（§7 取值范围 1~3）：收紧到 1 后距离 2 不再判重
        store = InMemoryFingerprintStore()
        _seed(store, "s", 0)
        strict = NearDeduper(store, hamming_max=1)
        assert strict.find_dup("product", 0b11, "f.docx")[0] is None  # 距离 2 > 1
        md5, d = strict.find_dup("product", 0b1, "f.docx")
        assert (md5, d) == ("s", 1)  # 距离 1 ≤ 1 仍命中

    def test_threshold_out_of_range_rejected(self) -> None:
        # >3 则 4 段鸽笼窗口失效（漏检），<1 无意义：构造期直接拒绝
        with pytest.raises(ValueError):
            NearDeduper(InMemoryFingerprintStore(), hamming_max=4)
        with pytest.raises(ValueError):
            NearDeduper(InMemoryFingerprintStore(), hamming_max=0)

    def test_same_kb_only_and_self_exclusion(self) -> None:
        # 近似去重仅同库判重（防跨库误杀）+ 本文件指纹自排除（重放安全）
        store = InMemoryFingerprintStore()
        _seed(store, "md5-training", 0b101, kb_type="training", source="训练.docx")
        dedup = NearDeduper(store)
        assert dedup.find_dup("product", 0b101, "其他.docx")[0] is None  # 跨库不判重
        assert dedup.find_dup("training", 0b101, "训练.docx")[0] is None  # 本文件自排除
        assert dedup.find_dup("training", 0b101, "其他.docx")[0] == "md5-training"
