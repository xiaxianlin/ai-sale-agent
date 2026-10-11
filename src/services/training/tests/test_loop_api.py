# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果闭环 API 集成测试（FR-TR-008，SR-TR-008 §5 接口增量；TDD 阶段①用例设计）。

覆盖（统一信封 code/message/data/trace_id，04-接口设计 §1.3）：
- POST /api/v1/training/loop/gap-events：缺口事件上报（dedup new/hit）；
- POST /api/v1/training/loop/gaps/aggregate：触发聚合；
- GET  /api/v1/training/loop/gaps：清单（status 过滤）；
- POST /api/v1/training/loop/gaps/{id}/approve|reject：审核回流（批准→条目 uid；驳回缺原因 40902）；
- GET  /api/v1/training/loop/metrics：闭环率快照。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.training.api.exam_api import (
    LOOP_AGGREGATE_PATH,
    LOOP_GAP_EVENTS_PATH,
    LOOP_GAPS_PATH,
    LOOP_GAP_APPROVE_PATH,
    LOOP_GAP_REJECT_PATH,
    LOOP_METRICS_PATH,
    create_exam_app,
)
from services.training.core.loop.closure import LoopService


@pytest.fixture()
def env() -> dict[str, Any]:
    """标准装配：内存闭环服务 + TestClient。"""
    loop = LoopService()
    return {"client": TestClient(create_exam_app(loop_service=loop)), "loop": loop}


def emit_and_aggregate(client: TestClient, question: str, ref_id: int = 1) -> dict[str, Any]:
    """上报一条导师零命中事件并触发聚合（测试步进助手）。"""
    resp = client.post(
        LOOP_GAP_EVENTS_PATH,
        json={
            "source": "mentor_zero_hit", "ref_type": "mentor_session",
            "ref_id": ref_id, "raw_text": question,
        },
    )
    assert resp.status_code == 200, resp.text
    agg = client.post(LOOP_AGGREGATE_PATH)
    assert agg.status_code == 200
    return resp.json()["data"]


class TestLoopApi:
    """闭环端点：事件上报 / 聚合 / 清单 / 审核 / 指标。"""

    def test_gap_events_endpoint_dedup(self, env: dict[str, Any]) -> None:
        """事件上报：首次 new、重复 hit（幂等口径随信封返回）。"""
        client: TestClient = env["client"]
        payload = {
            "source": "mentor_zero_hit", "ref_type": "mentor_session",
            "ref_id": 7, "raw_text": "信用证软条款怎么识别？",
        }
        first = client.post(LOOP_GAP_EVENTS_PATH, json=payload).json()["data"]
        second = client.post(LOOP_GAP_EVENTS_PATH, json=payload).json()["data"]
        assert first["dedup"] == "new"
        assert second["dedup"] == "hit"
        assert first["event_id"] == second["event_id"]

    def test_aggregate_and_list_endpoint(self, env: dict[str, Any]) -> None:
        """聚合 + 清单：事件归一化入清单（freq/最近发生/状态随行）。"""
        client: TestClient = env["client"]
        emit_and_aggregate(client, "FOB 报价怎么算？")
        resp = client.get(LOOP_GAPS_PATH)
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["count"] == 1
        (gap,) = body["data"]["gaps"]
        assert gap["freq"] == 1
        assert gap["status"] == "open"
        # status 过滤参数
        empty = client.get(LOOP_GAPS_PATH, params={"status": "resolved"}).json()["data"]
        assert empty["count"] == 0

    def test_approve_endpoint_reflow(self, env: dict[str, Any]) -> None:
        """批准端点：带草稿批准 → 条目 uid 回流凭证（status=resolved）。"""
        client: TestClient = env["client"]
        emit_and_aggregate(client, "客户要求样品费减免怎么办？")
        gaps = client.get(LOOP_GAPS_PATH).json()["data"]["gaps"]
        gap_id = gaps[0]["gap_id"]
        resp = client.post(
            LOOP_GAP_APPROVE_PATH.format(gap_id=gap_id),
            json={"reviewer": "管理员", "draft": {"title": "样品费策略", "content": "样品费政策：…"}},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "resolved"
        assert data["item_uid"].startswith("ui-loop-")

    def test_reject_endpoint_without_reason_40902(self, env: dict[str, Any]) -> None:
        """驳回缺原因 → 40902 信封；带原因 → rejected 留痕。"""
        client: TestClient = env["client"]
        emit_and_aggregate(client, "T/T 与 L/C 的风险差异？")
        gap_id = client.get(LOOP_GAPS_PATH).json()["data"]["gaps"][0]["gap_id"]
        missing = client.post(LOOP_GAP_REJECT_PATH.format(gap_id=gap_id), json={"reviewer": "管理员"})
        assert missing.status_code == 409
        assert missing.json()["code"] == 40902
        ok = client.post(
            LOOP_GAP_REJECT_PATH.format(gap_id=gap_id),
            json={"reviewer": "管理员", "reason": "与既有条目重复"},
        )
        assert ok.status_code == 200
        assert ok.json()["data"]["status"] == "rejected"

    def test_metrics_endpoint(self, env: dict[str, Any]) -> None:
        """闭环率端点：识别/入库/再命中/闭环率四键齐备。"""
        client: TestClient = env["client"]
        emit_and_aggregate(client, "paypal 手续费谁承担？")
        resp = client.get(LOOP_METRICS_PATH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"identified", "resolved", "rehit", "closure_rate"}
        assert data["identified"] == 1
