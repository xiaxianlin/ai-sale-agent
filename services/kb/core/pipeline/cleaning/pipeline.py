# 知识产权归甲方所有（COM-IP-01）
"""清洗三段编排：去重 → 纠错 → 脱敏（SR-KB-011 §6.6，决策 D1 / D2 / D3）。

- 去重只标记不物理删除（D2）：dropped 块保留 block_index 与 dup_of 指针，正文恒空，
  原文回溯走 p0/parse-output 归档；表格与短块豁免近似去重（防误杀）；
- 纠错仅处理 kept 块（表格逐单元格，保持原子块契约）；纠错后补一次全量 md5 复查，
  捕捉「纠错趋同」的漏网重复（D1；first-keep 保留先出现块）；
- 脱敏必须先于 FR-KB-012 分块（D3：向量不可脱敏，清洗是唯一闸门入口）；
- 全程留痕：每块维护 CleanAction 链（dedup / correct / mask 三段），
  stats 统计口径对齐 §4 action_detail。

三段全部为进程内确定性计算，无外部 API（NFR-SEC-06 数据不出域）。

依据文档：docs/07-实现方案/KB/SR-KB-011-数据清洗.md §6.6；
         docs/03-技术文档/02-开发规范.md §3。
"""
from __future__ import annotations

from typing import Sequence

from .base import (
    CleanAction,
    CleanedBlock,
    CleanedDoc,
    ParsedBlock,
    STAGE_CORRECT,
    STAGE_DEDUP,
    STATUS_DROPPED_EXACT,
    STATUS_DROPPED_NEAR,
    STATUS_KEPT,
    now_iso,
    table_repr,
)
from .correction import Corrector, default_corrector
from .dedup import (
    ExactDeduper,
    Fingerprint,
    FingerprintStore,
    InMemoryFingerprintStore,
    NearDeduper,
    SimHash,
    md5_fingerprint,
)
from .masking import MaskingEngine, RuleSet, Vault, default_ruleset


