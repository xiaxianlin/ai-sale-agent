# 知识产权归甲方所有（COM-IP-01）
"""清洗三段编排单元测试：三段留痕链、标记不删除、表格/短块豁免、纠错复查、
幂等重放、脱敏闸门零残留（SR-KB-011 §9 各用例的编排级验证）。"""
from __future__ import annotations

import re

from services.kb.core.pipeline.cleaning import (
    STATUS_DROPPED_EXACT,
    STATUS_DROPPED_NEAR,
    STATUS_KEPT,
    STAGE_CORRECT,
    STAGE_MASK,
    CleanAction,
    Fingerprint,
    InMemoryVault,
    ParsedBlock,
    SimHash,
    build_pipeline,
    md5_fingerprint,
)

# PII 复扫正则（与引擎默认规则同式）：留痕链 / 清洗产物零命中判定用
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,4}[\s\-]?)?\d{7,13}(?!\d)")
_IDNUM_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


class TestPipelineEndToEnd:
    """三段编排端到端：去重 → 纠错 → 脱敏 + stats 统计。"""

    def test_three_stage_end_to_end(self) -> None:
        pipe = build_pipeline(kb_type="training", vault=InMemoryVault())
        blocks = [
            ParsedBlock(0, "text", text="请把报价发到 john.doe@example.com，帐号信息稍后确认。"),
            ParsedBlock(1, "text", text="完全独立的一段文本，讨论海运周期与包装方式的细节安排。"),
            ParsedBlock(2, "table", table=[["参数", "数值"], ["重量", "5公斤"], ["交期", "20天"]]),
        ]
        doc = pipe.run(blocks, "新文件.docx")
        assert all(cb.status == STATUS_KEPT for cb in doc.blocks)
        # 块 0：纠错（帐号→账号）与脱敏（邮箱→j***@example.com）先后生效
        assert doc.blocks[0].text == "请把报价发到 j***@example.com，账号信息稍后确认。"
        # 块 2：表格逐单元格纠错后序列化（原子块契约）
        assert "5kg" in doc.blocks[2].text and "5公斤" not in doc.blocks[2].text
        # 留痕链：块 0 含 correct + mask 两段动作，seq 严格递增
        stages = [a.stage for a in doc.blocks[0].actions]
        assert STAGE_CORRECT in stages and STAGE_MASK in stages
        assert [a.seq for a in doc.blocks[0].actions] == list(range(1, len(doc.blocks[0].actions) + 1))
        # stats 与规则集版本
        assert doc.stats["blocks_in"] == 3 and doc.stats["kept"] == 3
        assert doc.stats["corrections"] >= 1 and doc.stats["masked_findings"] >= 1
        assert doc.ruleset_version == "mask.rules@v1"
        assert doc.stats["mask_ruleset_version"] == "mask.rules@v1"

    def test_exact_dup_crossfile(self) -> None:
        pipe = build_pipeline()
        text = "两份文件里完全一致的一段商务条款文本，用于全量去重验证场景。"
        pipe.run([ParsedBlock(0, "text", text=text)], "旧文件.docx")
        # 首尾空白不影响判重（md5 规范化口径）
        doc2 = pipe.run([ParsedBlock(0, "text", text=f"  {text}  ")], "新文件.docx")
        cb = doc2.blocks[0]
        assert cb.status == STATUS_DROPPED_EXACT
        assert cb.dup_of_md5 == md5_fingerprint(text)  # dup_of 指向先入库块指纹
        assert doc2.stats["dropped_exact"] == 1

    def test_near_dup_marked_not_deleted(self) -> None:
        pipe = build_pipeline()
        target = "这是一段足够长的正文文本，用于验证近似去重的段查表召回路径是否按预期工作。"
        seed_sim = SimHash(target).value ^ 1  # 与目标块汉明距离恰为 1 的既有指纹
        pipe.store.add(
            Fingerprint(
                md5="seed-near-md5",
                simhash=seed_sim,
                kb_type="product",
                source_file="旧文件.docx",
                block_index=0,
            )
        )
        doc = pipe.run([ParsedBlock(0, "text", text=target)], "新文件.docx")
        cb = doc.blocks[0]
        assert cb.status == STATUS_DROPPED_NEAR
        assert cb.dup_of_md5 == "seed-near-md5"
        assert cb.text == ""  # dropped 块正文恒空：只标记不物理删除（决策 D2）
        action = cb.actions[0]
        assert action.stage == "dedup" and action.rule_id == "dedup.near"
        assert action.before == "(block:0)"
        assert action.after == "(dup_of:seed-near-md5,hamming:1)"


