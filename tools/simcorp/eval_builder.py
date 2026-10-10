# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""评测集构造器（合成语料配套，迭代 0.3 收口）。

从 generator 的 manifest（素材锚点）与灌装结果（FileResult.chunk_ids）
推导 ≥100 条评测用例：
- 文件级用例：query 为锚点主题句的口语化变体，期望命中 = 该文件入库块主键集合
  （file 级口径：Top5 命中任一块即算命中）；
- 家族级用例：按 SKU 家族（ST/GP/LM/MC）跨文件检索，期望 = 家族内文件块集合。

声明：合成评测集用于工程自证；正式验收需业务专家人工出题 ≥100 条（FR-KB-016）。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from services.kb.core.evaluation.dataset import EvalCase, EvalDataset

#: 每库 query 模板（{uid}/{sku} 占位；锚点词原样保留保证稀疏命中）
QUERY_TEMPLATES: dict[str, list[str]] = {
    "product": [
        "{uid} 这款支架最大承载多少千克 认证情况",
        "{uid} {sku} 产品参数 承载与认证",
    ],
    "customer": [
        "{uid} 这个客户是什么分级 月采购量",
        "{uid} 客户档案 沟通历史 月采购",
    ],
    "skill": [
        "{uid} 话术要点 让步要换什么条件",
        "{uid} 场景话术 模板",
    ],
    "process": [
        "{uid} 这个节点操作规程 价格取数",
        "{uid} 流程要求 报价规范",
    ],
    "negative_case": [
        "{uid} 这次丢单的根因和改进",
        "{uid} 复盘案例 教训",
    ],
    "training": [
        "{uid} 课程目标与课后练习",
        "{uid} 带教材料 测验",
    ],
}


def build_eval_set(
    manifest_path: Path,
    file_chunk_ids: dict[str, list[int]],
    min_cases: int = 100,
    seed: int = 42,
) -> EvalDataset:
    """构造评测集：文件级用例为主，不足 min_cases 时叠加第二模板轮询。

    file_chunk_ids：文件名 → 入库块主键列表（BatchManifest.results 映射）。
    """
    rnd = random.Random(seed)
    manifest: dict[str, dict] = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases: list[EvalCase] = []
    # 第一轮：每文件一个用例（模板 0）；文件过多时随机采样保持规模可控
    names = [n for n, info in manifest.items() if file_chunk_ids.get(n)]
    if len(names) > 400:
        names = rnd.sample(names, 400)
    for i, name in enumerate(names):
        info = manifest[name]
        tpl = QUERY_TEMPLATES[info["kb_type"]][i % 1]
        q = tpl.format(uid=info["uid"], sku=info["sku"])
        cases.append(
            EvalCase(
                case_id=f"sim-{len(cases) + 1:04d}",
                query=q,
                expected_chunk_ids=tuple(file_chunk_ids[name]),
                kb_type=info["kb_type"],
                dimensions={"语料": "sim_corpus"},
            )
        )
    # 第二轮：未达 min_cases 时按模板 1 轮询补足
    j = 0
    while len(cases) < min_cases and names:
        name = names[j % len(names)]
        info = manifest[name]
        tpl = QUERY_TEMPLATES[info["kb_type"]][1]
        cases.append(
            EvalCase(
                case_id=f"sim-{len(cases) + 1:04d}",
                query=tpl.format(uid=info["uid"], sku=info["sku"]),
                expected_chunk_ids=tuple(file_chunk_ids[name]),
                kb_type=info["kb_type"],
                dimensions={"语料": "sim_corpus"},
            )
        )
        j += 1
    ds = EvalDataset(name="simcorp", version=1, cases=cases)
    ds.validate_capacity(min_cases=1)
    return ds