class CleaningPipeline:
    """三段清洗编排器：一次 run 输出带 diff 留痕链的 CleanedDoc。"""

    def __init__(
        self,
        *,
        store: FingerprintStore,
        exact: ExactDeduper,
        near: NearDeduper,
        corrector: Corrector,
        masker: MaskingEngine,
        kb_type: str = "product",
        dedup_min_chars: int = 20,
    ) -> None:
        self.store = store
        self.exact = exact
        self.near = near
        self.corrector = corrector
        self.masker = masker
        self.kb_type = kb_type
        self.dedup_min_chars = dedup_min_chars  # 短块豁免阈值（§7 clean.dedup.min_block_chars）

    def run(
        self,
        blocks: Sequence[ParsedBlock],
        source_file: str,
        kb_type: str | None = None,
    ) -> CleanedDoc:
        """执行三段清洗：去重 → 纠错（+复查）→ 脱敏，返回 CleanedDoc。"""
        kb = kb_type or self.kb_type
        by_index = {b.block_index: b for b in blocks}
        cleaned: list[CleanedBlock] = []
        inserted: dict[int, str] = {}  # 本轮各 kept 块写入的指纹 md5（复查判重后回滚用）

        # ── 阶段一 去重：全量 md5 → 近似 simhash；表格 / 短块豁免近似判重（§6.6）──
        for b in blocks:
            cb = CleanedBlock(block_index=b.block_index, status=STATUS_KEPT)
            text = table_repr(b.table) if (b.type == "table" and b.table is not None) else b.text
            dup, md5 = self.exact.check(text, source_file)
            near_md5: str | None = None
            ham = -1
            if not dup and b.type != "table" and len(text.strip()) >= self.dedup_min_chars:
                near_md5, ham = self.near.find_dup(kb, SimHash(text), source_file)
            if dup or near_md5:
                # 只标记不删除（D2）：dup_of 指向保留块指纹，正文回溯走 parse 归档
                cb.status = STATUS_DROPPED_EXACT if dup else STATUS_DROPPED_NEAR
                cb.dup_of_md5 = md5 if dup else near_md5
                rule = "dedup.exact" if dup else "dedup.near"
                cb.actions.append(
                    CleanAction(
                        seq=1,
                        stage=STAGE_DEDUP,
                        rule_id=rule,
                        before=f"(block:{b.block_index})",
                        after=f"(dup_of:{cb.dup_of_md5},hamming:{ham})",
                        ts=now_iso(),
                    )
                )
            else:
                # 原子落库（md5 唯一键幂等）+ 段索引，供后续文件判重
                self.store.add(
                    Fingerprint(
                        md5=md5,
                        simhash=SimHash(text).value,
                        kb_type=kb,
                        source_file=source_file,
                        block_index=b.block_index,
                    )
                )
                inserted[b.block_index] = md5
            cleaned.append(cb)

        # ── 阶段二 纠错：仅 kept 块；表格逐单元格处理（原子块契约，SR-KB-010 §6.1）──
        for cb in cleaned:
            if cb.status != STATUS_KEPT:
                continue
            b = by_index[cb.block_index]
            if b.type == "table" and b.table is not None:
                corrected_rows = [[self.corrector.run(cell, cb.actions) for cell in row] for row in b.table]
                cb.text = table_repr(corrected_rows)
            else:
                cb.text = self.corrector.run(b.text, cb.actions)

        # ── 纠错后全量 md5 复查：纠错趋同块补判 dropped_exact（D1；first-keep）──
        self._recheck_exact(cleaned, inserted, source_file)

        # ── 阶段三 脱敏：必须先于 FR-KB-012 分块（D3）；dropped 块不落原文 ──
        masked_findings = 0
        vault_refs = 0
        needs_review = 0
        for cb in cleaned:
            if cb.status != STATUS_KEPT:
                cb.text = ""  # dropped 块正文恒空（D2）
                continue
            result = self.masker.mask(
                cb.text,
                block_index=cb.block_index,
                source_file=source_file,
                kb_type=kb,
                actions=cb.actions,
            )
            cb.text = result.text
            masked_findings += len(result.findings)
            vault_refs += result.vault_refs
            needs_review += result.needs_review

        # ── 统计：口径对齐 §4 action_detail（三段计数 + 规则集版本）──
        corrections = sum(1 for cb in cleaned for a in cb.actions if a.stage == STAGE_CORRECT)
        kept = sum(1 for cb in cleaned if cb.status == STATUS_KEPT)
        dropped_exact = sum(1 for cb in cleaned if cb.status == STATUS_DROPPED_EXACT)
        dropped_near = len(cleaned) - kept - dropped_exact
        stats: dict[str, int | str] = {
            "blocks_in": len(blocks),
            "kept": kept,
            "dropped_exact": dropped_exact,
            "dropped_near": dropped_near,
            "corrections": corrections,
            "masked_findings": masked_findings,
            "vault_refs": vault_refs,
            "needs_review": needs_review,
            "correct_ruleset_version": self.corrector.version,
            "mask_ruleset_version": self.masker.ruleset.version,
        }
        return CleanedDoc(
            source_file=source_file,
            kb_type=kb,
            blocks=cleaned,
            ruleset_version=self.masker.ruleset.version,
            stats=stats,
        )

    def _recheck_exact(self, cleaned: list[CleanedBlock], inserted: dict[int, str], source_file: str) -> None:
        """纠错后全量 md5 复查（§6.6）：纠错趋同块补判 dropped_exact。

        判定来源两类：
        ① 本文件内先出现块的纠错后文本（first-keep 保留先出现块，按块序处理）；
        ② 指纹库中他文件的指纹（跨文件趋同；同文件指纹一律自排除，防重放自判重）。
        判重后回滚本块阶段一写入的指纹（若该指纹无其他本文件块共享）。
        """
        seen: dict[str, int] = {}  # 纠错后 md5 → 首个 kept 块 block_index
        for cb in cleaned:
            if cb.status != STATUS_KEPT:
                continue
            h = md5_fingerprint(cb.text)
            fp = self.store.get(h)
            if h in seen:  # ① 文内纠错趋同：与先出现块完全一致
                self._drop_after_recheck(cb, h, inserted)
            elif fp is not None and fp.source_file != source_file:  # ② 跨文件指纹趋同
                self._drop_after_recheck(cb, h, inserted)
            else:
                seen[h] = cb.block_index

    def _drop_after_recheck(self, cb: CleanedBlock, dup_of: str, inserted: dict[int, str]) -> None:
        """复查判重落地：标记 dropped_exact、追加留痕、回滚本块指纹。"""
        cb.status = STATUS_DROPPED_EXACT
        cb.dup_of_md5 = dup_of
        cb.text = ""
        cb.actions.append(
            CleanAction(
                seq=len(cb.actions) + 1,
                stage=STAGE_DEDUP,
                rule_id="dedup.exact",
                before=f"(block:{cb.block_index})",
                after=f"(dup_of:{dup_of},hamming:0)",
                ts=now_iso(),
            )
        )
        own = inserted.get(cb.block_index)
        if own is not None and sum(1 for m in inserted.values() if m == own) == 1:
            # 仅当该指纹未被本文件其他块共享时回滚（first-keep 块仍持有指纹）
            self.store.remove(own)


def build_pipeline(
    kb_type: str = "product",
    *,
    hamming_max: int = 3,
    dedup_min_chars: int = 20,
    vault: Vault | None = None,
    corrector: Corrector | None = None,
    ruleset: RuleSet | None = None,
) -> CleaningPipeline:
    """组装默认三段流水线（单元测试 / 单机演示便捷入口）。

    默认：进程内指纹库 + 默认纠错规则集 + 默认脱敏规则集（不可逆模式，vault 传入则可逆）。
    """
    store = InMemoryFingerprintStore()
    return CleaningPipeline(
        store=store,
        exact=ExactDeduper(store),
        near=NearDeduper(store, hamming_max=hamming_max),
        corrector=corrector or default_corrector(),
        masker=MaskingEngine(ruleset or default_ruleset(), vault),
        kb_type=kb_type,
        dedup_min_chars=dedup_min_chars,
    )
