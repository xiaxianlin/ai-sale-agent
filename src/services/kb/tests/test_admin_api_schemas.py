# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""管理后台 Schema 下发与看板统计单元测试（FR-KB-020 / SR-KB-020 §5①，迭代 0.3）。

覆盖（本迭代验收清单）：
- schemas 端点返回结构：契约五键必有（name/label/required/control/options）、
  契约外键（rules/visible_when）与注册表同级键（库中文 label）透传、
  缺省补齐（label=name / required=False / control=input / options=None）；
- ?kb_type= 单库过滤：合法枚举未登记 → 空集；非法枚举 → 40001；
- 缺省空注册表（未接线 registry）→ schemas 空映射；
- stats 端点：条目数/状态分布（三键齐全）/类型分布/近 7 日变更（7 日窗口、
  空日补 0、日期升序）——存储直灌可控时间口径验证窗口边界；
- 写路径联动：经 API 创建的条目即时反映到统计。

桩：SchemaProvider 为自建桩（registry.py 并行任务不 import）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from services.kb.api.admin import create_admin_app
from services.kb.api.admin.items import InMemoryKnowledgeItemStore, KnowledgeItemRecord

SCHEMAS = "/api/v1/knowledge/schemas"
STATS = "/api/v1/admin/stats"
ITEMS = "/api/v1/knowledge/items"


class StubSchemaProvider:
    """注册表桩：两库登记，含契约外键（rules/visible_when/help）与同级库标签。"""

    def list_schemas(self) -> list[dict[str, Any]]:
        """product（完整字段 + 极简字段各一）与 process（含显隐条件字段）。"""
        return [
            {
                "kb_type": "product",
                "label": "产品知识库",
                "fields": [
                    {
                        "name": "sku",
                        "label": "SKU",
                        "required": True,
                        "control": "input",
                        "options": None,
                        "rules": {"pattern": "^[A-Za-z0-9\\-]{3,32}$", "pattern_msg": "3-32 位字母数字连字符"},
                        "help": "主 SKU，入库时统一大写",
                    },
                    {"name": "category_path"},  # 极简形态：仅 name，其余键缺省补齐
                ],
            },
            {
                "kb_type": "process",
                "label": "流程知识库",
                "fields": [
                    {
                        "name": "is_redline",
                        "label": "红线标记",
                        "required": False,
                        "control": "switch",
                        "options": None,
                        "visible_when": {"field": "is_redline", "equals": True},
                    }
                ],
            },
        ]


def make_client(**kwargs: Any) -> TestClient:
    """组装被测应用（缺省空注册表；用例按需注入桩/存储）。"""
    return TestClient(create_admin_app(**kwargs))


# ─────────────────────────── Schema 下发 ───────────────────────────


