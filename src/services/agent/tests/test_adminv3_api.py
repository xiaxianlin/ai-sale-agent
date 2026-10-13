# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 API 单元测试（FR-SC-005 接口面，SR-SC-005 §5/§9；P4 第二批 TDD）。

覆盖：GET /api/v1/agent/v3/wall（client_ver 参数 → full/delta/ack 帧）；POST
/api/v1/agent/v3/batch（启停/策略，Idempotency-Key 头幂等）+ 进度查询；GET
/api/v1/agent/v3/roi|roi/{account_id}（预聚合只读视图）。错误契约 40001/40401
（中文消息）。装配走 create_adminv3_app 依赖注入（状态墙/批量/ROI 三核心）。

实现方案：docs/07-实现方案/SC/SR-SC-005-管理后台V3.md §5、§9
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi.testclient import TestClient

from services.agent.api.adminv3_api import create_adminv3_app
from services.agent.core.adminv3.batch import BatchOps
from services.agent.core.adminv3.roi import InquiryFact, RoiStats
from services.agent.core.adminv3.wall import ClusterWall, Heartbeat, InstanceMeta

NOW = datetime(2026, 10, 11, 10, 0, 0)
DAY = date(2026, 10, 11)


def build_client() -> tuple[TestClient, ClusterWall, dict]:
    """标准装配：2 实例状态墙 + 记录型批量执行器 + ROI 账本。"""
    wall = ClusterWall(hb_timeout_s=45.0, clock=lambda: NOW)
    for i in (1, 2):
        wall.register(InstanceMeta(account_id=i, account_name=f"acct-{i}"))
        wall.heartbeat(i, Heartbeat(ts=NOW))
    calls: list[tuple[str, int]] = []

    def instance_ctl(action: str, account_id: int) -> None:
        calls.append((action, account_id))
        if account_id == 2:
            raise RuntimeError("容器重启超时")

    ops = BatchOps(instance_ctl=instance_ctl,
                   config_writer=lambda k, v, a: None, clock=lambda: NOW)
    roi = RoiStats()
    roi.record(InquiryFact(account_id=1, conversation_id="c1", stat_date=DAY,
                           replied=True, reply_sec=30.0, gmv_confirmed=120.0))
    roi.rollup_day(DAY)
    app = create_adminv3_app(wall=wall, batch_ops=ops, roi=roi)
    return TestClient(app), wall, {"calls": calls, "ops": ops}


def test_healthz_ok() -> None:
    """存活探针。"""
    client, *_ = build_client()
    assert client.get("/healthz").json()["status"] == "ok"


def test_wall_frames_full_delta_ack_over_http() -> None:
    """状态墙：无参 → full 帧；落后版本 → delta；最新版本 → ack。"""
    client, wall, _ = build_client()
    first_ver = wall.aggregate(now=NOW)["ver"]
    full = client.get("/api/v1/agent/v3/wall").json()
    assert full["type"] == "full" and len(full["snapshot"]) == 2
    wall.heartbeat(2, Heartbeat(ts=NOW, sessions_today=7))
    wall.aggregate(now=NOW)
    delta = client.get("/api/v1/agent/v3/wall", params={"client_ver": first_ver}).json()
    assert delta["type"] == "delta" and set(delta["changes"]) == {2}
    ack = client.get("/api/v1/agent/v3/wall", params={"client_ver": delta["ver"]}).json()
    assert ack == {"type": "ack"}


def test_batch_submit_progress_and_partial_failure() -> None:
    """批量：受理返回任务；部分失败 items 精确标记且成功项不回滚；进度查询一致。"""
    client, _, ctx = build_client()
    resp = client.post("/api/v1/agent/v3/batch",
                       json={"action": "restart", "account_ids": [1, 2]},
                       headers={"X-Idempotency-Key": "k1"})
    assert resp.status_code == 200
    task = resp.json()
    assert task["status"] == "partial" and task["total"] == 2
    fails = [i for i in task["items"] if i["state"] == "fail"]
    assert [f["account_id"] for f in fails] == [2] and fails[0]["error_msg"]
    progress = client.get(f"/api/v1/agent/v3/batch/{task['uid']}").json()
    assert progress["status"] == "partial"
    retry = client.post("/api/v1/agent/v3/batch",
                        json={"action": "restart", "account_ids": [2]},
                        headers={"X-Idempotency-Key": "k2"})
    assert retry.json()["status"] == "failed"                       # 2 号仍失败（Fake 注入）


def test_batch_idempotent_and_error_contract() -> None:
    """幂等：同 Idempotency-Key 重复提交返回同一任务；空清单 → 400/40001；未知任务 → 404。"""
    client, _, _ = build_client()
    body = {"action": "stop", "account_ids": [1]}
    first = client.post("/api/v1/agent/v3/batch", json=body, headers={"X-Idempotency-Key": "same"})
    second = client.post("/api/v1/agent/v3/batch", json=body, headers={"X-Idempotency-Key": "same"})
    assert first.json()["uid"] == second.json()["uid"]
    empty = client.post("/api/v1/agent/v3/batch", json={"action": "stop", "account_ids": []},
                        headers={"X-Idempotency-Key": "k9"})
    assert empty.status_code == 400 and empty.json()["code"] == 40001
    missing = client.get("/api/v1/agent/v3/batch/b-none")
    assert missing.status_code == 404 and missing.json()["code"] == 40401


def test_roi_views_and_unknown_account() -> None:
    """ROI：整体视图与账号视图（预聚合只读）；未知账号 → 40401。"""
    client, _, _ = build_client()
    overview = client.get("/api/v1/agent/v3/roi", params={"date_from": DAY, "date_to": DAY}).json()
    assert overview["totals"]["inquiry_cnt"] == 1
    account = client.get("/api/v1/agent/v3/roi/1", params={"date_from": DAY, "date_to": DAY}).json()
    assert account["account_id"] == 1 and account["reply_rate"] == 1.0
    unknown = client.get("/api/v1/agent/v3/roi/99", params={"date_from": DAY, "date_to": DAY})
    assert unknown.status_code == 404 and unknown.json()["code"] == 40401
