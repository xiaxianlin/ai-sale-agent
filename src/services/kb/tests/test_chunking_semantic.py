# 知识产权归甲方所有（COM-IP-01）
"""语义边界与长度兜底用例（FR-KB-012 / SR-KB-012 §9：test_semantic_drop /
test_hard_limit_fallback / test_bge_degrade 的检测器单元部分）。

覆盖：mock 嵌入下的句向量骤降切分（boundary_reasons 含 semantic）、
检测器缺省（None）时的三级边界、纯长度兜底（hard_limit）、单句超长推进、
SimilarityBoundary 检测器与工具函数单元行为。
"""
from __future__ import annotations

from services.kb.core.pipeline.chunking import (
    BufferedSentence,
    BoundaryContext,
    ChunkConfig,
    SemanticChunker,
    SimilarityBoundary,
    cosine,
    split_sentences,
)
from services.kb.core.pipeline.chunking.boundary import LengthFallback
from services.kb.tests._chunking_testlib import (
    FakeDoc,
    cn_sentence,
    para,
    tokens_of,
    topic_sentence,
)

# mock 嵌入：首字「甲」→ 主题 A 向量 (1,0)；首字「乙」→ 主题 B 向量 (0,1)。
# 同主题句间相似度 1.0、跨主题 0.0 → 主题切换处骤降落差 1.0（≥ 默认阈值 0.15）。
_TOPIC_VECTORS = {"甲": (1.0, 0.0), "乙": (0.0, 1.0)}


def _mock_embed(sentences: list[str]) -> list[tuple[float, float]]:
    return [_TOPIC_VECTORS[s[0]] for s in sentences]


def _two_topic_doc() -> FakeDoc:
    """单段落拼接两主题：甲甲甲乙乙乙（各句 token 数一致，可精确推演缓冲行为）。"""
    text = "".join([
        topic_sentence("甲", 0), topic_sentence("甲", 1), topic_sentence("甲", 2),
        topic_sentence("乙", 0), topic_sentence("乙", 1), topic_sentence("乙", 2),
    ])
    return FakeDoc(blocks=[para(text)])


def test_semantic_drop_cut_with_mock_detector() -> None:
    """两主题拼接段落：骤降点处回溯切分，boundary_reasons 含 semantic。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=2 * tokens_of(topic_sentence("甲", 0)))
    chunker = SemanticChunker(
        config=cfg,
        similarity_detector=SimilarityBoundary(embed_fn=_mock_embed),
    )
    chunks, metrics = chunker.chunk(_two_topic_doc())
    assert len(chunks) >= 3
    # 含主题 B 首句的块由语义切点开启（骤降点在其前）
    b1_chunk = next(c for c in chunks if topic_sentence("乙", 0) in c.content)
    assert "semantic" in b1_chunk.boundary_reasons
    # 主题 A 与 B 不混入同一块（语义边界生效的直接证据）
    for chunk in chunks:
        has_a = topic_sentence("甲", 2) in chunk.content
        has_b = topic_sentence("乙", 0) in chunk.content
        assert not (has_a and has_b)
    assert metrics["boundary_mix"].get("semantic", 0) >= 1
    assert metrics["lossless_blocks"] is True


def test_similarity_default_none_degrades_to_three_levels() -> None:
    """未注入句向量检测器（默认 None）：退化为三级边界，无 semantic 切点，仍完整出块。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=2 * tokens_of(topic_sentence("甲", 0)))
    chunks, metrics = SemanticChunker(config=cfg).chunk(_two_topic_doc())
    assert len(chunks) >= 2
    assert all("semantic" not in c.boundary_reasons for c in chunks)
    assert metrics["boundary_mix"].get("semantic") is None
    assert metrics["lossless_blocks"] is True
    assert metrics["degrade"] == []  # 未启用 ③ 级是配置状态，不是运行时降级


def test_hard_limit_fallback() -> None:
    """单主题长段落（无骤降点、超上限）：在不超过上限的最后句边界切分，reason=hard_limit。"""
    text = "".join(cn_sentence(i) for i in range(8))  # 8 句同主题，无边界
    doc = FakeDoc(blocks=[para(text)])
    per_sentence = tokens_of(cn_sentence(0))
    cfg = ChunkConfig(min_tokens=1, max_tokens=2 * per_sentence)
    chunks, metrics = SemanticChunker(config=cfg).chunk(doc)
    assert len(chunks) == 4  # 每块恰好 2 句（2×13 token）
    for chunk in chunks:
        assert chunk.tokens <= cfg.max_tokens  # 兜底切分保证不超上限
    for chunk in chunks[1:]:
        assert "hard_limit" in chunk.boundary_reasons  # 首块自然开始，无切点依据
    assert metrics["boundary_mix"]["hard_limit"] == 3
    assert metrics["lossless_blocks"] is True


def test_single_overlong_sentence_progresses() -> None:
    """单句即超上限：以首句为界强制出块（句内不切），账目不丢、超限交指标暴露。"""
    sentence = cn_sentence(0, han=100)  # 远超 max_tokens
    doc = FakeDoc(blocks=[para(sentence)])
    cfg = ChunkConfig(min_tokens=1, max_tokens=26)
    chunks, metrics = SemanticChunker(config=cfg).chunk(doc)
    assert len(chunks) == 1
    assert chunks[0].tokens > cfg.max_tokens  # 宁可超限出块也不阻塞/不句内切
    assert metrics["lossless_blocks"] is True
    assert metrics["token_dist"]["max"] == chunks[0].tokens
    assert metrics["token_dist"]["out_of_range_rate"] == 1.0
    assert metrics["out_of_range_alert"] is True


