# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Phase 0 端到端入库编排（迭代 0.3 收口：六库入库主线，§16.1 KPI 预跑的执行底座）。

IngestionOrchestrator 驱动单个素材文件的完整入库链路（全部组件为既有冻结
接口的只读引用 + 鸭子类型注入，内存实现可全链跑通）：

    素材文件
      → ① 解析（parsers.registry：扩展名路由到五种格式解析器，输出 UnstructuredDoc）
      → ② 清洗（cleaning.build_pipeline：去重→纠错→脱敏三段，dropped 块不进分块）
      → ③ 分块（chunking.SemanticChunker：四级边界 + 账目对账自检）
      → ④ 条目化（models.item.KnowledgeItemCreate：kb_type 路由注入默认 metadata，
            六库内容模型（discriminated union）构造期全量校验）
      → ⑤ 嵌入（embedding.EmbeddingClient：embed_batch 批量契约）
      → ⑥ 向量入库（vectorstore.VectorStore：VectorRecord 按 chunk_id 幂等 upsert）
      → ⑦ ingest 留痕（reporting.IngestLogRecord + InMemoryIngestLogStore 语义，
            parse/dedup/correct/mask/chunk/embed/index 逐段一条，失败段 status=failed）

批次模型 BatchManifest：素材清单 + 每文件状态（success/failed + 中文原因）
+ 统计（文件数/块数/分块数/入库块数），供覆盖率核算（coverage.py）与
§16.1 KPI 预跑（tools/precheck/phase0_kpi.py）消费。

设计口径：
- 分块账目对账（lossless_blocks）失守 → 本文件按 failed 阻断入库并留痕
  （SR-KB-012 §6.5 CHUNK-2001 编排层阻断语义）；
- 单文件失败不中断批次（部分成功策略与 FR-KB-021 导入口径一致）；
- 增量更新（FR-KB-017）走独立引擎；本编排为首次全量入库口径（直接发布，
  item_status=published），chunk_id 由注入的分配器顺序发放（内存形态从 1 起，
  对齐 PG IDENTITY；生产由 knowledge_chunk.id 回填）。

依据文档：docs/05-项目管理/03-开发计划.md §4.1（迭代 0.3 退出条件）；
         docs/01-需求文档/AI外贸销售智能体系统_需求规格说明书.md §16.1。
