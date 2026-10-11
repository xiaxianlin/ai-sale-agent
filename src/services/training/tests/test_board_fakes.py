# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""主管看板 / 效果报告 TDD 用例公共 Fake 数据源（FR-TR-006 / FR-TR-009，迭代 1.3）。

本模块只含测试桩，不含用例（pytest 收集不到 test_ 函数，0 条用例）：
- :class:`FakeBoardSource`——注入 TeamStatsAggregator / FlagEvaluator 的学员事件流
  （学习日志 / 演练 / 考试 / 雷达快照），形态对齐 SR-TR-006 §6.1 的数据源描述，
  不 import drill/radar/path/exam 具体实现（并行迭代解耦）；
- :class:`FakeTeamDirectory`——主管→团队→学员名录（scope 数据权限注入）；
- :class:`FakeReportSource`——注入 ReportAssembler 的批次/个人报告数据源；
- 审计桩 / 信封断言助手。

全部数据为合成语料（demo 口径，无真实甲方素材）。
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from services.training.core.board.aggregate import (
    DrillEvent,
    ExamEvent,
    LearningEvent,
    RadarSnapshot,
    TraineeProfile,
)

# --------------------------------------------------------------------------- 看板侧桩


class FakeBoardSource:
    """内存学员事件流（构造参数即数据；list 副本返回防测试间串扰）。"""

    def __init__(
        self,
        profiles: Sequence[TraineeProfile],
        learning: Mapping[int, Sequence[LearningEvent]] | None = None,
        drills: Mapping[int, Sequence[DrillEvent]] | None = None,
        exams: Mapping[int, Sequence[ExamEvent]] | None = None,
        radars: Mapping[int, Sequence[RadarSnapshot]] | None = None,
    ) -> None:
        self.profiles: dict[int, TraineeProfile] = {p.trainee_id: p for p in profiles}
        self.learning: dict[int, list[LearningEvent]] = {k: list(v) for k, v in (learning or {}).items()}
        self.drills: dict[int, list[DrillEvent]] = {k: list(v) for k, v in (drills or {}).items()}
        self.exams: dict[int, list[ExamEvent]] = {k: list(v) for k, v in (exams or {}).items()}
        self.radars: dict[int, list[RadarSnapshot]] = {k: list(v) for k, v in (radars or {}).items()}

    def list_trainee_ids(self) -> list[int]:
        """全体学员 id（升序稳定）。"""
        return sorted(self.profiles)

    def trainee_profile(self, trainee_id: int) -> TraineeProfile:
        """学员档案；不存在抛 KeyError（API 层转 40404）。"""
        return self.profiles[trainee_id]

    def iter_learning(self, trainee_id: int) -> Iterator[LearningEvent]:
        """学习日志事件流（SR-TR-005 消费，只读）。"""
        return iter(list(self.learning.get(trainee_id, [])))

    def iter_drills(self, trainee_id: int) -> Iterator[DrillEvent]:
        """演练事件流（SR-TR-003 消费，只读）。"""
        return iter(list(self.drills.get(trainee_id, [])))

    def iter_exams(self, trainee_id: int) -> Iterator[ExamEvent]:
        """考试事件流（SR-TR-007 消费，只读）。"""
        return iter(list(self.exams.get(trainee_id, [])))

    def iter_radars(self, trainee_id: int) -> Iterator[RadarSnapshot]:
        """雷达快照流（SR-TR-004 消费，只读）。"""
        return iter(list(self.radars.get(trainee_id, [])))


class FakeTeamDirectory:
    """团队名录：team_id → 学员集合（生产由 train_team/train_team_member 查询承担）。"""

    def __init__(self, teams: Mapping[int, Iterable[int]]) -> None:
        self.teams: dict[int, frozenset[int]] = {tid: frozenset(m) for tid, m in teams.items()}

    def trainee_ids_of_teams(self, team_ids: Sequence[int]) -> frozenset[int]:
        """多团队学员并集（空集合输入 → 空集）。"""
        result: set[int] = set()
        for tid in team_ids:
            result |= self.teams.get(tid, frozenset())
        return frozenset(result)

    def all_trainee_ids(self) -> frozenset[int]:
        """全体学员（管理员全量口径）。"""
        result: set[int] = set()
        for members in self.teams.values():
            result |= members
        return frozenset(result)


