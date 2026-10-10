# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
"""版本管理 API 集成测试（FR-KB-022 §9：TestClient 全链路）。

覆盖（SR-KB-022 §5 接口定义）：
- GET  /items/{uid}/versions：倒序时间线 + is_current + rollback_from 徽标字段；
- GET  /items/{uid}/versions/{v1}/diff/{v2}：DiffReport UI 数据契约（信封包裹）；
- POST /items/{uid}/rollback：引用式回滚生效（新版本 + rollback_from）；
- 错误码：40401 条目/版本不存在、40901 乐观并发冲突、40001 参数非法/越界。

迭代 0.3 偏差：JWT/RBAC 未接入（X-Actor 头注入操作人），鉴权用例随平台身份
组件迭代补齐（NFR-SEC-02 对应 40301 用例不在本迭代范围）。

实现方案：docs/07-实现方案/KB/SR-KB-022-版本控制.md §5、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.kb.api.admin.versions import (
    install_version_error_handlers,
    router as versions_router,
)
from services.kb.core.versioning import VersioningService
from services.kb.core.versioning.store import InMemoryVersionStore

VERSIONS_PATH = "/api/v1/knowledge/items/{uid}/versions"
ROLLBACK_PATH = "/api/v1/knowledge/items/{uid}/rollback"


class _Hooks:
    """resync / audit 录音桩（断言 API 回滚触发的钩子联动）。"""

    def __init__(self) -> None:
        self.resync_calls: list[tuple[str, str, int]] = []
        self.audit_events: list[dict[str, Any]] = []

    def resync(self, item_id: str, action: str, version: int) -> None:
        self.resync_calls.append((item_id, action, version))

    def audit(self, event: dict[str, Any]) -> None:
        self.audit_events.append(event)


def _build_client() -> tuple[TestClient, VersioningService, _Hooks]:
    """组装被测应用：versions 路由 + 错误信封处理器 + app.state 注入服务。"""
    hooks = _Hooks()
    service = VersioningService(
        InMemoryVersionStore(), resync_hook=hooks.resync, audit_hook=hooks.audit
    )
    app = FastAPI(title="versions api test")
    install_version_error_handlers(app)
    app.include_router(versions_router)
    app.state.versioning_service = service
    return TestClient(app), service, hooks


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kb_type": "product",
        "title": "ST-3200 支架",
        "content": "# 概述\n工业级铝合金支架。",
        "metadata": {"sku": "ST-3200", "tags": ["询价", "报价"]},
        "status": "published",
    }
    base.update(overrides)
    return base


def _seed_two_versions(service: VersioningService) -> str:
    """经 service 播种 v1（旧标题）→ v2（改标题+加标签），返回 uid。"""
    item, _ = service.create_item(_payload(), actor="admin-01")
    service.save(
        item.item_id,
        _payload(
            title="ST-3200 支架（含认证）",
            metadata={"sku": "ST-3200", "tags": ["询价", "报价", "售后"]},
        ),
        actor="admin-02",
        expected_version=1,
    )
    return item.item_id


def _assert_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """统一信封四键齐全（04-接口设计 §1.3），成功时取 data 段。"""
    assert set(body) == {"code", "message", "data", "trace_id"}
    assert body["code"] == 0
    assert isinstance(body["trace_id"], str) and body["trace_id"]
    return body["data"]


# ── 版本历史 ───────────────────────────────────────────────────────────────


def test_list_versions_desc_with_current_flag() -> None:
    """版本历史：倒序时间线（V2 在前），is_current 指向当前版，rollback_from 为空。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)

    resp = client.get(VERSIONS_PATH.format(uid=uid))
    assert resp.status_code == 200
    data = _assert_envelope(resp.json())
    assert data["total"] == 2
    assert [v["version"] for v in data["versions"]] == [2, 1]  # 倒序
    current = data["versions"][0]
    assert current["is_current"] is True and current["rollback_from"] is None
    assert current["created_by"] == "admin-02"
    assert data["versions"][1]["is_current"] is False


def test_list_versions_pagination_window() -> None:
    """分页切片：page=1&page_size=1 只取最新一版。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)

    resp = client.get(VERSIONS_PATH.format(uid=uid), params={"page": 1, "page_size": 1})
    data = _assert_envelope(resp.json())
    assert data["total"] == 2
    assert [v["version"] for v in data["versions"]] == [2]


# ── 双版对比 ───────────────────────────────────────────────────────────────


def test_diff_endpoint_returns_ui_contract() -> None:
    """对比端点：DiffReport UI 契约（summary/fields，text→segments、tree→leaves）。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)

    resp = client.get(
        f"/api/v1/knowledge/items/{uid}/versions/1/diff/2",
        headers={"X-Trace-Id": "trace-diff-001"},
    )
    assert resp.status_code == 200
    assert resp.json()["trace_id"] == "trace-diff-001"  # trace 透传
    data = _assert_envelope(resp.json())
    assert data["from"] == {"version": 1} and data["to"] == {"version": 2}
    assert data["summary"]["modified"] >= 2
    fields = {entry["field"]: entry for entry in data["fields"]}
    assert "segments" in fields["title"] and fields["title"]["kind"] == "text"
    assert fields["metadata"]["leaves"] == [
        {"path": "$.tags[2]", "a": None, "b": "售后", "op": "added"}
    ]


