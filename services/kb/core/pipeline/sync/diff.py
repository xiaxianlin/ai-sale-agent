# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""增量 diff 引擎（FR-KB-017 增量更新，Phase 0 / 迭代 0.2）。

以 ``content_hash`` 为块身份，对「旧块集（当前已索引 chunk）」与
「新块集（重分块结果）」做双向差集，产出三分类增量计划 IncrementalPlan：

    unchanged（不变）  content_hash 命中 → 复用既有向量与 chunk_id，
                       跳过重嵌入（幂等重放零成本的核心）；
    to_upsert（入库）  未命中 → 新增 / 内容变更块，需重嵌入后 upsert；
    to_delete（失效）  旧块集中未被任何新块命中的残余 → 向量侧删除。

content_hash 口径（SR-KB-017 §6.2）：sha256(heading_path + 分隔符 + content)。
与 SR 的差异说明：SR §6.2 为 ``sha256(b.content + b.heading_path)`` 直接拼接，
本实现插入 ``\\x1f``（单元分隔符）防止「标题+正文」拼接歧义（(ab,c) 与
(a,bc) 碰撞），不改变「内容 + 标题层级联合指纹」的语义；条目标题等
item 级元数据不参与指纹——「改标题不动正文」因此判为不变，零重嵌入。

本模块为纯函数层：只做计算，不做任何持久化副作用（落库 / 删除由
engine.py 按 PG 先行原则执行），保证同一输入可反复重放（幂等）。

依据文档：docs/07-实现方案/KB/SR-KB-017-增量更新.md §2、§6.2
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..chunking.chunker import Chunk

#: 哈希拼接分隔符（US 单元分隔符）：标题层级与正文之间的无歧义边界
_HASH_SEPARATOR = "\x1f"