def test_schemas_endpoint_structure() -> None:
    """输出结构：库映射 + 契约五键 + 扩展键/同级键透传 + 缺省补齐。"""
    client = make_client(schema_provider=StubSchemaProvider())
    resp = client.get(SCHEMAS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and set(body) == {"code", "message", "data", "trace_id"}

    schemas = body["data"]["schemas"]
    assert set(schemas) == {"product", "process"}

    product = schemas["product"]
    assert product["label"] == "产品知识库"  # 注册表同级键透传
    sku, minimal = product["fields"]

    assert sku["name"] == "sku" and sku["label"] == "SKU"
    assert sku["required"] is True and sku["control"] == "input" and sku["options"] is None
    assert sku["rules"]["pattern"] == "^[A-Za-z0-9\\-]{3,32}$"  # 契约外键透传
    assert sku["help"] == "主 SKU，入库时统一大写"

    # 极简字段：五键缺省补齐（label=name / required=False / control=input / options=None）
    assert minimal == {
        "name": "category_path",
        "label": "category_path",
        "required": False,
        "control": "input",
        "options": None,
    }

    process_field = schemas["process"]["fields"][0]
    assert process_field["visible_when"] == {"field": "is_redline", "equals": True}


def test_schemas_kb_type_filter() -> None:
    """单库过滤：命中只回单库；合法枚举未登记回空集；非法枚举 40001。"""
    client = make_client(schema_provider=StubSchemaProvider())

    only_product = client.get(SCHEMAS, params={"kb_type": "product"}).json()["data"]["schemas"]
    assert set(only_product) == {"product"}

    unregistered = client.get(SCHEMAS, params={"kb_type": "customer"}).json()["data"]["schemas"]
    assert unregistered == {}

    resp = client.get(SCHEMAS, params={"kb_type": "oops"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40001
    assert body["data"]["errors"][0]["field"] == "kb_type"


def test_schemas_default_empty_provider() -> None:
    """未接线 registry（缺省空注册表）→ schemas 空映射（端点可用不报错）。"""
    client = make_client()
    body = client.get(SCHEMAS).json()
    assert body["code"] == 0 and body["data"]["schemas"] == {}


# ─────────────────────────── 看板统计 ───────────────────────────


def _seed_stats_store() -> InMemoryKnowledgeItemStore:
    """直灌存储（可控 updated_at）：2 发布 + 2 草稿 + 1 归档，1 条落在 7 日窗口外。"""
    store = InMemoryKnowledgeItemStore()
    now = datetime.now(timezone.utc)

    def seed(kb_type: str, title: str, status: str, updated_at: datetime) -> None:
        store.create(
            KnowledgeItemRecord(kb_type=kb_type, title=title, status=status, updated_at=updated_at)
        )

    seed("product", "P-1", "published", now - timedelta(days=3))
    seed("product", "P-2", "draft", now)
    seed("skill", "S-1", "draft", now - timedelta(days=2))
    seed("negative_case", "N-1", "archived", now - timedelta(days=5))
    seed("product", "P-old", "published", now - timedelta(days=8))  # 窗口外
    return store


def test_stats_aggregation_window_and_distributions() -> None:
    """总数/状态分布（三键齐全）/类型分布/近 7 日窗口（8 日前变更不计入）。"""
    client = make_client(store=_seed_stats_store())
    resp = client.get(STATS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and set(body) == {"code", "message", "data", "trace_id"}

    data = body["data"]
    assert data["total_items"] == 5  # 不含软删（本用例无软删行）
    assert data["status_distribution"] == {"draft": 2, "published": 2, "archived": 1}
    assert data["kb_type_distribution"] == {"product": 3, "skill": 1, "negative_case": 1}

    changes = data["changes_last_7_days"]
    assert changes["total"] == 4  # 8 日前的 P-old 不计入
    by_day = changes["by_day"]
    assert len(by_day) == 7  # 连续 7 日刻度
    dates = [point["date"] for point in by_day]
    assert dates == sorted(dates)  # 日期升序
    assert dates[-1] == datetime.now(timezone.utc).date().isoformat()  # 含当日
    assert sum(point["count"] for point in by_day) == 4
    assert all(point["count"] >= 0 for point in by_day)  # 空日补 0
    assert "generated_at" in data


def test_stats_empty_store() -> None:
    """空存储：总数 0、状态分布三键归零、7 日刻度全零。"""
    client = make_client(store=InMemoryKnowledgeItemStore())
    data = client.get(STATS).json()["data"]
    assert data["total_items"] == 0
    assert data["status_distribution"] == {"draft": 0, "published": 0, "archived": 0}
    assert data["kb_type_distribution"] == {}
    assert data["changes_last_7_days"]["total"] == 0
    assert [point["count"] for point in data["changes_last_7_days"]["by_day"]] == [0] * 7


def test_stats_reflects_write_path_and_excludes_soft_deleted() -> None:
    """经 API 写入即时反映统计；软删行从统计消失（与列表可见性同口径）。"""
    client = make_client()
    uids = []
    for title in ("条目一", "条目二"):
        resp = client.post(
            ITEMS,
            json={"kb_type": "product", "title": title, "metadata": {}, "action": "save_draft"},
        )
        assert resp.status_code == 200, resp.text
        uids.append(resp.json()["data"]["uid"])
    resp = client.put(
        f"{ITEMS}/{uids[0]}",
        json={"kb_type": "product", "title": "条目一", "metadata": {}, "action": "publish"},
    )
    assert resp.status_code == 200, resp.text

    data = client.get(STATS).json()["data"]
    assert data["total_items"] == 2
    assert data["status_distribution"] == {"draft": 1, "published": 1, "archived": 0}
    assert data["changes_last_7_days"]["total"] == 2

    # 已发布条目删除前须先归档（FR-KB-020 §5④），归档后软删从统计消失
    resp = client.put(
        f"{ITEMS}/{uids[0]}",
        json={"kb_type": "product", "title": "条目一", "metadata": {}, "action": "archive"},
    )
    assert resp.status_code == 200, resp.text
    assert client.delete(f"{ITEMS}/{uids[0]}").status_code == 200
    data = client.get(STATS).json()["data"]
    assert data["total_items"] == 1 and data["status_distribution"]["archived"] == 0
