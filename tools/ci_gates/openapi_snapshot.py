# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""OpenAPI 快照比对门禁（CR R3 修复，FR-KB-030 契约冻结的第三道防线）。

比对口径（冻结面比对）：
- docs/openapi/openapi.yaml 为 v1 冻结基线（检索契约）；
- live openapi 中，基线所含的每条 path 及其引用的 schemas 必须**逐字段一致**
  （防实现漂移：字段增删/类型变化/必填变化即失败）；
- live 中的**增量**（后续迭代新增的 admin 等路由）不判违规——增量走接口设计
  §5 版本化（向下兼容新增，破坏性变更升 v2）。

用法：python3 tools/ci_gates/openapi_snapshot.py（退出码 0/1）
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _live_spec() -> dict:
    """构造最小装配的 kb 应用并导出 live OpenAPI（不依赖外部服务）。"""
    from foundation.apikey.keys import InMemoryKeyStore, KeyRegistry
    from services.kb.app import create_app
    from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
    from services.kb.core.retrieval.reranker import PassthroughReranker
    from services.kb.core.vectorstore.inmemory_impl import InMemoryVectorStore

    registry = KeyRegistry(store=InMemoryKeyStore())
    app = create_app(
        store=InMemoryVectorStore(),
        embedder=FakeEmbeddingClient(),
        reranker=PassthroughReranker(),
        key_registry=registry,
    )
    return app.openapi()


def _collect_refs(node: object, seen: set[str]) -> None:
    """递归收集 $ref 引用的 schema 名。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                seen.add(value.rsplit("/", 1)[-1])
            else:
                _collect_refs(value, seen)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, seen)


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("[跳过] 本机无 yaml 解析依赖（CI 环境经 dev extras 安装后生效）")
        return 0

    baseline = yaml.safe_load((_REPO_ROOT / "docs/openapi/openapi.yaml").read_text(encoding="utf-8"))
    live = _live_spec()

    problems: list[str] = []
    base_paths = baseline.get("paths", {})
    for path, item in base_paths.items():
        if path not in live.get("paths", {}):
            problems.append(f"冻结路径在 live 中缺失：{path}")
            continue
        a = json.dumps(item, ensure_ascii=False, sort_keys=True, indent=1).splitlines()
        b = json.dumps(live["paths"][path], ensure_ascii=False, sort_keys=True, indent=1).splitlines()
        if a != b:
            diff = "\n".join(list(difflib.unified_diff(a, b, "baseline", "live", lineterm=""))[:20])
            problems.append(f"冻结路径漂移：{path}\n{diff}")

    # 引用的 schemas 同口径比对（请求/响应模型）
    refs: set[str] = set()
    _collect_refs(base_paths, refs)
    for name in sorted(refs):
        base_schema = baseline.get("components", {}).get("schemas", {}).get(name)
        live_schema = live.get("components", {}).get("schemas", {}).get(name)
        if base_schema is None:
            continue  # 基线未定义的外部引用不在冻结面
        if live_schema is None:
            problems.append(f"冻结模型在 live 中缺失：{name}")
            continue
        a = json.dumps(base_schema, ensure_ascii=False, sort_keys=True, indent=1).splitlines()
        b = json.dumps(live_schema, ensure_ascii=False, sort_keys=True, indent=1).splitlines()
        if a != b:
            diff = "\n".join(list(difflib.unified_diff(a, b, "baseline", "live", lineterm=""))[:16])
            problems.append(f"冻结模型漂移：{name}\n{diff}")

    if problems:
        print("[不通过] OpenAPI 快照比对发现冻结契约漂移：")
        for p in problems:
            print(" -", p)
        print("处置：向下兼容的字段新增走 live 同步基线并升 yaml 次版本；"
              "破坏性变更必须走接口设计 §5 升 v2。")
        return 1
    print(f"[通过] OpenAPI 快照比对：{len(base_paths)} 条冻结路径与 {len(refs)} 个引用模型与 live 逐字段一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