def compute_content_hash(content: str, heading_path: str = "") -> str:
    """块内容指纹：sha256(heading_path + US + content) 的十六进制串（64 字符）。

    确定性纯函数：同一段清洗后正文 + 同一标题层级路径恒得同一指纹，
    是 FR-KB-017 幂等重放与「未变块复用向量」的判定基础
    （对应 SR-KB-017 §4 knowledge_chunk.content_hash 列，CHAR(64)）。
    """
    digest = hashlib.sha256()
    digest.update(heading_path.encode("utf-8"))
    digest.update(_HASH_SEPARATOR.encode("ascii"))
    digest.update(content.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class IndexedChunk:
    """旧块集成员：块仓中该条目当前存活块的最小身份。

    任务口径（FR-KB-017 迭代 0.2）：旧块集 = content_hash + chunk_id；
    chunk_index / vector_status / indexed_status 为编排层附加信息
    （序校正与自愈重嵌判定用），diff 纯计算阶段可安全缺省。
    """

    chunk_id: int                      # 主键 = PG knowledge_chunk.id
    content_hash: str                  # 块内容指纹（compute_content_hash 产物）
    chunk_index: int = -1              # item 内当前顺序（-1 表示未知）
    vector_status: str = "indexed"     # pending | indexed | failed（removed 不入旧块集）
    indexed_status: str = ""           # 最近一次写入向量库的 item_status（''=从未写入）


@dataclass(frozen=True)
class UnchangedChunk:
    """不变块：content_hash 命中，直接复用既有向量与 chunk_id。

    new_index 为重分块后的新顺序；与 old_index 不一致时由编排层
    校正 chunk_index 位移（头部插入段落场景，SR-KB-017 §9 test_diff_index_shift）。
    """

    chunk_id: int
    content_hash: str
    old_index: int
    new_index: int
    vector_status: str = "indexed"     # 透传旧块状态：编排层据此判定是否需自愈重嵌
    indexed_status: str = ""           # 透传：编排层据此判定是否需翻转可见性


@dataclass
class PendingUpsert:
    """待入库的新块：新块集中未命中旧哈希的块（新增 / 内容变更）。

    chunk_id 为块仓分配主键后的回填位（diff 阶段恒为 None，
    engine 在 insert_chunk 之后回填），默认 None 保持「未落库」语义。
    """

    chunk: Chunk                       # 新块（重分块产物，含 content / heading_path）
    content_hash: str                  # 新块指纹（落库列）
    new_index: int                     # 新顺序
    reason: str = "added"              # added（新增）| changed（内容变更）
    chunk_id: int | None = None        # 块仓分配的主键（engine 回填）


@dataclass
class IncrementalPlan:
    """增量更新计划：三分类结果 + 计数（SR-KB-017 §6.2 DiffPlan 的迭代 0.2 形态）。"""

    unchanged: list[UnchangedChunk] = field(default_factory=list)
    to_upsert: list[PendingUpsert] = field(default_factory=list)
    to_delete: list[int] = field(default_factory=list)

    @property
    def stat(self) -> dict[str, int]:
        """计划计数：{reused, embedded, deleted}，供作业账目与管理统计（SR §4 stat 列）。"""
        return {
            "reused": len(self.unchanged),
            "embedded": len(self.to_upsert),
            "deleted": len(self.to_delete),
        }

    @property
    def live_chunk_ids(self) -> list[int]:
        """存活块全集（unchanged ∪ 已回填主键的 to_upsert），可见性切换作用范围。

        注意：to_upsert 尚未落库（chunk_id=None）时该块不计入——调用方
        应在 engine 回填主键之后再取本属性（对齐 SR §6.2 live_chunk_ids）。
        """
        merged = [u.chunk_id for u in self.unchanged]
        merged.extend(p.chunk_id for p in self.to_upsert if p.chunk_id is not None)
        return merged


def diff_chunks(old_chunks: Iterable[IndexedChunk], new_chunks: Iterable[Chunk]) -> IncrementalPlan:
    """计算增量计划：旧块集 vs 新块集按 content_hash 双向差集。

    分类规则：
    - 新块哈希命中旧块 → unchanged（旧块从待匹配表中弹出；新块集中
      出现重复哈希时仅首个命中复用，其余按未命中处理，保证块数守恒）；
    - 未命中且新顺序位在旧块顺序位集合内 → changed（内容变更，原地替换）；
    - 未命中且为全新顺序位 → added（新增块）；
    - 待匹配表中的旧块残余 → to_delete（内容消失 / 被替换，向量失效）。

    输出顺序稳定（unchanged / to_upsert 按新块顺序、to_delete 按 chunk_id
    升序），同一输入重放结果完全一致（幂等）。
    """
    old_by_hash: dict[str, IndexedChunk] = {}
    for old in old_chunks:
        old_by_hash.setdefault(old.content_hash, old)  # 同哈希旧块以先入为主（理论不重复）
    old_indexes = {old.chunk_index for old in old_by_hash.values()}

    unchanged: list[UnchangedChunk] = []
    to_upsert: list[PendingUpsert] = []
    for new_index, chunk in enumerate(new_chunks):
        content_hash = compute_content_hash(chunk.content, chunk.heading_path)
        hit = old_by_hash.pop(content_hash, None)
        if hit is not None:
            unchanged.append(UnchangedChunk(
                chunk_id=hit.chunk_id,
                content_hash=content_hash,
                old_index=hit.chunk_index,
                new_index=new_index,
                vector_status=hit.vector_status,
                indexed_status=hit.indexed_status,
            ))
        else:
            reason = "changed" if new_index in old_indexes else "added"
            to_upsert.append(PendingUpsert(
                chunk=chunk,
                content_hash=content_hash,
                new_index=new_index,
                reason=reason,
            ))
    to_delete = sorted(old_by_hash[content_hash].chunk_id for content_hash in old_by_hash)
    return IncrementalPlan(unchanged=unchanged, to_upsert=to_upsert, to_delete=to_delete)


__all__ = [
    "compute_content_hash",
    "IndexedChunk",
    "UnchangedChunk",
    "PendingUpsert",
    "IncrementalPlan",
    "diff_chunks",
]
