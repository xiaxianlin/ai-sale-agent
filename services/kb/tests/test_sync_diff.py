# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
"""增量 diff 引擎单元测试（FR-KB-017 §9，任务口径：三分类正确性）。

覆盖（SR-KB-017 §9 测试要点 diff 部分）：
- 改标题不动正文 → 全部不变（content_hash 不含条目级元数据）；
- 改正文 → 仅该块 to_upsert（changed），其余复用；
- 删块 → 残余旧块进 to_delete；
- 头部插入段落 → hash 命中块仅校正序（chunk_index 位移）不重嵌；
- 真实 SemanticChunker 重分块确定性（同正文重跑 diff 全命中）；
- 哈希分隔符无歧义 / 空新块集全删 / 计数账目。

实现方案：docs/07-实现方案/KB/SR-KB-017-增量更新.md §6.2、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from services.kb.core.pipeline.chunking.chunker import Chunk
from services.kb.core.pipeline.sync import (
    IncrementalPlan,
    IndexedChunk,
    compute_content_hash,
    diff_chunks,
    make_text_chunk_fn,
)


def _chunk(content: str, heading_path: str = "") -> Chunk:
    """构造最小分块产物（diff 只消费 content / heading_path 两个字段）。"""
    return Chunk(content=content, heading_path=heading_path)


def _indexed(chunks: list[Chunk]) -> list[IndexedChunk]:
    """把一次分块结果固化为旧块集（模拟「当前已索引 chunk」）。"""
    return [
        IndexedChunk(
            chunk_id=index + 1,
            content_hash=compute_content_hash(c.content, c.heading_path),
            chunk_index=index,
        )
        for index, c in enumerate(chunks)
    ]


BODY = (
    "# 产品概述\n\n本产品是一款高端工业支架，适用于重型设备。\n\n"
    "## 技术参数\n\n材质为铝合金，承重 200 公斤。\n\n"
    "## 交付说明\n\n下单后 30 天内发货，含安装说明。"
)


# ── 三分类正确性 ────────────────────────────────────────────────────────────


def test_diff_title_only_all_unchanged() -> None:
    """改标题不动正文 → 全部不变：content_hash 只含块内容 + 标题层级路径。

    条目标题是 item 级元数据，不进入块身份；用真实 SemanticChunker
    对同一正文重分块（文档 title 换掉）应得到完全一致的块集。
    """
    chunk_fn = make_text_chunk_fn()
    old = _indexed(chunk_fn(BODY))
    new = chunk_fn(BODY)  # 条目标题已改（不作为 chunk 字段传入），正文不动

    plan = diff_chunks(old, new)

    assert isinstance(plan, IncrementalPlan)
    assert len(plan.to_upsert) == 0
    assert len(plan.to_delete) == 0
    assert len(plan.unchanged) == len(old) == 3
    assert [u.chunk_id for u in plan.unchanged] == [1, 2, 3]
    assert plan.stat == {"reused": 3, "embedded": 0, "deleted": 0}


def test_diff_body_change_yields_single_upsert() -> None:
    """改正文 → 仅该块进 to_upsert（reason=changed），其余块复用。"""
    old = _indexed([_chunk("第一段内容"), _chunk("第二段内容"), _chunk("第三段内容")])
    new = [_chunk("第一段内容"), _chunk("第二段内容（已修改）"), _chunk("第三段内容")]

    plan = diff_chunks(old, new)

    assert len(plan.to_upsert) == 1
    pending = plan.to_upsert[0]
    assert pending.reason == "changed"
    assert pending.new_index == 1
    assert pending.chunk_id is None  # 尚未落库：主键由块仓回填
    assert pending.content_hash == compute_content_hash("第二段内容（已修改）")
    assert [u.chunk_id for u in plan.unchanged] == [1, 3]
    assert plan.to_delete == [2]  # 被替换的旧块失效
    assert plan.stat == {"reused": 2, "embedded": 1, "deleted": 1}


def test_diff_removed_block_yields_delete() -> None:
    """删块 → 旧块集中未被命中的残余进 to_delete（按 chunk_id 升序）。"""
    old = _indexed([_chunk("段甲"), _chunk("段乙"), _chunk("段丙"), _chunk("段丁")])
    new = [_chunk("段甲"), _chunk("段丙")]  # 删掉乙、丁两段

    plan = diff_chunks(old, new)

    assert plan.to_delete == [2, 4]
    assert [u.chunk_id for u in plan.unchanged] == [1, 3]
    assert len(plan.to_upsert) == 0


def test_diff_head_insert_only_index_shift() -> None:
    """头部插入一段 → 旧块全部 hash 命中（仅 chunk_index 位移），新增块唯一待嵌入。

    reason 口径：插入块的顺序位 0 落在旧块顺序位集合内，按「内容变更」
    归类（位置启发式，仅作账目参考）；分类核心是三集合划分正确。
    """
    old = _indexed([_chunk("旧一段"), _chunk("旧二段"), _chunk("旧三段")])
    new = [_chunk("新插入段"), _chunk("旧一段"), _chunk("旧二段"), _chunk("旧三段")]

    plan = diff_chunks(old, new)

    assert len(plan.to_upsert) == 1
    assert plan.to_upsert[0].new_index == 0
    assert plan.to_upsert[0].reason == "changed"
    # 命中块不重嵌：仅记录 new_index 位移（编排层据此校正 chunk_index）
    assert [(u.chunk_id, u.old_index, u.new_index) for u in plan.unchanged] == [
        (1, 0, 1),
        (2, 1, 2),
        (3, 2, 3),
    ]
    assert plan.to_delete == []


def test_diff_empty_new_set_deletes_all() -> None:
    """新块集为空（正文清空）→ 旧块全部失效。"""
    old = _indexed([_chunk("段甲"), _chunk("段乙")])

    plan = diff_chunks(old, [])

    assert plan.unchanged == []
    assert plan.to_upsert == []
    assert plan.to_delete == [1, 2]


def test_diff_empty_old_set_all_added() -> None:
    """旧块集为空（新条目首次入库）→ 全部新增。"""
    plan = diff_chunks([], [_chunk("段甲"), _chunk("段乙")])

    assert [p.reason for p in plan.to_upsert] == ["added", "added"]
    assert plan.to_delete == []
    assert plan.unchanged == []


def test_diff_duplicate_hash_keeps_chunk_accounting() -> None:
    """新块集出现重复内容：仅首个命中复用，其余按新增处理（块数守恒）。"""
    old = _indexed([_chunk("重复段")])
    new = [_chunk("重复段"), _chunk("重复段")]

    plan = diff_chunks(old, new)

    assert [u.chunk_id for u in plan.unchanged] == [1]
    assert len(plan.to_upsert) == 1
    assert plan.to_delete == []


def test_diff_status_fields_passthrough() -> None:
    """旧块状态（vector_status / indexed_status）透传到不变块，供编排层判定。"""
    old = [
        IndexedChunk(
            chunk_id=7,
            content_hash=compute_content_hash("段甲"),
            chunk_index=0,
            vector_status="pending",     # 上轮降级滞留：需自愈重嵌
            indexed_status="",
        )
    ]

    plan = diff_chunks(old, [_chunk("段甲")])

    assert plan.unchanged[0].vector_status == "pending"
    assert plan.unchanged[0].indexed_status == ""


# ── 哈希口径 ────────────────────────────────────────────────────────────────


def test_content_hash_covers_heading_path() -> None:
    """同正文不同标题层级 → 指纹不同（标题改写会触发该块重嵌入）。"""
    assert compute_content_hash("正文", "产品/参数") != compute_content_hash("正文", "产品/交付")


def test_content_hash_separator_disambiguates() -> None:
    """分隔符消除拼接歧义：(标题=ab, 正文=c) 与 (标题=a, 正文=bc) 不得碰撞。"""
    assert compute_content_hash("c", "ab") != compute_content_hash("bc", "a")


def test_content_hash_deterministic_and_hex64() -> None:
    """指纹确定性（幂等重放前提）+ 64 位十六进制（对齐 CHAR(64) 列）。"""
    first = compute_content_hash("同一段正文", "同/路径")
    second = compute_content_hash("同一段正文", "同/路径")
    assert first == second
    assert len(first) == 64
    int(first, 16)  # 合法十六进制串


def test_live_chunk_ids_after_backfill() -> None:
    """存活块全集：unchanged + 已回填主键的 to_upsert（可见性切换范围）。"""
    old = _indexed([_chunk("段甲")])
    plan = diff_chunks(old, [_chunk("段甲"), _chunk("段乙")])
    plan.to_upsert[0].chunk_id = 99  # 模拟块仓回填

    assert plan.live_chunk_ids == [1, 99]
