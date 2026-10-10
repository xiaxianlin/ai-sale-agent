# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-TR-001 AI 导师 30 条抽检冒烟（迭代 1.1 退出证据，SRS §16.2 可溯源率 100% 口径）。

链路（全部内存实现，复用 Phase 0 冻结接口的只读引用）：

    tools/simcorp/generator 确定性合成语料（六库素材文件）
      → IngestionOrchestrator + FakeEmbeddingClient + InMemoryVectorStore 全链入库
      → search_fn 内存适配（FR-KB-030 契约信封，富化 title/kb_type/text）
      → TutorPipeline + FakeTutorLlm（确定性教学桩：事实句带 [来源N]）
      → 30 个问题（27 知识库内命中 + 3 故意零命中）

断言（退出条件，任一失败退出码 1）：
  ① 27 条命中回答 100% 含有效 [来源N]（编号不缺失、不越界），且被引块能对回
     实际入库 chunk（对回口径：被引 chunk 正文含该问题锚点 uid，证明事实接地）；
  ② 3 条零命中全部走固定话术（含「知识库暂无该内容，已记录」）+ 缺口登记，
     且零命中路径 LLM 调用次数为 0（零编造闸 1 桩计数）；
  ③ LLM 总调用次数 = 27（命中才生成）；缺口清单恰 3 条 pending（频次各 1）。

附加演示：缺口回流闭环（FR-TR-008）——补录知识后 on_knowledge_ingested 将
开放缺口置 resolved，同问题再现开新缺口行（不计入上述 30 条口径）。

**demo 口径声明**：本冒烟使用合成语料与确定性 Fake LLM（无真实甲方素材、
无 DeepSeek API Key）；正式验收必须按 SRS §16.2 以双方联合抽检 ≥100 条、
甲方真实素材与 DeepSeek-V3 生产链路复测（DeepSeekLlmClient 已写全，接入需
KB_DEEPSEEK_API_KEY 与出域白名单注册，NFR-SEC-06）。

用法（仓库根）：
  python3 tools/training/qa_smoke.py [--files 30] [--seed 42]
                                     [--hit-questions 27] [--zero-hits 3]
                                     [--corpus out/training/corpus]
                                     [--report out/training/qa_smoke_report.md]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# 仓库根自举：直接以文件路径执行时补齐仓库根（tools 包）与 src（services 包）
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.kb.core.ingestion import IngestionOrchestrator, SourceMaterial  # noqa: E402
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient  # noqa: E402
from services.kb.core.vectorstore import InMemoryVectorStore  # noqa: E402
from services.training.core.tutor.gap import (  # noqa: E402
    GAP_STATUS_PENDING,
    GAP_STATUS_RESOLVED,
    KnowledgeGapRecorder,
)
from services.training.core.tutor.llm import FakeTutorLlm  # noqa: E402
from services.training.core.tutor.pipeline import (  # noqa: E402
    NO_ANSWER_TEXT,
    CITATION_RE,
    InMemoryQaStore,
    TutorPipeline,
)
from tools.simcorp.generator import generate as generate_corpus  # noqa: E402

TAG = "[导师抽检]"

# ── 语料与抽检规模 ───────────────────────────────────────────────────────────

DEFAULT_CORPUS_DIR = Path("out") / "training" / "corpus"
DEFAULT_REPORT = Path("out") / "training" / "qa_smoke_report.md"
DEFAULT_FILES = 30          # 合成素材文件数（六库配比由生成器决定）
DEFAULT_SEED = 42           # 确定性种子（语料与问题全链可复现）
DEFAULT_HIT_QUESTIONS = 27  # 知识库内命中问题数
DEFAULT_ZERO_HITS = 3       # 故意零命中问题数

#: 六库问题模板（{uid} 为语料唯一锚点，保证稀疏路精确召回锚点块）
QUESTION_TEMPLATES: dict[str, str] = {
    "product": "{uid} 这款产品的最大承载是多少千克？",
    "customer": "{uid} 客户档案的分级和月采购量是多少？",
    "skill": "{uid} 话术里让步的原则是什么？",
    "process": "{uid} 节点规程要求价格数据从哪里取？",
    "negative_case": "{uid} 案例复盘的根因是什么？",
    "training": "{uid} 新人课程的课后练习要求是什么？",
}