"""
from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.kb.core.pipeline.chunking.chunker import Chunk, SemanticChunker
from services.kb.core.pipeline.cleaning.base import CleanedDoc, ParsedBlock
from services.kb.core.pipeline.cleaning.pipeline import CleaningPipeline, build_pipeline
from services.kb.core.pipeline.embedding.base import EmbeddingClient
from services.kb.core.pipeline.parsers.base import UnstructuredDoc
from services.kb.core.pipeline.parsers.registry import REGISTRY, ParserRegistry
from services.kb.core.reporting.cleaning_report import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    IngestLogRecord,
    IngestLogStore,
    InMemoryIngestLogStore,
)
from services.kb.core.vectorstore.base import (
    ITEM_STATUS_PUBLISHED,
    KB_TYPES,
    VectorRecord,
    VectorStore,
)
from services.kb.models.item import KnowledgeItem, KnowledgeItemCreate

# ── 常量：批次/文件状态与留痕阶段 ─────────────────────────────────────────────

#: 批次终态（批次恒跑完所有素材，不整批失败）
BATCH_STATUS_DONE: str = "done"
#: 单文件终态：全部阶段成功且分块全部入库
FILE_STATUS_SUCCESS: str = "success"
#: 单文件终态：任一阶段失败（未入库或部分入库），error 携带中文原因
FILE_STATUS_FAILED: str = "failed"

#: ingest 留痕阶段（对齐 cleaning_report.ALL_STAGES 的 CHECK 枚举；
#: 清洗三段在一次 run 内完成，仍按 dedup/correct/mask 三条分别留痕，供
#: FR-KB-024 报告聚合器直接消费）
STAGE_PARSE = "parse"
STAGE_DEDUP = "dedup"
STAGE_CORRECT = "correct"
STAGE_MASK = "mask"
STAGE_CHUNK = "chunk"
STAGE_EMBED = "embed"
STAGE_INDEX = "index"

#: 分块账目对账失守的错误码（SR-KB-012 §5 CHUNK-2001，编排层阻断入库）
CODE_CHUNK_LOSSY = "CHUNK-2001"


class IngestionError(RuntimeError):
    """入库编排统一异常（中文原因 + 可选错误码，供 FileResult.error 留痕）。"""

    def __init__(self, message: str, code: str = "INGEST-1000", stage: str = STAGE_PARSE) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.stage = stage


# ── 素材与批次模型 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceMaterial:
    """单个待入库素材：文件路径 + 目标知识库 + 可选条目覆盖项。

    metadata 为「覆盖注入」：与六库默认 metadata（default_metadata_for）浅合并，
    调用方只需覆盖关心的键（如真实 sku / scene_tags），其余走缺省合法值。
    """

    path: str
    kb_type: str
    title: str = ""          # 缺省取文件名主干
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """构造期校验：kb_type 必须命中六大知识库枚举。"""
        if self.kb_type not in KB_TYPES:
            raise ValueError(
                f"素材 {self.path} 的 kb_type={self.kb_type!r} 不在六大知识库枚举 {list(KB_TYPES)}"
            )


@dataclass
class FileResult:
    """单文件入库结果（BatchManifest 的行单元）。"""

    source: str                       # 素材文件路径（清单锚点）
    kb_type: str
    status: str = FILE_STATUS_FAILED  # success | failed
    error: str = ""                   # 失败原因（中文人话）
    error_code: str = ""              # 错误码（如 CHUNK-2001 / PARSE-1001）
    source_format: str = ""           # pdf/word/excel/image_ocr/audio_asr
    blocks: int = 0                   # 解析产出块数
    kept_blocks: int = 0              # 清洗保留块数（dropped 不进分块）
    chunks: int = 0                   # 分块产出块数
    indexed_chunks: int = 0           # 成功写入向量库块数
    chunk_ids: list[int] = field(default_factory=list)   # 入库块主键（评测集锚点）
    item_uid: str = ""                # 条目化产物 uid（KnowledgeItem.uid）
    quality: dict[str, Any] = field(default_factory=dict)  # 分块账目对账等自检指标

    @property
    def ok(self) -> bool:
        """成功口径：无失败且至少一个块完成向量入库（空文档不算入库成功）。"""
        return self.status == FILE_STATUS_SUCCESS and self.indexed_chunks > 0


@dataclass
class BatchManifest:
    """批次台账：素材清单 + 每文件状态 + 统计（覆盖率核算的输入）。"""

    batch_id: str
    sources: list[SourceMaterial]
    results: list[FileResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    status: str = BATCH_STATUS_DONE

    @property
    def total_files(self) -> int:
        """清单文件数（分母）。"""
        return len(self.sources)

    @property
    def success_files(self) -> int:
        """成功入库文件数（status=success 且有入库块）。"""
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_files(self) -> int:
        """失败文件数（含解析/清洗/分块/条目化/嵌入各阶段失败）。"""
        return sum(1 for r in self.results if r.status == FILE_STATUS_FAILED)

    @property
    def total_chunks(self) -> int:
        """全批次分块总数。"""
        return sum(r.chunks for r in self.results)

    @property
    def indexed_chunks(self) -> int:
        """全批次向量入库块总数。"""
        return sum(r.indexed_chunks for r in self.results)

    def result_of(self, source: str) -> FileResult | None:
        """按素材路径取结果行（覆盖率核算按清单锚点回查）。"""
        for result in self.results:
            if result.source == source:
                return result
        return None

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（报告/调试输出；结果行按清单序）。"""
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stats": {
                "total_files": self.total_files,
                "success_files": self.success_files,
                "failed_files": self.failed_files,
                "total_chunks": self.total_chunks,
                "indexed_chunks": self.indexed_chunks,
            },
            "results": [
                {
                    "source": r.source,
                    "kb_type": r.kb_type,
                    "status": r.status,
                    "error": r.error,
                    "error_code": r.error_code,
                    "source_format": r.source_format,
                    "blocks": r.blocks,
                    "kept_blocks": r.kept_blocks,
                    "chunks": r.chunks,
                    "indexed_chunks": r.indexed_chunks,
                    "chunk_ids": list(r.chunk_ids),
                    "item_uid": r.item_uid,
                }
                for r in self.results
            ],
        }