class TestExemption:
    """§9 test_table_short_exempt：表格与短块豁免近似去重。"""

    def test_table_and_short_exempt_from_near(self) -> None:
        pipe = build_pipeline()
        base_table = [["参数", "数值"], ["重量", "5公斤"], ["长度", "120厘米"]]
        pipe.run(
            [
                ParsedBlock(0, "table", table=[row[:] for row in base_table]),
                ParsedBlock(1, "text", text="很短的相似文本甲"),
            ],
            "旧文件.docx",
        )
        # 相似（非相同）表格 + 相似短块：全量 md5 不同、近似判重被豁免 → 均保留
        similar_table = [["参数", "数值"], ["重量", "6公斤"], ["长度", "121厘米"]]
        doc = pipe.run(
            [
                ParsedBlock(0, "table", table=similar_table),
                ParsedBlock(1, "text", text="很短的相似文本乙"),
            ],
            "新文件.docx",
        )
        assert doc.blocks[0].status == STATUS_KEPT  # 表格豁免近似去重
        assert doc.blocks[1].status == STATUS_KEPT  # 短块（<20 字）豁免


class TestRecheckAfterCorrection:
    """D1：纠错后全量 md5 复查，捕捉「纠错趋同」漏网重复（first-keep）。"""

    def test_correction_convergence_dropped(self) -> None:
        pipe = build_pipeline()
        blocks = [
            ParsedBlock(0, "text", text="客户的帐号已注销。"),  # 纠错后 → 与块 1 完全一致
            ParsedBlock(1, "text", text="客户的账号已注销。"),  # 本就是纠错后形态
        ]
        doc = pipe.run(blocks, "趋同.docx")
        assert doc.blocks[0].status == STATUS_KEPT  # first-keep：保留先出现块
        cb1 = doc.blocks[1]
        assert cb1.status == STATUS_DROPPED_EXACT
        assert cb1.dup_of_md5 == md5_fingerprint("客户的账号已注销。")
        assert cb1.actions[0].rule_id == "dedup.exact"  # 复查补判留痕
        assert cb1.actions[0].after == f"(dup_of:{cb1.dup_of_md5},hamming:0)"

    def test_infile_identical_originals_second_dropped(self) -> None:
        # 文件内两块原文完全一致：阶段一自排除不判重，由复查阶段按 first-keep 补判
        pipe = build_pipeline()
        text = "本文件内部完全重复的一段文本，出现在两个块里，应当只保留首个块。"
        doc = pipe.run(
            [ParsedBlock(0, "text", text=text), ParsedBlock(1, "text", text=text)], "重复.docx"
        )
        assert doc.blocks[0].status == STATUS_KEPT
        assert doc.blocks[1].status == STATUS_DROPPED_EXACT
        # 共享指纹不回滚：先出现块仍持有指纹，后续文件同文本仍可判重
        assert pipe.store.get(md5_fingerprint(text)) is not None


class TestIdempotentReplay:
    """§9 test_replay_idempotent：同任务重放不自判重、指纹零新增。"""

    def test_replay_same_file_idempotent(self) -> None:
        pipe = build_pipeline()
        blocks = [ParsedBlock(0, "text", text="幂等重放验证文本块：内容固定，重复投递不应产生新的去重判定。")]
        doc1 = pipe.run(blocks, "重放.docx")
        n_fingerprints = len(pipe.store)
        doc2 = pipe.run(blocks, "重放.docx")
        assert [cb.status for cb in doc1.blocks] == ["kept"]
        assert [cb.status for cb in doc2.blocks] == ["kept"]  # 本文件指纹自排除
        assert len(pipe.store) == n_fingerprints  # md5 唯一键：指纹零新增
        assert doc2.stats["kept"] == 1 and doc2.stats["dropped_exact"] == 0


