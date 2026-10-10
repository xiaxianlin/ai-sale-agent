# 知识产权归甲方所有（COM-IP-01）
"""清洗流水线共享数据结构（SR-KB-011 §6.1）。

定义块级 diff 留痕链节点（CleanAction）、清洗后块/文档归档结构（CleanedBlock / CleanedDoc）、
以及上游 FR-KB-010 解析产物的块契约（ParsedBlock）。本模块是清洗包的公共叶子模块，
被 dedup / correction / masking / pipeline 共同引用，禁止反向依赖（避免环状导入）。

依据文档：
- docs/07-实现方案/KB/SR-KB-011-数据清洗.md §6.1（diff 链）、§6.6（三段编排）
- docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── 留痕 stage 枚举：与 ingest_pipeline_log.stage 的 CHECK 枚举一致（SR-KB-011 §4）──
STAGE_DEDUP = "dedup"
STAGE_CORRECT = "correct"
STAGE_MASK = "mask"

# ── 块状态枚举：去重只标记不物理删除（决策 D2）──
STATUS_KEPT = "kept"
STATUS_DROPPED_EXACT = "dropped_exact"
STATUS_DROPPED_NEAR = "dropped_near"


def now_iso() -> str:
    """留痕时间戳：UTC ISO-8601（毫秒精度），供 CleanAction.ts 使用。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class CleanAction:
    """diff 链节点：一次原子清洗动作的定位 + 前后对照 + 规则来源（SRS §12.2 可回溯）。

    约定（防明文 PII 落入留痕链，SR-KB-011 §4「不落任何明文 PII」）：
    - mask 段 before 固定为占位符：可逆规则为 "(vault:{id})"，不可逆规则为 "(masked:{rule_id})"；
    - correct 段 before / after 为原文片段（纠错白名单规则不涉及客户 PII 原文改写）；
    - dedup 段 before 为 "(block:{i})"，after 为判重指针 "(dup_of:{md5},hamming:{d})"。
    """

    seq: int  # 块内严格递增序号；按 seq 顺序重放可从原始块复现清洗后块
    stage: str  # dedup | correct | mask（同 ingest_pipeline_log 枚举）
    rule_id: str  # 规则来源（dedup.exact / typo:帐号 / mask.email …）
    before: str  # 动作前（mask 段为占位符，不落明文）
    after: str  # 动作后
    ts: str  # UTC ISO 时间戳


@dataclass
class CleanedBlock:
    """清洗后块：status 标记去重结果；dropped 块正文恒空，原文回溯走 parse 归档（决策 D2）。"""

    block_index: int  # 锚定 p0/parse-output 归档中的原始块（§12.2 回溯入口）
    status: str = STATUS_KEPT  # kept | dropped_exact | dropped_near
    dup_of_md5: str | None = None  # 判重指向的保留块指纹
    text: str = ""  # 清洗后正文（已脱敏）；dropped 块恒空
    actions: list[CleanAction] = field(default_factory=list)  # 块级 diff 留痕链


@dataclass
class CleanedDoc:
    """清洗后文档：一次清洗任务的归档产物（p0/clean-output/{md5}.json 的内存形态）。"""

    source_file: str  # 原始材料文件名（回溯锚点）
    kb_type: str  # 六大知识库类型（product/training/…，近似去重仅同库判重）
    blocks: list[CleanedBlock] = field(default_factory=list)
    ruleset_version: str = ""  # 脱敏规则集版本（NFR-SEC-01 决策 D3 版本化）
    stats: dict[str, int | str] = field(default_factory=dict)  # 三段计数（§4 action_detail 口径）


@dataclass
class ParsedBlock:
    """上游解析块（FR-KB-010 产物契约）：文本块用 text，表格块用 table 二维单元格。"""

    block_index: int
    type: str = "text"  # text | table
    text: str = ""
    table: list[list[str]] | None = None  # 仅 type=table 时有效


def table_repr(table: list[list[str]]) -> str:
    """表格块的文本化表示（去重指纹与脱敏扫描均基于该序列化文本）。"""
    return "\n".join(" | ".join(row) for row in table)