# ── 六库默认 metadata 路由（kb_type 路由注入）与向量维度路由 ──────────────────


def default_metadata_for(kb_type: str) -> dict[str, Any]:
    """按 kb_type 生成可通过六库内容模型校验的最小合法 metadata（缺省注入）。

    缺省值仅保证「结构合法」；生产入库由调用方经 SourceMaterial.metadata
    覆盖为真实业务字段（样例语料见 tools/precheck/sample_corpus/）。
    """
    if kb_type == "product":
        return {
            "category_path": ["样例品类", "演示产品"],
            "sku": "SAMPLE-0001",
            "moq": 100,
            "lead_time_days": 30,
        }
    if kb_type == "customer":
        # 分级标准类为全局文档：不关联具体 customer_ref（SR-KB-002 §6.1）
        return {"category": "grade_standard"}
    if kb_type == "skill":
        # negotiation 无需槽位声明（talk_track 才强制 slots）
        return {"skill_kind": "negotiation", "stage": "bargain", "scene_tags": ["议价"]}
    if kb_type == "process":
        return {"category": "sop", "process_nodes": ["P03"]}
    if kb_type == "negative_case":
        return {
            "case_category": "wrong_script",
            "reason_type": "wrong_script.over_promise",
            "occurred_at": "2025-06",
            "script_pairs": [
                {
                    "pair_uid": "SP-0001",
                    "wrong_script": "我们保证七天之内一定交货。",
                    "correct_script": "常规交期三十天，加急需求我确认生产排期后回复您。",
                    "scene_tag": "报价",
                    "reason_leaf": "wrong_script.over_promise",
                }
            ],
        }
    if kb_type == "training":
        return {
            "material_kind": "lesson",
            "course_code": "DEMO-COURSE-01",
            "course_title": "新人示范课程",
            "seq": 1,
        }
    raise ValueError(f"kb_type 必须为六大知识库枚举之一 {list(KB_TYPES)}，收到：{kb_type!r}")


def _meta_get(metadata: Any, key: str) -> Any:
    """从 metadata 取值：兼容 Mapping 与 pydantic 模型（KnowledgeItem.metadata）。"""
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return getattr(metadata, key, None)


def vector_dims_for(kb_type: str, metadata: Any) -> dict[str, str]:
    """条目 metadata → VectorRecord 五个高频过滤维度的路由注入。

    与 03-数据库设计 §4.2 标量列对齐：product→sku、skill/training→scene_tag、
    process→process_node、negative_case→reason_type、customer→customer_ref；
    长度合法性由 VectorRecord 校验器与 metadata 内容模型双重兜底。
    """
    if kb_type == "product":
        return {"sku": str(_meta_get(metadata, "sku") or "")}
    if kb_type == "skill":
        tags = list(_meta_get(metadata, "scene_tags") or ())
        return {"scene_tag": str(tags[0]) if tags else ""}
    if kb_type == "process":
        nodes = list(_meta_get(metadata, "process_nodes") or ())
        return {"process_node": str(nodes[0]) if nodes else ""}
    if kb_type == "negative_case":
        return {"reason_type": str(_meta_get(metadata, "reason_type") or "")}
    if kb_type == "customer":
        return {"customer_ref": str(_meta_get(metadata, "customer_ref") or "")}
    # training：scene_tag 可选（空串合法）
    return {"scene_tag": str(_meta_get(metadata, "scene_tag") or "")}