def test_degrade_to_struct_only_keeps_lossless() -> None:
    """BGE 不可用降级：degrade_to_struct_only 后按三级边界出块并留痕（可重复调用不重复留痕）。"""
    cfg = ChunkConfig(min_tokens=1, max_tokens=2 * tokens_of(topic_sentence("甲", 0)))
    chunker = SemanticChunker(
        config=cfg,
        similarity_detector=SimilarityBoundary(embed_fn=_mock_embed),
    )
    chunker.degrade_to_struct_only()
    chunker.degrade_to_struct_only()  # 幂等：留痕不重复
    assert chunker.similarity_boundary is None
    chunks, metrics = chunker.chunk(_two_topic_doc())
    assert all("semantic" not in c.boundary_reasons for c in chunks)
    assert metrics["degrade"] == ["bge_unavailable->struct_only"]
    assert metrics["lossless_blocks"] is True


# ---- 检测器与工具函数单元行为 --------------------------------------------------


def test_similarity_boundary_flag_drops() -> None:
    """flag_drops：前序窗口基线落差 ≥ sim_drop 判骤降（SR-KB-012 §6.2 公式直测）。"""
    detector = SimilarityBoundary(embed_fn=_mock_embed)
    cfg = ChunkConfig(sim_drop=0.15, sim_window=3)
    sentences = [
        topic_sentence("甲", 0), topic_sentence("甲", 1), topic_sentence("甲", 2),
        topic_sentence("乙", 0),
    ]
    # 甲甲甲相似度 1.0；甲→乙骤降至 0.0 → 第 3 句后为切点
    flags = detector.flag_drops(sentences, cfg)
    # 精确断言：唯一骤降点在第 3 句之后（flags 覆盖相邻对，长度 = 句数 - 1）
    assert len(flags) == len(sentences) - 1
    assert flags == [False, False, True]


def test_similarity_boundary_detect_finds_latest_cut() -> None:
    """detect：回溯缓冲内最近的骤降切点（cut=切点位置，发射 [0:cut)）。"""
    detector = SimilarityBoundary(embed_fn=_mock_embed)
    cfg = ChunkConfig(sim_drop=0.15, sim_window=3)
    sentences = [
        BufferedSentence(0, "s0", 10, drop_before=False),
        BufferedSentence(0, "s1", 10, drop_before=True),   # s1 之前为骤降点 → 候选切点
        BufferedSentence(0, "s2", 10, drop_before=False),
        BufferedSentence(0, "s3", 10, drop_before=True),   # 更近的骤降点
    ]
    context = BoundaryContext(block=None, block_index=0, sentences=tuple(sentences),
                              buffer_tokens=40, config=cfg)
    hit = detector.detect(context)
    assert hit is not None
    assert hit.reason == "semantic"
    assert hit.detail["cut"] == 3  # 最近骤降点：发射 [s0,s1,s2)，s3 起留缓冲


def test_similarity_boundary_disabled_by_default() -> None:
    """可插拔缺省态：embed_fn=None → 未启用（flag_drops 全 False、detect 恒 None）。"""
    detector = SimilarityBoundary()
    assert detector.enabled is False
    cfg = ChunkConfig()
    assert detector.flag_drops(["句子一。", "句子二。"], cfg) == [False]
    context = BoundaryContext(
        block=None, block_index=0,
        sentences=(BufferedSentence(0, "s", 1, drop_before=True),),
        buffer_tokens=1, config=cfg,
    )
    assert detector.detect(context) is None


def test_length_fallback_prefers_max_prefix_and_forces_single() -> None:
    """长度兜底：取不超过上限的最长句前缀；单句超长时强制以首句为界。"""
    fallback = LengthFallback()
    cfg = ChunkConfig(max_tokens=26)
    sentences = (BufferedSentence(0, "s0", 13, drop_before=False),
                 BufferedSentence(0, "s1", 13, drop_before=False),
                 BufferedSentence(0, "s2", 13, drop_before=False))
    hit = fallback.detect(BoundaryContext(block=None, block_index=0, sentences=sentences,
                                          buffer_tokens=39, config=cfg))
    assert hit is not None and hit.reason == "hard_limit" and hit.detail["cut"] == 2
    # 单句即超上限：cut 强制为 1，保证主循环推进
    overlong = (BufferedSentence(0, "s0", 101, drop_before=False),)
    hit2 = fallback.detect(BoundaryContext(block=None, block_index=0, sentences=overlong,
                                           buffer_tokens=101, config=cfg))
    assert hit2 is not None and hit2.detail["cut"] == 1


def test_cosine_zero_vector_guard() -> None:
    """零向量防护：余弦相似度返回 0.0，避免 NaN 污染骤降判定。"""
    assert cosine((0.0, 0.0), (1.0, 1.0)) == 0.0
    assert abs(cosine((1.0, 0.0), (1.0, 0.0)) - 1.0) < 1e-9
    assert abs(cosine((1.0, 0.0), (0.0, 1.0))) < 1e-9


def test_split_sentences_merges_short_fragments() -> None:
    """切句：过短句并入邻句；切句结果按序拼接还原原文（账目对账基础）。"""
    text = "短。这是足够长的第一句话。这也是足够长的第二句话。"
    sentences = split_sentences(text, min_chars=6)
    assert len(sentences) == 2
    assert sentences[0].startswith("短。")  # 过短句并入后句
    assert "".join(sentences) == text  # 拼接无损
