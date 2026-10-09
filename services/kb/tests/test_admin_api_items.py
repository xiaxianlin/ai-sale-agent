# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""管理后台条目 API 单元测试（FR-KB-020 / SR-KB-020 §9，迭代 0.3）。

覆盖（本迭代验收清单）：
- CRUD 全路径（TestClient + InMemoryKnowledgeItemStore 真链路，无 mock 截断）；
- 草稿-发布状态机：draft→publish→save_draft→archive→restore 全链 + 非法迁移 40001
  （SR §9 test_transition_illegal）；
- 乐观锁：expected_version 落后 → 40901 且数据未被覆盖（SR §9 test_optimistic_lock_conflict）；
- 发布触发 sync_hook：enqueue(upsert, 幂等键=uid:v版本)；save_draft 不入队；
  archive/delete → remove；索引钩子异常不阻断保存（SR §6.4 降级口径）；
- audit_hook 收到事件：action_type=kb_change、before/after 摘要、actor/trace_id 透传；
- 软删：已发布删除防线（SR §9 test_delete_published_guard 的 API 侧）、软删后
  列表/详情不可见；
- 分页与过滤边界：page_size≤100、越界 40001、kb_type/status/关键词过滤；
- 错误信封 {code,message,data,trace_id} 四键齐全（04-接口设计 §1.3）。

桩：SchemaProvider / SyncHook / AuditHook 均为自建桩（registry.py 并行任务不 import）。
"""
from __future__ import annotations

from typing import Any, Mapping

from fastapi.testclient import TestClient

from services.kb.api.admin import create_admin_app
from services.kb.api.admin.items import InMemoryKnowledgeItemStore, KnowledgeItemRecord

#: 被测端点前缀（04-接口设计 §3.1）
ITEMS = "/api/v1/knowledge/items"

# ─────────────────────────── 测试桩 ───────────────────────────


class StubSyncHook:
    """同步钩子桩：记录 enqueue 调用；可注入异常以测索引失败不阻断保存。"""

    def __init__(self, raise_on: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise_on = raise_on

    def enqueue(self, *, item_uid: str, change: str, idem_key: str, version: int) -> None:
        """记录一次入队；命中注入的异常方向则抛错（模拟 FR-KB-017 不可达）。"""
        self.calls.append(
            {"item_uid": item_uid, "change": change, "idem_key": idem_key, "version": version}
        )
        if self._raise_on == change:
            raise RuntimeError("FR-KB-017 索引入队失败（测试桩）")


class StubAuditHook:
    """审计钩子桩：记录事件字典（断言 action_type/actor/trace_id/before/after）。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: Mapping[str, Any]) -> None:
        """留存事件快照（浅拷贝防外部篡改）。"""
        self.events.append(dict(event))


class StubSchemaProvider:
    """Schema 注册表桩：产品库含必填 sku 与非必填 remark（metadata 必填校验用例）。"""

    def list_schemas(self) -> list[dict[str, Any]]:
        """单库两字段的最小注册表。"""
        return [
            {
                "kb_type": "product",
                "fields": [
                    {"name": "sku", "label": "SKU", "required": True, "control": "input", "options": None},
                    {"name": "remark", "label": "备注", "required": False, "control": "textarea", "options": None},
                ],
            }
        ]


def make_client(
    *,
    store: InMemoryKnowledgeItemStore | None = None,
    provider: Any = None,
    sync: Any = None,
    audit: Any = None,
) -> TestClient:
    """组装被测应用（真实链路：内存存储 + 注入桩；未注入组件取缺省装配）。"""
    return TestClient(
        create_admin_app(store=store, schema_provider=provider, sync_hook=sync, audit_hook=audit)
    )