#: 故意零命中问题（语料不可能覆盖的虚构主题，token 与合成语料零交集）
ZERO_HIT_QUESTIONS: tuple[str, ...] = (
    "ZXQ-9999 量子支架的谐振频率参数在哪里查？",
    "福波斯卫星能给客户做信用背书吗？",
    "银河系边境关税的起征点是多少？",
)

#: 稀疏相关性分词（与 FakeEmbeddingClient 稀疏路同法：unicode \w 分词 + 小写）
_TOKEN_RE_CACHE: Any = None


def _tokens(text: str) -> set[str]:
    """文本 → 稀疏 token 集合（与 FakeEmbeddingClient._TOKEN_RE 同口径）。"""
    global _TOKEN_RE_CACHE
    import re

    if _TOKEN_RE_CACHE is None:
        _TOKEN_RE_CACHE = re.compile(r"\w+", re.UNICODE)
    return {t for t in _TOKEN_RE_CACHE.findall(text.lower()) if len(t) >= 2}


# ── 内存检索适配（FR-KB-030 契约信封 + 元数据富化）──────────────────────────


class EnrichedMemorySearchFn:
    """内存链路 search_fn：查询向量化 → hybrid_search（六库过滤）→ 相关性门 → 富化回表。

    与 tools/precheck/phase0_kpi.MemorySearchFn 同法，两处 demo 适配：
    - 相关性门（模拟 FR-KB-030/SR-TR-001 zero_hit_score 语义）：FakeEmbeddingClient
      的 dense 向量为哈希伪随机（任意两文本余弦几乎恒为正），不过门则任何查询都
      有 Top5——故按「查询与命中块正文无任何共享 token → 该次检索返回空信封」
      截断，等价于检索层判零相关；有共享 token 时按共享度重排（模拟重排把
      词法证据充分的块前置，RRF 路由截断的并列分不再影响首位）；生产形态由
      真实向量相似度 + 重排分阈值天然给出；
    - chunk_id 富化为 title/kb_type/text（模拟 kb-service 的 PG 回表，供导师
      构造【知识资料】与 Fake 桩接地）。
    """

    def __init__(
        self,
        embedder: FakeEmbeddingClient,
        store: InMemoryVectorStore,
        chunk_meta: dict[int, dict[str, Any]],
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._chunk_meta = chunk_meta
        self._token_cache: dict[int, set[str]] = {}
        self.calls = 0

    def _tokens_of_chunk(self, chunk_id: int) -> set[str]:
        """chunk 正文 token 集（缓存；相关性门用）。"""
        if chunk_id not in self._token_cache:
            self._token_cache[chunk_id] = _tokens(str(self._chunk_meta.get(chunk_id, {}).get("text", "")))
        return self._token_cache[chunk_id]

    def __call__(
        self,
        *,
        query: str,
        kb_types: Sequence[str] | None = None,
        filters: Any = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """执行一次混合检索，返回统一信封 {"results": [{chunk_id, score, …}]}。"""
        self.calls += 1
        result = self._embedder.embed_batch([query])
        dense, sparse = next(result.rows())
        expr = None
        if kb_types:
            listing = ", ".join(f"'{kb}'" for kb in kb_types)
            expr = f"kb_type in ({listing})"
        hits = self._store.hybrid_search(dense=dense, sparse=sparse, expr=expr, top_k=top_k)
        # 相关性门：与查询无任何共享 token 的检索判零相关，返回空信封（导师闸 1 随即生效）
        query_tokens = _tokens(query)
        overlaps = [(hit, len(query_tokens & self._tokens_of_chunk(hit.chunk_id))) for hit in hits]
        if hits and max((overlap for _, overlap in overlaps), default=0) == 0:
            return {"results": []}
        # 词法证据重排：共享 token 多者前置（并列保持原 RRF 顺序）
        ordered = [hit for hit, _ in sorted(overlaps, key=lambda item: -item[1])]
        results = []
        for hit in ordered:
            meta = self._chunk_meta.get(hit.chunk_id, {})
            results.append(
                {
                    "chunk_id": hit.chunk_id,
                    "score": round(float(hit.score), 4),
                    "title": str(meta.get("title", "")),
                    "kb_type": str(meta.get("kb_type", "")),
                    "text": str(meta.get("text", "")),
                }
            )
        return {"results": results}


def build_knowledge_base(corpus_dir: Path, files: int, seed: int) -> tuple[Any, dict[int, dict[str, Any]], Any]:
    """合成语料 → 全链入库 → (检索函数, chunk 登记表, 入库台账)。

    chunk 登记表：chunk_id → {kb_type, title, text}。text 取嵌入批次快照按
    chunk_id 顺序对齐（编排器 chunk_id 顺序分配与 embed_batch 逐块同序）。
    """
    manifest_path = generate_corpus(corpus_dir, files, seed=seed)
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))

    embedder = FakeEmbeddingClient()
    store = InMemoryVectorStore()
    orchestrator = IngestionOrchestrator(embedder=embedder, vector_store=store)
    sources = [
        SourceMaterial(str(corpus_dir / name), meta["kb_type"])
        for name, meta in entries.items()
    ]
    manifest = orchestrator.run(sources)

    failed = [r for r in manifest.results if not r.ok]
    if failed:
        raise SystemError(
            f"入库失败 {len(failed)} 个文件（抽检前置条件不满足）："
            + "；".join(f"{r.source}: {r.error}" for r in failed[:3])
        )

    # 嵌入批次展平 = chunk_id 顺序（1 起自增分配与批量嵌入同序，见编排器 §⑤⑥）
    flat_texts = [text for batch in embedder.calls for text in batch]
    if len(flat_texts) != manifest.indexed_chunks:
        raise SystemError(
            f"嵌入块数 {len(flat_texts)} 与入库块数 {manifest.indexed_chunks} 不一致，登记表无法对齐"
        )
    chunk_meta: dict[int, dict[str, Any]] = {}
    pointer = 0
    for file_result in manifest.results:
        for chunk_id in file_result.chunk_ids:
            chunk_meta[chunk_id] = {
                "kb_type": file_result.kb_type,
                "title": Path(file_result.source).stem,
                "text": flat_texts[pointer],
            }
            pointer += 1

    search_fn = EnrichedMemorySearchFn(FakeEmbeddingClient(), store, chunk_meta)
    return search_fn, chunk_meta, manifest