# ── 清洗产物 → 分块输入的桥（对齐 UnstructuredDocLike / BlockLike 鸭子契约）──


@dataclass
class _ChunkDocBlock:
    """分块输入块（鸭子对齐 parsers.base.Block：type/text/heading_level/table）。"""

    type: str                     # 'heading' | 'paragraph' | 'table'
    text: str = ""
    heading_level: int = 0
    table: list[list[str]] | None = None


@dataclass
class _ChunkDoc:
    """分块输入文档（鸭子对齐 UnstructuredDocLike：blocks/source_file）。"""

    blocks: list[_ChunkDocBlock]
    source_file: str
    source_format: str = ""


def _table_from_repr(text: str, original: list[list[str]] | None) -> list[list[str]] | None:
    """清洗后表格文本（table_repr 序列化）还原为二维表；形状不符回退原表。

    table_repr 格式恒为 ``行内 " | " 连接、行间 "\\n" 连接``；纠错后的单元格
    文本若自身含分隔符导致形状漂移（行列数与原表不一致），回退用原始表格
    （纠错只影响指纹口径，不阻塞入库；确定性守卫，不引入静默文本失真）。
    """
    if not text.strip():
        return [list(row) for row in original] if original is not None else None
    rows = [line.split(" | ") for line in text.split("\n")]
    if original is not None:
        if len(rows) != len(original) or any(
            len(row) != len(orig) for row, orig in zip(rows, original)
        ):
            return [list(row) for row in original]
    return rows


def _build_chunk_input(
    parsed: UnstructuredDoc, cleaned: CleanedDoc
) -> _ChunkDoc:
    """清洗产物 → 分块输入：仅保留 kept 块，文本取清洗后正文，表格做形状守卫还原。"""
    by_index = dict(enumerate(parsed.blocks))
    blocks: list[_ChunkDocBlock] = []
    for cb in cleaned.blocks:  # CleanedBlock.block_index 锚定解析块
        if cb.status != "kept":
            continue  # 去重 dropped 块不进分块（决策 D2）
        origin = by_index.get(cb.block_index)
        if origin is None:  # pragma: no cover - 防御分支：清洗块锚点必然存在于解析产物
            continue
        if origin.type == "table" and origin.table is not None:
            blocks.append(
                _ChunkDocBlock(
                    type="table",
                    table=_table_from_repr(cb.text, origin.table),
                )
            )
        else:
            blocks.append(
                _ChunkDocBlock(
                    type=origin.type,
                    text=cb.text,
                    heading_level=getattr(origin, "heading_level", 0) or 0,
                )
            )
    return _ChunkDoc(
        blocks=blocks,
        source_file=parsed.source_file,
        source_format=parsed.source_format,
    )


# ── 编排器本体 ───────────────────────────────────────────────────────────────


