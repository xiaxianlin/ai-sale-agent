# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训服务 API 端到端测试（FR-TR-001：SSE 流式 + 历史 + 缺口 + 信封错误码）。"""
from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from services.training.api.app import create_training_app
from services.training.core.tutor.gap import KnowledgeGapRecorder
from services.training.core.tutor.llm import FakeTutorLlm
from services.training.core.tutor.pipeline import InMemoryQaStore

from .conftest import make_search_fn, sample_results

ASK_PATH = "/api/v1/training/tutor/ask"


def build_client(results: list[dict[str, Any]] | None = None):
    """装配被测应用：桩检索 + Fake LLM + 内存存储（组件经工厂注入）。"""
    search = make_search_fn(results if results is not None else sample_results(2))
    llm = FakeTutorLlm()
    qa_store = InMemoryQaStore()
    gap_recorder = KnowledgeGapRecorder()
    app = create_training_app(
        search_fn=search, llm=llm, qa_store=qa_store, gap_recorder=gap_recorder
    )
    return TestClient(app), llm, qa_store, gap_recorder


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """解析 SSE 文本 → [(event, data)] 序列（逐事件断言的解析器）。"""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event, data = "", {}
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        events.append((event, data))
    return events


class TestAskSse:
    def test_hit_path_streaming_events(self) -> None:
        """命中路径：token* → done，done 含 knowledge_refs（SSE 逐事件断言）。"""
        client, llm, _, _ = build_client()
        with client.stream(
            "POST", ASK_PATH, json={"question": "ST-3200 最大承载是多少？", "session": "s1"}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            text = "".join(response.iter_text())
        events = parse_sse(text)
        types = [e for e, _ in events]
        assert types[0] == "token" and types[-1] == "done"
        assert all(t == "token" for t in types[1:-1])
        assert llm.calls == 1
        done = events[-1][1]
        assert done["is_gap"] is False
        assert done["trace_id"]
        assert [r["no"] for r in done["knowledge_refs"]] == [1, 2]
        assert done["knowledge_refs"][0]["chunk_id"] == 1000
        # token 聚合与 done.answer 一致（流式与落库同源）
        tokens = "".join(d["text"] for e, d in events if e == "token")
        assert tokens == done["answer"]
        assert "[来源1]" in done["answer"]

    def test_zero_hit_path_gap_event(self) -> None:
        """零命中路径：gap → done（固定话术 + 缺口登记），无 token 事件。"""
        client, llm, _, gap_recorder = build_client(results=[])
        with client.stream("POST", ASK_PATH, json={"question": "虚空问题", "session": "s1"}) as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())
        events = parse_sse(text)
        assert [e for e, _ in events] == ["gap", "done"]
        assert llm.calls == 0  # 零命中不调 LLM
        assert "知识库暂无该内容，已记录" in events[0][1]["message"]
        assert events[0][1]["gap_id"]
        assert events[-1][1]["is_gap"] is True
        assert len(gap_recorder.list_gaps()) == 1

    def test_empty_question_rejected_40001(self) -> None:
        """空问题：40001 JSON 信封（流建立前失败）。"""
        client, _, _, _ = build_client()
        response = client.post(ASK_PATH, json={"question": "   "})
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == 40001 and body["data"] is None

    def test_question_too_long_rejected_40001(self) -> None:
        client, _, _, _ = build_client()
        response = client.post(ASK_PATH, json={"question": "长" * 501})
        assert response.status_code == 400
        assert response.json()["code"] == 40001

    def test_downstream_error_emits_sse_error_event(self) -> None:
        """检索故障：流内 error 事件（code 50002），不落库不记缺口。"""
        def broken_search(**kwargs: Any):
            raise TimeoutError("kb down")

        llm = FakeTutorLlm()
        qa_store = InMemoryQaStore()
        gap_recorder = KnowledgeGapRecorder()
        app = create_training_app(
            search_fn=broken_search, llm=llm, qa_store=qa_store, gap_recorder=gap_recorder
        )
        client = TestClient(app)
        with client.stream("POST", ASK_PATH, json={"question": "任意", "session": "s"}) as response:
            assert response.status_code == 200  # SSE 已建立，错误在流内下发
            text = "".join(response.iter_text())
        events = parse_sse(text)
        assert len(events) == 1
        event, data = events[0]
        assert event == "error"
        assert data["code"] == 50002
        assert "知识服务暂不可用" in data["message"]
        assert qa_store.count() == 0 and gap_recorder.list_gaps() == []

    def test_idempotency_key_header_replays(self) -> None:
        """同 Idempotency-Key 头重发：不重复落库，重放首次 done 事件。"""
        client, llm, qa_store, _ = build_client()
        for _ in range(2):
            with client.stream(
                "POST",
                ASK_PATH,
                json={"question": "第一次提问", "session": "s1", "idempotency_key": "msg-9"},
                headers={"Idempotency-Key": "hdr-9"},
            ) as response:
                text = "".join(response.iter_text())
            events = parse_sse(text)
        # 第二次请求：单 done 事件（重放），LLM 只被调用一次，落库仅一条
        assert llm.calls == 1
        assert qa_store.count("s1") == 1
        assert [e for e, _ in events] == ["done"]
        assert events[0][1]["idempotent_replay"] is True


class TestHistoryAndGaps:
    def test_session_history_endpoint(self) -> None:
        """历史端点：返回该会话全部记录（含 knowledge_refs）。"""
        client, _, _, _ = build_client()
        client.post(ASK_PATH, json={"question": "问题一", "session": "s1"})
        client.post(ASK_PATH, json={"question": "问题二", "session": "s1"})
        client.post(ASK_PATH, json={"question": "别会话", "session": "s2"})
        response = client.get("/api/v1/training/tutor/sessions/s1")
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 0 and body["trace_id"]
        data = body["data"]
        assert data["session"] == "s1" and data["count"] == 2
        assert [r["question"] for r in data["records"]] == ["问题一", "问题二"]
        assert data["records"][0]["knowledge_refs"][0]["chunk_id"] == 1000

    def test_gaps_endpoint_sorted_by_hotness(self) -> None:
        """缺口端点：按 hit_count 降序，status 过滤可用（FR-TR-008 消费形态）。"""
        client, _, _, _ = build_client(results=[])
        for _ in range(3):
            client.post(ASK_PATH, json={"question": "热门缺口问题", "session": "s"})
        client.post(ASK_PATH, json={"question": "冷门缺口问题", "session": "s"})
        body = client.get("/api/v1/training/gaps").json()
        assert body["code"] == 0
        gaps = body["data"]["gaps"]
        assert body["data"]["count"] == 2
        assert gaps[0]["sample_question"] == "热门缺口问题" and gaps[0]["hit_count"] == 3
        assert gaps[1]["hit_count"] == 1
        assert gaps[0]["status"] == "pending"
        pending = client.get("/api/v1/training/gaps", params={"status": "resolved"}).json()
        assert pending["data"]["count"] == 0

    def test_healthz(self) -> None:
        client, _, _, _ = build_client()
        assert client.get("/healthz").json() == {"status": "ok"}