class TestTraceChainIntegrity:
    """§9 test_diff_chain_replay / test_clean_before_chunk：留痕链完整性 + 脱敏闸门。"""

    def _pii_doc(self):  # type: ignore[no-untyped-def]
        pipe = build_pipeline(kb_type="training", vault=InMemoryVault())
        blocks = [
            ParsedBlock(
                0,
                "text",
                text="联系邮箱 john.doe@example.com，电话 +86 13812345678，"
                "地址广东省深圳市南山区科技园路1号，身份证110101199003077758。",
            ),
            ParsedBlock(1, "text", text="常规条款文本，不包含任何个人信息，用于校验留痕链的完整性约束。"),
        ]
        return pipe, pipe.run(blocks, "链路.docx")

    def test_chain_seq_strictly_increasing_with_ts(self) -> None:
        _, doc = self._pii_doc()
        for cb in doc.blocks:
            seqs = [a.seq for a in cb.actions]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # seq 块内严格递增
            assert all(a.ts for a in cb.actions)  # 时间戳齐全（可回溯）
            assert all(a.stage in ("dedup", "correct", "mask") for a in cb.actions)

    def test_chain_contains_no_plaintext_pii(self) -> None:
        # mask 段 before 为占位符：整条留痕链（rule_id/before/after）做 PII 正则复扫零命中
        _, doc = self._pii_doc()
        chain_text = repr([(a.rule_id, a.before, a.after) for cb in doc.blocks for a in cb.actions])
        for pattern in (_EMAIL_RE, _PHONE_RE, _IDNUM_RE):
            assert not pattern.search(chain_text), f"留痕链泄露明文 PII：{pattern.pattern}"

    def test_clean_before_chunk_gate(self) -> None:
        # 决策 D3：进入 FR-KB-012 分块前，清洗产物经引擎复扫零疑似 PII 残留
        pipe, doc = self._pii_doc()
        for cb in doc.blocks:
            if cb.status == STATUS_KEPT:
                assert pipe.masker.scan_only(cb.text, kb_type="training") == []

    def test_correct_actions_replay_reproduces_text(self) -> None:
        # §12.2 抽样核对脚本化：按 seq 重放纠错动作可复现清洗后文本（product 库无价格遮蔽干扰）
        pipe = build_pipeline(kb_type="product")
        original = "客户帐号于２０２６.１.５登陆，样品重5公斤，价格  １２００元。"
        doc = pipe.run([ParsedBlock(0, "text", text=original)], "回放.docx")
        replayed = original
        for action in (a for a in doc.blocks[0].actions if a.stage == STAGE_CORRECT):
            replayed = replayed.replace(action.before, action.after)
        assert replayed == doc.blocks[0].text

    def test_stats_consistency(self) -> None:
        pipe = build_pipeline(vault=InMemoryVault())
        blocks = [
            ParsedBlock(0, "text", text="联系 john@x.com 确认帐号问题，此块内容足够长以参与近似去重。"),
            ParsedBlock(1, "text", text="此块与首块完全不同，仅用于统计口径验证。"),
        ]
        doc = pipe.run(blocks, "统计.docx")
        s = doc.stats
        assert s["blocks_in"] == 2
        assert s["kept"] + s["dropped_exact"] + s["dropped_near"] == s["blocks_in"]
        assert s["corrections"] == sum(
            1 for cb in doc.blocks for a in cb.actions if a.stage == STAGE_CORRECT
        )
        vault_actions = [
            a for cb in doc.blocks for a in cb.actions if a.stage == STAGE_MASK and a.before.startswith("(vault:")
        ]
        assert s["vault_refs"] == len(vault_actions)