class IngestionOrchestrator:
    """端到端入库编排器：一个实例消费一批素材，全程鸭子类型注入。

    组件缺省值均为内存实现（FakeEmbeddingClient 需显式传入），单测与
    tools/precheck KPI 预跑直接全链跑通；生产替换为 TEI 客户端与 Milvus
    实现（协议不变，NFR-SCA-01）。
    """

    def __init__(
        self,
        *,
        embedder: EmbeddingClient,
        vector_store: VectorStore,
        parser_registry: ParserRegistry | None = None,
        cleaner: CleaningPipeline | None = None,
        chunker: SemanticChunker | None = None,
        log_store: IngestLogStore | None = None,
        chunk_id_allocator: Callable[[], int] | None = None,
        actor: str = "ingest-orchestrator",
    ) -> None:
        self._registry = parser_registry if parser_registry is not None else REGISTRY
        self._cleaner = cleaner if cleaner is not None else build_pipeline()
        self._chunker = chunker if chunker is not None else SemanticChunker()
        self._embedder = embedder
        self._vector_store = vector_store
        self._actor = actor
        # 留痕存储：缺省内存账本；生产对接 PG ingest_pipeline_log（同协议写入）
        self.log_store: Any = log_store if log_store is not None else InMemoryIngestLogStore()
        # chunk_id 分配器：缺省自增计数（对齐 PG IDENTITY 从 1 起）；测试可注入定点序列
        self._next_chunk_id: Iterator[int] = chunk_id_allocator if chunk_id_allocator is not None else itertools.count(1)

    # ---- 批次入口 -----------------------------------------------------------

    def run(self, sources: Sequence[SourceMaterial]) -> BatchManifest:
        """执行整批入库：逐文件编排，单文件失败不中断批次（部分成功策略）。"""
        manifest = BatchManifest(
            batch_id=f"b-{uuid.uuid4().hex[:12]}",
            sources=list(sources),
            started_at=_now_iso(),
        )
        for source in manifest.sources:
            manifest.results.append(self._ingest_one(source, manifest.batch_id))
        manifest.finished_at = _now_iso()
        return manifest

    # ---- 单文件链路 ---------------------------------------------------------

    def _ingest_one(self, source: SourceMaterial, batch_id: str) -> FileResult:
        """单文件全链：解析 → 清洗 → 分块（对账门禁）→ 条目化 → 嵌入 → 入库 → 留痕。"""
        result = FileResult(source=source.path, kb_type=source.kb_type)
        try:
            # ① 解析（FR-KB-010）
            rule = self._registry.rule_of(source.path)
            doc = rule.factory().parse(source.path)
            result.source_format = doc.source_format or rule.fmt
            result.blocks = len(doc.blocks)
            self._log(
                source, batch_id, STAGE_PARSE,
                {
                    "blocks_in": len(doc.blocks),
                    "parser": rule.parser_name,
                    "chars": int(doc.stats.get("chars", 0)),
                },
            )

            # ② 清洗（FR-KB-011）：三段一次执行，按段分别留痕
            cleaned = self._clean_blocks(doc, source, batch_id)
            result.kept_blocks = sum(1 for cb in cleaned.blocks if cb.status == "kept")

            # ③ 分块（FR-KB-012）+ 账目对账门禁（CHUNK-2001 阻断入库）
            chunks, quality = self._chunk_cleaned(doc, cleaned, source, batch_id)
            result.chunks = len(chunks)
            result.quality = quality

            # ④ 条目化（FR-KB-001~006 六库内容模型校验）
            item = self._materialize_item(source, cleaned)
            result.item_uid = str(item.uid)

            # ⑤⑥ 嵌入 + 向量入库（FR-KB-013 / FR-KB-014）
            chunk_ids = self._embed_and_index(chunks, source, item, batch_id)
            result.chunk_ids = chunk_ids
            result.indexed_chunks = len(chunk_ids)
            if not chunk_ids:
                raise IngestionError(
                    "清洗分块后无有效内容块可入库（素材为空或全部被判重）", "INGEST-1001", STAGE_CHUNK
                )
            result.status = FILE_STATUS_SUCCESS
        except IngestionError as exc:
            result.status = FILE_STATUS_FAILED
            result.error = exc.message
            result.error_code = exc.code
            self._try_log_failure(source, batch_id, exc.stage, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 —— 冻结接口的业务异常统一转批次失败行
            result.status = FILE_STATUS_FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            result.error_code = "INGEST-1000"
            self._try_log_failure(source, batch_id, STAGE_PARSE, "INGEST-1000", result.error)
        return result

    def _try_log_failure(
        self, source: SourceMaterial, batch_id: str, stage: str, code: str, message: str
    ) -> None:
        """失败留痕（尽力而为）：留痕写入本身异常不再上抛，保证批次不中断。"""
        try:
            self._log(
                source, batch_id, stage,
                {"status": STATUS_FAILED, "error_code": code, "error_msg": message},
            )
        except Exception:  # noqa: BLE001 —— 失败留痕失败不应扩大为批次异常
            pass

    def _clean_blocks(
        self, doc: UnstructuredDoc, source: SourceMaterial, batch_id: str
    ) -> CleanedDoc:
        """解析产物 → 三段清洗；stats 按 dedup/correct/mask 三条留痕（FR-KB-024 口径）。"""
        parsed_blocks = [
            ParsedBlock(
                block_index=index,
                type=block.type,
                text=block.text,
                table=block.table,
            )
            for index, block in enumerate(doc.blocks)
        ]
        cleaned = self._cleaner.run(parsed_blocks, source_file=source.path, kb_type=source.kb_type)
        stats = dict(cleaned.stats)
        self._log(source, batch_id, STAGE_DEDUP, {
            "blocks_in": int(stats.get("blocks_in", 0)),
            "dropped_exact": int(stats.get("dropped_exact", 0)),
            "dropped_near": int(stats.get("dropped_near", 0)),
        })
        self._log(source, batch_id, STAGE_CORRECT, {
            "corrections": int(stats.get("corrections", 0)),
            "correct_ruleset_version": str(stats.get("correct_ruleset_version", "")),
        })
        self._log(source, batch_id, STAGE_MASK, {
            "masked_blocks": int(stats.get("masked_findings", 0)),
            "needs_review": int(stats.get("needs_review", 0)),
            "mask_ruleset_version": str(stats.get("mask_ruleset_version", "")),
        })
        return cleaned

    def _chunk_cleaned(
        self,
        doc: UnstructuredDoc,
        cleaned: CleanedDoc,
        source: SourceMaterial,
        batch_id: str,
    ) -> tuple[list[Chunk], dict[str, Any]]:
        """清洗产物 → 语义分块 + 质量自检；账目失守抛 CHUNK-2001 阻断入库。"""
        chunk_doc = _build_chunk_input(doc, cleaned)
        chunks, metrics = self._chunker.chunk(chunk_doc)
        quality = {
            "lossless_blocks": bool(metrics.get("lossless_blocks")),
            "block_ledger_ok": bool(metrics.get("block_ledger_ok")),
            "text_ledger_ok": bool(metrics.get("text_ledger_ok")),
            "table_integrity": bool(metrics.get("table_integrity")),
        }
        self._log(source, batch_id, STAGE_CHUNK, {
            "blocks": int(metrics.get("blocks", len(chunk_doc.blocks))),
            "chunks": len(chunks),
            **quality,
        })
        if not quality["lossless_blocks"]:
            raise IngestionError(
                f"分块账目对账失守（block 不重不漏或文本拼接不一致），本文件阻断入库；"
                f"明细 blocks={quality['block_ledger_ok']} text={quality['text_ledger_ok']}",
                CODE_CHUNK_LOSSY,
                STAGE_CHUNK,
            )
        return chunks, quality

    def _materialize_item(self, source: SourceMaterial, cleaned: CleanedDoc) -> KnowledgeItem:
        """条目化：六库默认 metadata 路由注入 + 覆盖合并 → KnowledgeItemCreate 校验落 uid。

        KnowledgeItemCreate 构造即完成 discriminated union 校验（六库结构化规则
        全量生效）与 kb_type 交叉核对，非法 metadata 直接抛错（本文件转 failed）。
        """
        metadata: dict[str, Any] = {**default_metadata_for(source.kb_type)}
        metadata.update(dict(source.metadata))
        title = source.title.strip() or Path(source.path).stem or source.path
        content = "\n\n".join(
            cb.text for cb in cleaned.blocks if cb.status == "kept" and cb.text.strip()
        )
        create = KnowledgeItemCreate(
            kb_type=source.kb_type,  # type: ignore[arg-type] —— 枚举校验在模型层
            title=title[:200],
            content=content,
            metadata=metadata,  # 判别标签 kb_type 由创建模型自动注入
        )
        return KnowledgeItem.from_create(create, actor=self._actor)

    def _embed_and_index(
        self,
        chunks: list[Chunk],
        source: SourceMaterial,
        item: KnowledgeItem,
        batch_id: str,
    ) -> list[int]:
        """嵌入 + 向量入库：批量为全部块一次 embed_batch，逐块构造 VectorRecord 幂等 upsert。"""
        if not chunks:
            return []
        vectors = self._embedder.embed_batch([chunk.content for chunk in chunks])
        if len(vectors) != len(chunks):
            raise IngestionError(
                f"向量化结果条数（{len(vectors)}）与分块数（{len(chunks)}）不一致", "INGEST-1002", STAGE_EMBED
            )
        self._log(source, batch_id, STAGE_EMBED, {"embedded": len(chunks)})
        dims = vector_dims_for(source.kb_type, item.metadata)
        chunk_ids: list[int] = []
        records: list[VectorRecord] = []
        for chunk, (dense, sparse) in zip(chunks, vectors.rows()):
            chunk_id = next(self._next_chunk_id)
            chunk_ids.append(chunk_id)
            records.append(
                VectorRecord(
                    chunk_id=chunk_id,
                    kb_type=source.kb_type,
                    dense=dense,
                    sparse=sparse,
                    item_status=ITEM_STATUS_PUBLISHED,  # 首次全量入库直达发布
                    **dims,
                )
            )
        self._vector_store.upsert(records)
        self._log(
            source, batch_id, STAGE_INDEX,
            {"upserted": len(records), "chunk_ids": chunk_ids},
            output=f"item:{item.uid}",
        )
        return chunk_ids

    # ---- 留痕 ---------------------------------------------------------------

    def _log(
        self,
        source: SourceMaterial,
        batch_id: str,
        stage: str,
        detail: Mapping[str, Any],
        *,
        output: str = "",
    ) -> None:
        """写一条 ingest 留痕（IngestLogStore 协议；失败段显式带 status=failed）。"""
        record = IngestLogRecord(
            source_file=source.path,
            source_format=_suffix_of(source.path),
            stage=stage,
            action_detail={"status": STATUS_SUCCESS, "batch_id": batch_id, **dict(detail)},
            output=output,
        )
        append = getattr(self.log_store, "append", None)
        if callable(append):  # 鸭子兼容：append 协议（InMemoryIngestLogStore）
            append(record)
            return
        # 协议形态兜底：只读 iter_logs 视图（生产 PG 侧由写入口承接，此处不丢账）
        raise IngestionError(
            f"留痕存储缺少 append 协议（stage={stage}），无法写 ingest 留痕", "INGEST-1003", stage
        )


def _suffix_of(path: str) -> str:
    """取小写扩展名（含点；无扩展名返回空串，与 ParserRegistry._ext 同口径）。"""
    idx = path.rfind(".")
    if idx <= 0 or idx == len(path) - 1:
        return ""
    return path[idx:].lower()


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（毫秒精度，批次台账用）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "BATCH_STATUS_DONE",
    "FILE_STATUS_FAILED",
    "FILE_STATUS_SUCCESS",
    "STAGE_PARSE",
    "STAGE_DEDUP",
    "STAGE_CORRECT",
    "STAGE_MASK",
    "STAGE_CHUNK",
    "STAGE_EMBED",
    "STAGE_INDEX",
    "CODE_CHUNK_LOSSY",
    "IngestionError",
    "SourceMaterial",
    "FileResult",
    "BatchManifest",
    "default_metadata_for",
    "vector_dims_for",
    "IngestionOrchestrator",
]