def post_item(
    client: TestClient,
    *,
    title: str = "ST-3200 支架参数表",
    kb_type: str = "product",
    action: str = "save_draft",
    metadata: dict[str, Any] | None = None,
    actor: str = "op-a",
    trace_id: str | None = None,
) -> Any:
    """发一次创建请求（成功断言内联；守卫类用例直接用 client.post）。"""
    body: dict[str, Any] = {
        "kb_type": kb_type,
        "title": title,
        "content": f"{title} 正文",
        "metadata": metadata if metadata is not None else {"sku": "ST-3200"},
        "action": action,
    }
    headers = {"X-Actor": actor}
    if trace_id is not None:
        headers["X-Trace-Id"] = trace_id
    resp = client.post(ITEMS, json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["code"] == 0
    return payload["data"]


def put_item(
    client: TestClient,
    uid: str,
    *,
    action: str,
    expected_version: int | None = None,
    title: str = "ST-3200 支架参数表（修订）",
    actor: str = "op-b",
) -> Any:
    """发一次修改请求（成功断言内联；冲突/非法迁移用例直接用 client.put）。"""
    body: dict[str, Any] = {
        "kb_type": "product",
        "title": title,
        "content": f"{title} 正文",
        "metadata": {"sku": "ST-3200"},
        "action": action,
    }
    if expected_version is not None:
        body["expected_version"] = expected_version
    resp = client.put(f"{ITEMS}/{uid}", json=body, headers={"X-Actor": actor})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["code"] == 0
    return payload["data"]


# ─────────────────────────── CRUD 全路径 ───────────────────────────


def test_create_draft_and_get_roundtrip() -> None:
    """新建草稿 + 查详情：uid/状态/版本/修改人回填，草稿不入索引，创建留审计。"""
    sync, audit = StubSyncHook(), StubAuditHook()
    client = make_client(provider=StubSchemaProvider(), sync=sync, audit=audit)

    data = post_item(client, actor="alice", trace_id="trace-create-001")
    assert data["uid"].startswith("k-") and data["status"] == "draft"
    assert data["current_version"] == 1 and data["sync_status"] == "none"
    assert data["updated_by"] == "alice" and data["kb_type"] == "product"

    detail = client.get(f"{ITEMS}/{data['uid']}", headers={"X-Trace-Id": "trace-get-001"})
    assert detail.status_code == 200
    body = detail.json()
    assert body["code"] == 0 and body["trace_id"] == "trace-get-001"
    assert body["data"]["metadata"]["sku"] == "ST-3200"
    assert body["data"]["content"].endswith("正文")

    # 草稿不进向量（SR §2 决策 3）；创建动作留痕（NFR-SEC-03）
    assert sync.calls == []
    assert [event["action"] for event in audit.events] == ["create"]
    event = audit.events[0]
    assert event["action_type"] == "kb_change" and event["state_action"] == "save_draft"
    assert event["actor"] == "alice" and event["trace_id"] == "trace-create-001"
    assert event["before"] is None and event["after"]["uid"] == data["uid"]


def test_create_publish_triggers_sync_upsert() -> None:
    """新建即发布：sync_hook 收到 enqueue(upsert, 幂等键=uid:v1)（SR §9 用例）。"""
    sync, audit = StubSyncHook(), StubAuditHook()
    client = make_client(provider=StubSchemaProvider(), sync=sync, audit=audit)

    data = post_item(client, action="publish")
    assert data["status"] == "published" and data["sync_status"] == "indexing"
    assert sync.calls == [
        {
            "item_uid": data["uid"],
            "change": "upsert",
            "idem_key": f"{data['uid']}:v1",
            "version": 1,
        }
    ]
    assert audit.events[-1]["action"] == "create"
    assert audit.events[-1]["state_action"] == "publish"


def test_full_lifecycle_publish_save_archive_restore() -> None:
    """状态机全链：draft→publish→save_draft(出新版本保持已发布)→archive→restore。"""
    sync, audit = StubSyncHook(), StubAuditHook()
    client = make_client(provider=StubSchemaProvider(), sync=sync, audit=audit)
    data = post_item(client)
    uid = data["uid"]

    # 草稿发布（v2，upsert）
    step = put_item(client, uid, action="publish", expected_version=1)
    assert step["status"] == "published" and step["current_version"] == 2

    # 已发布条目修改保存：出新版本、保持已发布（v3，upsert）
    step = put_item(client, uid, action="save_draft", expected_version=2)
    assert step["status"] == "published" and step["current_version"] == 3

    # 归档（v4，remove）
    step = put_item(client, uid, action="archive", expected_version=3)
    assert step["status"] == "archived" and step["current_version"] == 4

    # 恢复为草稿（v5，不入索引）
    step = put_item(client, uid, action="restore", expected_version=4)
    assert step["status"] == "draft" and step["current_version"] == 5
    assert step["sync_status"] == "none"

    # 索引触发序列与幂等键（SR §6.4：发布/修改已发布 → upsert；归档 → remove）
    assert [call["change"] for call in sync.calls] == ["upsert", "upsert", "remove"]
    assert sync.calls[0]["idem_key"] == f"{uid}:v2"
    assert sync.calls[1]["idem_key"] == f"{uid}:v3"
    assert sync.calls[2]["idem_key"] == f"{uid}:v4"

    # 审计序列：create + 4 次 update（state_action 区分动作）
    assert [event["action"] for event in audit.events] == ["create"] + ["update"] * 4
    assert [event["state_action"] for event in audit.events] == [
        "save_draft", "publish", "save_draft", "archive", "restore",
    ]
    before, after = audit.events[3]["before"], audit.events[3]["after"]
    assert before["status"] == "published" and after["status"] == "archived"


# ─────────────────────────── 状态机与乐观锁守卫 ───────────────────────────


def test_illegal_transition_rejected() -> None:
    """非法状态迁移 40001：draft+archive、published+restore（SR §9 用例）。"""
    client = make_client(provider=StubSchemaProvider())
    data = post_item(client)

    resp = client.put(
        f"{ITEMS}/{data['uid']}",
        json={"kb_type": "product", "title": "t", "metadata": {"sku": "S-1"}, "action": "archive"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40001
    assert body["data"]["errors"][0]["field"] == "action"

    # 已发布条目不能直接 restore（须先 archive）
    put_item(client, data["uid"], action="publish", expected_version=1)
    resp = client.put(
        f"{ITEMS}/{data['uid']}",
        json={"kb_type": "product", "title": "t", "metadata": {"sku": "S-1"}, "action": "restore"},
    )
    assert resp.status_code == 400 and resp.json()["code"] == 40001


def test_optimistic_lock_conflict() -> None:
    """expected_version 落后 → 40901 且数据未被覆盖（SR §9 用例）。"""
    client = make_client(provider=StubSchemaProvider())
    data = post_item(client)
    put_item(client, data["uid"], action="publish", expected_version=1)  # 推进到 v2

    resp = client.put(
        f"{ITEMS}/{data['uid']}",
        json={
            "kb_type": "product",
            "title": "被覆盖的标题",
            "metadata": {"sku": "ST-3200"},
            "action": "save_draft",
            "expected_version": 1,  # 落后一个版本
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == 40901 and "他人" in body["message"] and body["data"] is None

    detail = client.get(f"{ITEMS}/{data['uid']}").json()["data"]
    assert detail["current_version"] == 2 and detail["title"] != "被覆盖的标题"


def test_update_missing_item_40401() -> None:
    """修改/查询不存在的条目 → 40401 统一信封。"""
    client = make_client(provider=StubSchemaProvider())
    body = {"kb_type": "product", "title": "t", "metadata": {"sku": "S-1"}, "action": "save_draft"}
    for method, path in (("put", f"{ITEMS}/k-not-exist"), ("get", f"{ITEMS}/k-not-exist")):
        if method == "put":
            resp = client.put(path, json=body)
        else:
            resp = client.get(path)
        assert resp.status_code == 404
        payload = resp.json()
        assert payload["code"] == 40401 and payload["data"] is None
        assert payload["trace_id"]


# ─────────────────────────── 入参校验（40001） ───────────────────────────


def test_create_action_and_field_guards() -> None:
    """新建守卫：非法 action / 未知 kb_type / 空标题 / 契约外字段 → 40001。"""
    client = make_client(provider=StubSchemaProvider())
    base = {"kb_type": "product", "title": "t", "metadata": {"sku": "S-1"}}

    resp = client.post(ITEMS, json={**base, "action": "archive"})
    assert resp.status_code == 400 and resp.json()["code"] == 40001
    assert resp.json()["data"]["errors"][0]["field"] == "action"

    resp = client.post(ITEMS, json={**base, "kb_type": "oops"})
    assert resp.status_code == 400 and resp.json()["code"] == 40001

    resp = client.post(ITEMS, json={**base, "title": ""})
    assert resp.status_code == 400 and resp.json()["code"] == 40001

    resp = client.post(ITEMS, json={**base, "unknown_field": 1})  # extra=forbid
    assert resp.status_code == 400 and resp.json()["code"] == 40001


def test_metadata_required_validation() -> None:
    """metadata 必填缺失 → 40001 + data.errors 中文定位（SR §5③ 表单逐项标红口径）。"""
    client = make_client(provider=StubSchemaProvider())
    resp = client.post(
        ITEMS,
        json={"kb_type": "product", "title": "t", "metadata": {"remark": "无 SKU"}, "action": "save_draft"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40001
    assert body["data"]["errors"] == [{"field": "sku", "msg": "请填写「SKU」"}]

    # 注册表未登记的库（skill）不做必填校验（宽松口径，见 items.missing_required_fields）
    resp = client.post(
        ITEMS, json={"kb_type": "skill", "title": "t", "metadata": {}, "action": "save_draft"}
    )
    assert resp.status_code == 200 and resp.json()["code"] == 0


# ─────────────────────────── 软删 ───────────────────────────


def test_delete_published_guard_then_soft_delete() -> None:
    """已发布条目直接删除被拒 → 先归档再删成功；删除留审计并触发索引 remove。"""
    sync, audit = StubSyncHook(), StubAuditHook()
    client = make_client(provider=StubSchemaProvider(), sync=sync, audit=audit)
    data = post_item(client, action="publish")
    uid = data["uid"]

    resp = client.delete(f"{ITEMS}/{uid}", headers={"X-Actor": "admin"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40001 and "归档" in body["message"]

    put_item(client, uid, action="archive", expected_version=1)
    resp = client.delete(f"{ITEMS}/{uid}", headers={"X-Actor": "admin"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0 and payload["data"] == {"uid": uid, "deleted": True}

    # 软删后详情 40401、列表不可见
    assert client.get(f"{ITEMS}/{uid}").status_code == 404
    listing = client.get(ITEMS).json()["data"]
    assert listing["total"] == 0 and listing["items"] == []

    # 删除留痕 + 索引 remove（archive 一次、delete 一次）
    assert audit.events[-1]["action"] == "delete"
    assert audit.events[-1]["actor"] == "admin"
    assert audit.events[-1]["after"]["deleted"] is True
    assert [call["change"] for call in sync.calls] == ["upsert", "remove", "remove"]


def test_draft_delete_and_list_invisibility() -> None:
    """草稿可直接软删；软删行从列表/过滤结果消失，幸存条目不受影响。"""
    client = make_client(provider=StubSchemaProvider())
    keep = post_item(client, title="话术-保留")
    gone = post_item(client, title="话术-待删")

    resp = client.delete(f"{ITEMS}/{gone['uid']}")
    assert resp.status_code == 200 and resp.json()["code"] == 0

    listing = client.get(ITEMS, params={"q": "话术"}).json()["data"]
    assert listing["total"] == 1 and listing["items"][0]["uid"] == keep["uid"]
    assert client.get(f"{ITEMS}/{gone['uid']}").status_code == 404


# ─────────────────────────── 分页与过滤 ───────────────────────────


def _seed_catalog(client: TestClient) -> str:
    """灌数 8 条：5 条产品（2 条标题含「支架」）+ 3 条技能（1 条已发布）。

    返回被发布的技能条目 uid（status 过滤混合数据）。
    """
    for index in range(5):
        title = f"支架 ST-32{index:02d} 参数" if index < 2 else f"产品 P-{index:02d}"
        post_item(client, title=title, metadata={"sku": f"ST-32{index:02d}"})
    skill_uids = [
        post_item(client, title=f"话术 S-{index:02d}", kb_type="skill", metadata={})["uid"]
        for index in range(3)
    ]
    resp = client.put(
        f"{ITEMS}/{skill_uids[0]}",
        json={"kb_type": "skill", "title": "话术 S-00", "metadata": {}, "action": "publish"},
    )
    assert resp.status_code == 200, resp.text
    return skill_uids[0]


def test_pagination_and_filters() -> None:
    """分页（默认 20/上限 100/越界 40001）与 kb_type/status/关键词过滤。"""
    client = make_client(provider=StubSchemaProvider())
    _seed_catalog(client)

    listing = client.get(ITEMS).json()["data"]
    assert listing["total"] == 8 and len(listing["items"]) == 8
    assert listing["page"] == 1 and listing["page_size"] == 20  # 默认分页口径

    page1 = client.get(ITEMS, params={"page": 1, "page_size": 3}).json()["data"]
    assert len(page1["items"]) == 3 and page1["total"] == 8 and page1["page_size"] == 3
    page3 = client.get(ITEMS, params={"page": 3, "page_size": 3}).json()["data"]
    assert len(page3["items"]) == 2  # 8 = 3+3+2
    beyond = client.get(ITEMS, params={"page": 9, "page_size": 3}).json()["data"]
    assert beyond["items"] == [] and beyond["total"] == 8  # 越界页空集不报错

    assert client.get(ITEMS, params={"page_size": 100}).status_code == 200  # 上限放行
    for bad_params in ({"page_size": 101}, {"page_size": 0}, {"page": 0}):
        resp = client.get(ITEMS, params=bad_params)
        assert resp.status_code == 400 and resp.json()["code"] == 40001, bad_params

    skills = client.get(ITEMS, params={"kb_type": "skill"}).json()["data"]
    assert skills["total"] == 3 and {row["kb_type"] for row in skills["items"]} == {"skill"}
    published = client.get(ITEMS, params={"status": "published"}).json()["data"]
    assert published["total"] == 1 and published["items"][0]["status"] == "published"
    keyword = client.get(ITEMS, params={"q": "支架"}).json()["data"]
    assert keyword["total"] == 2

    resp = client.get(ITEMS, params={"kb_type": "oops"})
    assert resp.status_code == 400 and resp.json()["code"] == 40001
    assert resp.json()["data"]["errors"][0]["field"] == "kb_type"
    resp = client.get(ITEMS, params={"status": "zzz"})
    assert resp.status_code == 400 and resp.json()["code"] == 40001
    assert resp.json()["data"]["errors"][0]["field"] == "status"


def test_list_row_shape_matches_contract() -> None:
    """列表行字段与 SR-KB-020 §5② items[] 契约一致（键集逐项断言）。"""
    client = make_client(provider=StubSchemaProvider())
    post_item(client)
    row = client.get(ITEMS).json()["data"]["items"][0]
    assert set(row) == {
        "uid", "kb_type", "title", "status", "current_version", "sync_status", "updated_at", "updated_by",
    }
    assert "T" in row["updated_at"]  # ISO 8601 带时区


# ─────────────────────────── 索引失败降级与信封形态 ───────────────────────────


def test_sync_hook_failure_does_not_block_save() -> None:
    """索引入队异常不阻断保存：响应仍成功，sync_status=failed（SR §6.4 口径）。"""
    sync = StubSyncHook(raise_on="upsert")
    audit = StubAuditHook()
    client = make_client(provider=StubSchemaProvider(), sync=sync, audit=audit)

    data = post_item(client, action="publish")
    assert data["status"] == "published" and data["sync_status"] == "failed"
    assert client.get(f"{ITEMS}/{data['uid']}").status_code == 200  # 条目已落库（权威源）
    assert audit.events and audit.events[-1]["action"] == "create"  # 审计不受索引失败影响


def test_envelope_shape_on_success_and_error() -> None:
    """统一信封四键齐全（04 §1.3）：成功 message=ok；错误 data 可为 None/errors。"""
    client = make_client(provider=StubSchemaProvider())

    ok = client.get(ITEMS)
    assert set(ok.json()) == {"code", "message", "data", "trace_id"}
    assert ok.json()["message"] == "ok" and ok.json()["code"] == 0

    err = client.get(ITEMS, params={"page_size": 101})
    assert set(err.json()) == {"code", "message", "data", "trace_id"}
    assert err.json()["code"] == 40001 and "errors" in err.json()["data"]

    missing = client.get(f"{ITEMS}/k-none")
    assert set(missing.json()) == {"code", "message", "data", "trace_id"}
    assert missing.json()["data"] is None


def test_trace_id_passthrough_and_generation() -> None:
    """trace_id：X-Trace-Id 透传复用；缺省新生成（04 §1.4）。"""
    client = make_client(provider=StubSchemaProvider())
    passed = client.get(ITEMS, headers={"X-Trace-Id": "trace-fixed-42"}).json()
    assert passed["trace_id"] == "trace-fixed-42"
    generated = client.get(ITEMS).json()["trace_id"]
    assert isinstance(generated, str) and len(generated) >= 8


def test_store_protocol_runtime_conformance() -> None:
    """InMemory 实现结构性满足 KnowledgeItemStore Protocol（鸭子类型注入前提）。"""
    from services.kb.api.admin.items import KnowledgeItemStore

    assert isinstance(InMemoryKnowledgeItemStore(), KnowledgeItemStore)
    # create 保留显式时间口径（stats 用例的灌数前提）
    record = KnowledgeItemRecord(kb_type="product", title="t")
    stored = InMemoryKnowledgeItemStore().create(record)
    assert stored.uid.startswith("k-") and stored.created_at is not None