class AuditRecorder:
    """审计桩：记录 (action_type, detail) 元组，供越权留痕断言。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __call__(self, action_type: str, detail: str) -> None:
        self.records.append((action_type, detail))

    def of_type(self, action_type: str) -> list[tuple[str, str]]:
        """按动作类型过滤（rbac_deny 断言用）。"""
        return [r for r in self.records if r[0] == action_type]


# --------------------------------------------------------------------------- 报告侧桩


class FakeReportSource:
    """内存报告数据源：批次/个人两维数据 + 调用计数（sections 装配裁剪断言用）。"""

    def __init__(
        self,
        *,
        batches: Mapping[int, dict[str, Any]] | None = None,
        members: Mapping[int, Sequence[dict[str, Any]]] | None = None,
        radar_means: Mapping[int, dict[str, dict[str, float]]] | None = None,
        gaps: Mapping[int, Sequence[dict[str, Any]]] | None = None,
        individuals: Mapping[int, dict[str, Any]] | None = None,
        page_size: int = 200,
    ) -> None:
        self.batches: dict[int, dict[str, Any]] = dict(batches or {})
        self.members: dict[int, list[dict[str, Any]]] = {k: list(v) for k, v in (members or {}).items()}
        self.radar_means: dict[int, dict[str, dict[str, float]]] = dict(radar_means or {})
        self.gaps: dict[int, list[dict[str, Any]]] = {k: list(v) for k, v in (gaps or {}).items()}
        self.individuals: dict[int, dict[str, Any]] = dict(individuals or {})
        self.page_size: int = page_size
        # 调用观测：键集分页 fetch 记录 (batch_id, after_id, limit) 与缺口查询次数
        self.fetch_log: list[tuple[int, int, int]] = []
        self.gap_calls: int = 0

    # ── 批次维度 ──
    def batch_info(self, batch_id: int) -> dict[str, Any] | None:
        """批次档案（None=不存在）。"""
        return self.batches.get(batch_id)

    def batch_overview(self, batch_id: int) -> dict[str, Any]:
        """批次总览聚合（生产为单条 GROUP BY 下推，demo 直读预置）。"""
        rows = self.members.get(batch_id, [])
        certified = [m for m in rows if m["status"] == "certified"]
        dropped = [m for m in rows if m["status"] == "dropped"]
        cycles = [m["cycle_days"] for m in certified if m.get("cycle_days") is not None]
        return {
            "total": len(rows),
            "certified": len(certified),
            "dropped": len(dropped),
            "avg_cycle_days": (sum(cycles) / len(cycles)) if cycles else None,
        }

    def batch_radar_means(self, batch_id: int) -> dict[str, dict[str, float]]:
        """雷达均值对比（initial=初评快照均值 / current=当前值均值）。"""
        return self.radar_means.get(batch_id, {"initial": {}, "current": {}})

    def fetch_member_page(self, batch_id: int, after_id: int, limit: int) -> list[dict[str, Any]]:
        """键集分页取成员页（id 升序；demo 用列表切片模拟 SQL ``WHERE id > :cursor``）。"""
        self.fetch_log.append((batch_id, after_id, limit))
        rows = sorted(self.members.get(batch_id, []), key=lambda m: m["id"])
        return [dict(r) for r in rows if r["id"] > after_id][:limit]

    def iter_batch_members(self, batch_id: int, page_size: int) -> Iterator[dict[str, Any]]:
        """成员明细流（生产=键集分页游标；demo 复用 core 的 keyset_iterate 助手）。"""
        from services.training.core.board.report import keyset_iterate

        return keyset_iterate(
            lambda after, limit: self.fetch_member_page(batch_id, after, limit),
            page_size=page_size,
            cursor_of=lambda page: page[-1]["id"],
        )

    def batch_gaps(self, batch_id: int) -> list[dict[str, Any]]:
        """知识缺口统计（FR-TR-008 只读消费）。"""
        self.gap_calls += 1
        return list(self.gaps.get(batch_id, []))

    # ── 个人维度 ──
    def trainee_profile(self, trainee_id: int) -> dict[str, Any] | None:
        """学员档案 dict（None=不存在）。"""
        data = self.individuals.get(trainee_id)
        return None if data is None else dict(data["profile"])

    def trainee_timeline(self, trainee_id: int, window_days: int) -> list[dict[str, Any]]:
        """学习历程时间线（演练/考试/问答三类合并）。"""
        data = self.individuals.get(trainee_id)
        return list(data["timeline"]) if data else []

    def trainee_score_details(self, trainee_id: int, window_days: int) -> list[dict[str, Any]]:
        """成绩单明细（演练 + 考试）。"""
        data = self.individuals.get(trainee_id)
        return list(data["scores"]) if data else []

    def trainee_radar_evolution(self, trainee_id: int) -> list[dict[str, Any]]:
        """雷达演进序列（initial/stage/latest）。"""
        data = self.individuals.get(trainee_id)
        return list(data["radar_evolution"]) if data else []

    def trainee_cycle(self, trainee_id: int) -> dict[str, Any] | None:
        """个人培训周期（joined_at→certified_at；未认证 None）。"""
        data = self.individuals.get(trainee_id)
        return None if data is None else data.get("cycle")


# --------------------------------------------------------------------------- 通用助手


def envelope_ok(payload: Mapping[str, Any]) -> None:
    """断言成功信封四键齐全且 code=0（04-接口设计 §1.3）。"""
    assert set(payload) >= {"code", "message", "data", "trace_id"}, payload
    assert payload["code"] == 0, payload


class SleepApp:
    """极简 ASGI 应用：每个请求固定耗时（页面池计时器测试桩）。

    以 ``time.sleep`` 模拟后端处理耗时；响应体恒为 JSON ``{"ok": true}``。
    """

    def __init__(self, delay_s: float = 0.002, paths: Iterable[str] = ("/a", "/b")) -> None:
        import time

        self._time = time
        self._delay = delay_s
        self._paths = tuple(paths)
        self.hits: dict[str, int] = {p: 0 for p in self._paths}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # lifecycle 等非 http 消息直接透传
            return
        path = scope.get("path", "")
        self.hits[path] = self.hits.get(path, 0) + 1
        self._time.sleep(self._delay)
        body = b'{"ok": true}'
        start_headers = [(b"content-type", b"application/json")]
        await send({"type": "http.response.start", "status": 200, "headers": start_headers})
        await send({"type": "http.response.body", "body": body})