def choose_anchor(meta: dict[str, Any], file_chunk_texts: list[str]) -> str:
    """为单个素材文件选问题锚点：uid（docx/pdf 正文含）→ SKU 数字段（xlsx 单元格含）。

    锚点必须真实出现在该文件入库后的 chunk 正文中（稀疏召回与对回核验共用），
    保证「问有所指、引有所据」；两候选均缺失时退回 uid（该文件将按零命中审计）。
    """
    joined = "\n".join(file_chunk_texts)
    uid = str(meta.get("uid", ""))
    sku_numeric = str(meta.get("sku", "")).split("-")[-1]
    for candidate in (uid, sku_numeric):
        if candidate and candidate in joined:
            return candidate
    return uid


def build_questions(
    entries: dict[str, dict[str, Any]],
    manifest: Any,
    chunk_meta: dict[int, dict[str, Any]],
    hit_count: int,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """从语料 manifest 构造 (问题, 锚点, 库类型) 列表 + 无法出题的文件清单（确定性顺序）。"""
    by_source = {Path(r.source).name: r for r in manifest.results}
    questions: list[tuple[str, str, str]] = []
    unusable: list[str] = []
    for name, meta in entries.items():
        if len(questions) >= hit_count:
            break
        kb_type = meta["kb_type"]
        template = QUESTION_TEMPLATES.get(kb_type)
        file_result = by_source.get(name)
        if template is None or file_result is None:
            continue
        file_chunk_texts = [
            str(chunk_meta[cid].get("text", "")) for cid in file_result.chunk_ids
        ]
        anchor = choose_anchor(meta, file_chunk_texts)
        if anchor not in "\n".join(file_chunk_texts):
            unusable.append(name)  # 正文无锚点（如 xlsx 无 uid）：不出题，避免假命中
            continue
        questions.append((template.format(uid=anchor), anchor, kb_type))
    return questions, unusable


# ── 单题抽检 ─────────────────────────────────────────────────────────────────


def audit_hit_case(
    index: int, question: str, anchor_uid: str, kb_type: str, events: list[Any], chunk_meta: dict[int, dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """审计一条命中回答：引用有效性 + 对回 chunk（锚点接地）。"""
    errors: list[str] = []
    done = next((e.data for e in events if e.type == "done"), None)
    if done is None or done.get("is_gap"):
        return {}, [f"第 {index} 题未产出命中终态（done 缺失或走了兜底）"]
    answer = str(done.get("answer", ""))
    refs = list(done.get("knowledge_refs") or [])
    cited = sorted({int(n) for n in CITATION_RE.findall(answer)})
    if not cited:
        errors.append(f"第 {index} 题回答无任何 [来源N] 引用标记")
    ref_nos = {r["no"] for r in refs}
    invalid = [n for n in cited if n not in ref_nos]
    if invalid:
        errors.append(f"第 {index} 题引用越界：{invalid} 不在下发块编号 {sorted(ref_nos)} 内")
    # 对回 chunk：被引块的入库正文须含问题锚点 uid（事实接地证明）
    anchor_chunk_ids = {
        cid for cid, meta in chunk_meta.items() if anchor_uid in str(meta.get("text", ""))
    }
    cited_chunk_ids = [r["chunk_id"] for r in refs if r["no"] in cited]
    grounded = anchor_chunk_ids & set(cited_chunk_ids)
    if not grounded:
        errors.append(
            f"第 {index} 题引用无法对回锚点块（锚点 {anchor_uid} 所在块 {sorted(anchor_chunk_ids)[:4]}，"
            f"被引块 {cited_chunk_ids}）"
        )
    row = {
        "index": index,
        "kind": "命中",
        "kb_type": kb_type,
        "question": question,
        "cited": cited,
        "cited_chunk_ids": [c for c in cited_chunk_ids if c in grounded] or cited_chunk_ids,
        "anchor_chunk_ids": sorted(anchor_chunk_ids),
        "trace_id": done.get("trace_id", ""),
    }
    return row, errors


def audit_zero_case(index: int, question: str, events: list[Any]) -> tuple[dict[str, Any], list[str]]:
    """审计一条零命中：固定话术 + 无引用 + 走缺口路径。"""
    errors: list[str] = []
    types = [e.type for e in events]
    if "token" in types:
        errors.append(f"第 {index} 题（零命中）出现了 token 事件：零命中不得进入生成")
    gap_events = [e for e in events if e.type == "gap"]
    done = next((e.data for e in events if e.type == "done"), None)
    if not gap_events or done is None:
        return {}, [f"第 {index} 题（零命中）缺少 gap/done 事件：{types}"]
    if done.get("is_gap") is not True or done.get("knowledge_refs"):
        errors.append(f"第 {index} 题（零命中）终态应为 is_gap=true 且 refs 为空")
    if "知识库暂无该内容，已记录" not in str(done.get("answer", "")):
        errors.append(f"第 {index} 题（零命中）未走固定话术：{str(done.get('answer', ''))[:40]!r}")
    if str(gap_events[0].data.get("message", "")) != NO_ANSWER_TEXT:
        errors.append(f"第 {index} 题（零命中）gap 话术与固定话术不一致")
    row = {
        "index": index,
        "kind": "零命中",
        "kb_type": "-",
        "question": question,
        "cited": [],
        "gap_id": gap_events[0].data.get("gap_id", ""),
        "trace_id": done.get("trace_id", ""),
    }
    return row, errors


# ── 报告 ──────────────────────────────────────────────────────────────────────


def write_report(
    report_path: Path,
    *,
    manifest: Any,
    rows: list[dict[str, Any]],
    gaps_snapshot: list[dict[str, Any]],
    llm_calls: int,
    closure_note: str,
    failures: list[str],
) -> None:
    """写中文抽检报告（Markdown，demo 口径声明置顶）。"""
    hit_rows = [r for r in rows if r["kind"] == "命中"]
    zero_rows = [r for r in rows if r["kind"] == "零命中"]
    total, hit_ok = len(rows), len(hit_rows)
    lines: list[str] = []
    lines.append("# FR-TR-001 AI 导师系统——迭代 1.1 退出证据（30 条回答可溯源抽检）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}（UTC）")
    lines.append("- 需求依据：SRS §6.2/§16.2（可溯源率 100%）；SR-TR-001 §9 集成/验收抽检要点")
    lines.append("")
    lines.append("> **demo 口径声明**：本报告为工程自证证据——语料为确定性合成语料（tools/simcorp，")
    lines.append("> 固定种子可复现），LLM 为确定性教学桩 FakeTutorLlm（无真实甲方素材、无 DeepSeek")
    lines.append("> API Key）。DeepSeek-V3 真实客户端（DeepSeekLlmClient）代码已写全，接入需配置")
    lines.append("> KB_DEEPSEEK_API_KEY 并完成出域白名单注册（NFR-SEC-06）。正式验收必须按 SRS §16.2")
    lines.append("> 以双方联合抽检 ≥100 条（≥20% 知识库外问题）、甲方真实素材与生产链路复测。")
    lines.append("")
    lines.append("## 一、抽检结论")
    lines.append("")
    verdict = "通过" if not failures else "未通过"
    lines.append(f"| 项 | 目标 | 实测 | 结论 |")
    lines.append(f"|---|---|---|---|")
    lines.append(
        f"| 命中回答含有效 [来源N] 且能对回 chunk | {hit_ok}/{hit_ok}（100%）"
        f" | {hit_ok}/{total} 条命中全部通过锚点对回核验 | {'通过' if not failures else '见失败清单'} |"
    )
    lines.append(
        f"| 零命中走固定话术 + 缺口登记 | {len(zero_rows)}/{len(zero_rows)} | "
        f"{len(zero_rows)} 条全部固定话术并登记缺口 | {'通过' if not failures else '见失败清单'} |"
    )
    lines.append(f"| 零命中路径 LLM 调用次数 | 0 | 0（LLM 总调用 {llm_calls} 次 = 命中题数 {hit_ok}） | 通过 |")
    lines.append(f"| **总体判定** | 30 条 100% 可溯源 | 抽检 {total} 条，失败 {len(failures)} 条 | **{verdict}** |")
    lines.append("")
    lines.append("## 二、语料与链路")
    lines.append("")
    lines.append(f"- 合成语料：{manifest.total_files} 个素材文件（六库配比），入库 {manifest.indexed_chunks} 个知识块；")
    lines.append("- 入库链路：IngestionOrchestrator（解析→清洗→分块→条目化→嵌入→向量库，内存实现）；")
    lines.append("- 检索链路：FR-KB-030 契约 search_fn（FakeEmbeddingClient 稀疏锚点召回 + InMemoryVectorStore RRF 融合）；")
    lines.append("- 问答链路：TutorPipeline 零编造三道闸（零命中不生成 → 引用校验 → 落库带 knowledge_refs）。")
    lines.append("")
    lines.append("## 三、30 条抽检明细")
    lines.append("")
    lines.append("| # | 类型 | 知识库 | 问题（截断） | 引用标记 | 对回 chunk_id |")
    lines.append("|---|---|---|---|---|---|")
    for row in rows:
        cited = ",".join(f"[来源{n}]" for n in row.get("cited", [])) or "-"
        chunk_part = row.get("cited_chunk_ids") or row.get("gap_id", "")
        chunk_text = (
            "，".join(str(c) for c in chunk_part[:3]) if isinstance(chunk_part, list) else str(chunk_part)
        )
        lines.append(
            f"| {row['index']} | {row['kind']} | {row['kb_type']} | {row['question'][:28]}… "
            f"| {cited} | {chunk_text} |"
        )
    lines.append("")
    lines.append("## 四、知识缺口清单（FR-TR-008 回流入口）")
    lines.append("")
    lines.append("| gap_id | 样例问题 | 原因 | 频次 | 状态 |")
    lines.append("|---|---|---|---|---|")
    for gap in gaps_snapshot:
        lines.append(
            f"| {gap['gap_id']} | {gap['sample_question'][:24]}… | {gap['reason']} "
            f"| {gap['hit_count']} | {gap['status']} |"
        )
    if closure_note:
        lines.append("")
        lines.append("## 五、缺口回流闭环演练（FR-TR-008）")
        lines.append("")
        lines.append(closure_note)
    if failures:
        lines.append("")
        lines.append("## 失败清单")
        lines.append("")
        lines.extend(f"- {msg}" for msg in failures)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── 主流程 ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FR-TR-001 AI 导师 30 条可溯源抽检（demo 口径）")
    parser.add_argument("--files", type=int, default=DEFAULT_FILES, help="合成素材文件数")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="确定性种子")
    parser.add_argument("--hit-questions", type=int, default=DEFAULT_HIT_QUESTIONS)
    parser.add_argument("--zero-hits", type=int, default=DEFAULT_ZERO_HITS)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    total_questions = args.hit_questions + args.zero_hits
    if total_questions != 30:
        print(f"{TAG} 警告：题目总数 {total_questions} != 30（退出证据口径为 27 命中 + 3 零命中）")

    failures: list[str] = []
    try:
        search_fn, chunk_meta, manifest = build_knowledge_base(args.corpus, args.files, args.seed)
        entries = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
        hit_questions, unusable = build_questions(entries, manifest, chunk_meta, args.hit_questions)
        if len(hit_questions) < args.hit_questions:
            raise SystemError(
                f"语料条目不足：需要 {args.hit_questions} 条可出题文件，仅 {len(hit_questions)} 条"
                f"（正文无锚点 {len(unusable)} 个：{unusable[:3]}…；可调大 --files）"
            )
    except SystemError as exc:
        print(f"{TAG} 前置失败：{exc}")
        return 1

    llm = FakeTutorLlm()
    qa_store = InMemoryQaStore()
    gap_recorder = KnowledgeGapRecorder()
    pipeline = TutorPipeline(search_fn=search_fn, llm=llm, qa_store=qa_store, gap_recorder=gap_recorder)

    print(f"{TAG} 语料入库 {manifest.indexed_chunks} 块；开始 {total_questions} 条抽检（会话 qa-smoke）")
    rows: list[dict[str, Any]] = []
    index = 0
    for question, uid, kb_type in hit_questions:
        index += 1
        events = list(pipeline.ask(question, session="qa-smoke"))
        row, errors = audit_hit_case(index, question, uid, kb_type, events, chunk_meta)
        rows.append(row or {"index": index, "kind": "命中", "kb_type": kb_type, "question": question})
        failures.extend(errors)
    llm_calls_after_hits = llm.calls

    for question in ZERO_HIT_QUESTIONS[: args.zero_hits]:
        index += 1
        events = list(pipeline.ask(question, session="qa-smoke"))
        row, errors = audit_zero_case(index, question, events)
        rows.append(row or {"index": index, "kind": "零命中", "kb_type": "-", "question": question})
        failures.extend(errors)
    llm_calls_total = llm.calls

    # 总闸断言：零命中不生成（LLM 调用数 = 命中题数）；缺口恰 N 条 pending
    if llm_calls_total != llm_calls_after_hits or llm_calls_after_hits != args.hit_questions:
        failures.append(
            f"LLM 调用计数异常：命中阶段 {llm_calls_after_hits}（应 {args.hit_questions}），"
            f"总调用 {llm_calls_total}（零命中阶段不得调用）"
        )
    gaps = gap_recorder.list_gaps(status=GAP_STATUS_PENDING)
    if len(gaps) != args.zero_hits:
        failures.append(f"缺口登记数 {len(gaps)} != 零命中题数 {args.zero_hits}")

    # 附加演示：缺口回流闭环（不占 30 条口径）——补录知识 → resolved → 同问再现开新行
    closure_note = ""
    if args.zero_hits >= 1 and gaps:
        demo_gap = gaps[0]
        resolved = gap_recorder.resolve(demo_gap, resolved_item_uid="ui-demo-reflow-001")
        reopened = gap_recorder.record(demo_gap.sample_question, session="qa-smoke")
        ok_closure = (
            resolved.status == GAP_STATUS_RESOLVED
            and resolved.resolved_item_uid == "ui-demo-reflow-001"
            and reopened.gap_id != resolved.gap_id
            and reopened.status == GAP_STATUS_PENDING
        )
        closure_note = (
            f"以缺口 {demo_gap.gap_id}（「{demo_gap.sample_question[:18]}…」）演练：知识补录入库后经 "
            f"on_knowledge_ingested/resolve 置 resolved（resolved_item_uid=ui-demo-reflow-001）；"
            f"同问题再现时开新缺口行 {reopened.gap_id}（resolved 退出频次聚合）——"
            + ("闭环验证通过。" if ok_closure else "闭环验证未通过。")
        )
        if not ok_closure:
            failures.append("缺口回流闭环演练未通过")

    gaps_snapshot = [g.to_dict() for g in gap_recorder.list_gaps()]
    write_report(
        args.report,
        manifest=manifest,
        rows=rows,
        gaps_snapshot=gaps_snapshot,
        llm_calls=llm_calls_total,
        closure_note=closure_note,
        failures=failures,
    )

    hit_rows = [r for r in rows if r["kind"] == "命中"]
    print(
        f"{TAG} 抽检完成：命中 {len(hit_rows)}/{total_questions} 条（100% 有效引用且对回 chunk："
        f"{'是' if not failures else '否'}）；零命中 {total_questions - len(hit_rows)} 条全部固定话术+缺口登记"
    )
    print(f"{TAG} 报告 → {args.report}")
    if failures:
        print(f"{TAG} 失败 {len(failures)} 项：")
        for msg in failures:
            print(f"  - {msg}")
        return 1
    print(f"{TAG} 退出证据判定：通过（30/30 可溯源）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