def test_diff_endpoint_same_version_empty() -> None:
    """同版对比：空差异成功响应。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)
    resp = client.get(f"/api/v1/knowledge/items/{uid}/versions/2/diff/2")
    assert resp.status_code == 200
    assert _assert_envelope(resp.json())["fields"] == []


# ── 回滚 ───────────────────────────────────────────────────────────────────


def test_rollback_endpoint_creates_referencing_version() -> None:
    """回滚端点：生成 V3（rollback_from=1），resync 恰一次，时间线出现回滚徽标。"""
    client, service, hooks = _build_client()
    uid = _seed_two_versions(service)

    resp = client.post(
        ROLLBACK_PATH.format(uid=uid),
        json={"to_version": 1, "reason": "误改回退", "expected_version": 2},
        headers={"X-Actor": "admin-01"},
    )
    assert resp.status_code == 200
    data = _assert_envelope(resp.json())
    assert data == {"item_uid": uid, "new_version": 3, "rollback_from": 1}
    assert hooks.resync_calls == [(uid, "upsert", 3)]  # FR-KB-017 联动恰一次
    assert service.get_item(uid).payload["title"] == "ST-3200 支架"  # 内容恢复

    listed = _assert_envelope(client.get(VERSIONS_PATH.format(uid=uid)).json())
    assert [v["version"] for v in listed["versions"]] == [3, 2, 1]  # 链完整
    v3 = listed["versions"][0]
    assert v3["is_current"] is True and v3["rollback_from"] == 1
    assert "回滚至 V1" in v3["change_note"]


def test_rollback_endpoint_without_expected_version() -> None:
    """expected_version 可选：缺省同样回滚成功（基础 body {to_version, reason}）。"""
    client, _, _ = _build_client()
    uid = _seed_two_versions(_client_service(client))
    resp = client.post(ROLLBACK_PATH.format(uid=uid), json={"to_version": 1, "reason": "r"})
    assert resp.status_code == 200
    assert resp.json()["data"]["new_version"] == 3


def _client_service(client: TestClient) -> VersioningService:
    """取被测应用注入的 service（播种数据用）。"""
    return client.app.state.versioning_service  # type: ignore[no-any-return]


# ── 错误码路径 ─────────────────────────────────────────────────────────────


def test_unknown_item_40401() -> None:
    """未知条目：404 + 40401 信封。"""
    client, _, _ = _build_client()
    resp = client.get(VERSIONS_PATH.format(uid="no-such-uid"))
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 40401 and body["data"] is None
    assert set(body) == {"code", "message", "data", "trace_id"}


def test_rollback_missing_version_40401() -> None:
    """回滚到不存在版本：404 + 40401，版本链不变。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)
    resp = client.post(ROLLBACK_PATH.format(uid=uid), json={"to_version": 99})
    assert resp.status_code == 404 and resp.json()["code"] == 40401
    assert service.get_item(uid).current_version == 2


def test_rollback_stale_expected_version_40901() -> None:
    """乐观并发冲突：expected_version 落后 → 409 + 40901。"""
    client, _, _ = _build_client()
    uid = _seed_two_versions(_client_service(client))
    resp = client.post(
        ROLLBACK_PATH.format(uid=uid),
        json={"to_version": 1, "reason": "", "expected_version": 1},  # 当前已是 V2
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == 40901 and "已被他人修改" in body["message"]


def test_invalid_version_path_40001() -> None:
    """非法版本号（路径参数越界）：400 + 40001（data.errors 定位字段）。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)
    resp = client.get(f"/api/v1/knowledge/items/{uid}/versions/0/diff/1")
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40001
    assert body["data"]["errors"]


def test_rollback_extra_field_rejected_40001() -> None:
    """契约外字段 fail-closed（extra=forbid）：400 + 40001。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)
    resp = client.post(
        ROLLBACK_PATH.format(uid=uid),
        json={"to_version": 1, "reason": "x", "bogus_field": 1},
    )
    assert resp.status_code == 400 and resp.json()["code"] == 40001


def test_rollback_missing_to_version_40001() -> None:
    """缺必填 to_version：400 + 40001。"""
    client, service, _ = _build_client()
    uid = _seed_two_versions(service)
    resp = client.post(ROLLBACK_PATH.format(uid=uid), json={"reason": "缺目标版本"})
    assert resp.status_code == 400 and resp.json()["code"] == 40001
