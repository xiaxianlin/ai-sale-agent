# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 集群状态墙单元测试（FR-SC-005，SR-SC-005 §6.1/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（28 账号心跳汇聚、健康三态 ok/warn/down）/
边界（心跳恰好超时两侧、down 连续 2 周期告警阈值）/ 异常（未注册账号心跳拒绝）/
幂等（数据无变化不增版本不产 delta；WS resume 版本号决定 ack/delta/full）/ 降级
（缺心跳实例判 down 高亮、仅影响该卡片——NFR-REL-02；版本过旧重发全量帧）。
验收口径映射：拔线演练 ≤30s 状态墙变红（SRS §16.5）→ down 判定与告警用例组。

实现方案：docs/07-实现方案/SC/SR-SC-005-管理后台V3.md §6.1、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.agent.core.adminv3.wall import ClusterWall, Heartbeat, InstanceMeta

NOW = datetime(2026, 10, 11, 10, 0, 0)


def register_28(wall: ClusterWall) -> None:
    """注册 28 账号档案（4 主账号 + 24 子账号，账号/IP 规模口径）。"""
    for i in range(1, 29):
        wall.register(InstanceMeta(
            account_id=i, account_name=f"acct-{i:02d}",
            account_type="main" if i <= 4 else "sub",
            container_name=f"agent-{i:02d}", proxy_ip=f"<PROXY-IP-{i:02d}>"))


def make_wall(**kw: object) -> ClusterWall:
    """标准状态墙：注册 28 账号 + 全量心跳（28 号缺心跳用于降级断言）。"""
    base: dict = dict(hb_timeout_s=45.0, clock=lambda: NOW)
    base.update(kw)
    wall = ClusterWall(**base)  # type: ignore[arg-type]
    register_28(wall)
    for i in range(1, 28):
        wall.heartbeat(i, Heartbeat(ts=NOW - timedelta(seconds=10), sessions_today=i, ops_today=i))
    return wall


class TestAggregationAndHealth:
    """等价类：心跳汇聚与健康三态判定。"""

    def test_snapshot_covers_28_with_main_sub_split(self) -> None:
        """28 账号全量汇聚：4 主 + 24 子，卡片字段齐备。"""
        cards = {c["account_id"]: c for c in make_wall().cards()}
        assert len(cards) == 28
        assert sum(1 for c in cards.values() if c["account_type"] == "main") == 4
        assert {"health", "state", "sessions_today", "ops_today", "ip_status"} <= set(cards[1])

    def test_health_ok_warn_down_three_states(self) -> None:
        """三态：新鲜心跳 ok；ip_status=switched warn；心跳 >45s down（46s 边界）。"""
        wall = make_wall()
        wall.heartbeat(5, Heartbeat(ts=NOW - timedelta(seconds=5), ip_status="switched"))
        wall.aggregate(now=NOW)
        assert wall.health_of(1) == "ok"
        assert wall.health_of(5) == "warn"
        assert wall.health_of(28) == "down"

    def test_hb_timeout_boundary_one_second(self) -> None:
        """边界：心跳 44s 前 → ok；46s 前 → down（timeout=45s 两侧）。"""
        wall = make_wall()
        wall.heartbeat(3, Heartbeat(ts=NOW - timedelta(seconds=44)))
        wall.heartbeat(4, Heartbeat(ts=NOW - timedelta(seconds=46)))
        wall.aggregate(now=NOW)
        assert wall.health_of(3) == "ok" and wall.health_of(4) == "down"

    def test_missing_heartbeat_down_others_unaffected(self) -> None:
        """降级/影响面：28 号缺心跳 → 仅该卡 down，其余 27 张 ok（NFR-REL-02）。"""
        wall = make_wall()
        wall.aggregate(now=NOW)
        assert wall.abnormal_count == 1
        assert sum(1 for c in wall.cards() if c["health"] == "ok") == 27

    def test_abnormal_cards_sorted_first_with_highlight(self) -> None:
        """异常高亮：down/warn 卡片置顶且 highlight=True（一眼定位）。"""
        wall = make_wall()
        wall.heartbeat(3, Heartbeat(ts=NOW - timedelta(seconds=5), ip_status="stale"))
        wall.aggregate(now=NOW)
        cards = wall.cards()
        assert cards[0]["health"] in {"down", "warn"}
        assert all(c["highlight"] for c in cards if c["health"] != "ok")

    def test_unregistered_heartbeat_rejected(self) -> None:
        """异常：未注册账号心跳 → 拒绝（40401 语义）。"""
        with pytest.raises(KeyError):
            make_wall().heartbeat(99, Heartbeat(ts=NOW))


class TestWsDeltaSemantics:
    """幂等/并发：WS 增量推送语义（版本号 + delta，SR-SC-005 §6.1）。"""

    def test_no_change_no_version_bump(self) -> None:
        """幂等：数据无变化 → 版本号不增、无变更项（心跳年龄等易变字段不计入 diff）。"""
        wall = make_wall()
        first = wall.aggregate(now=NOW)
        second = wall.aggregate(now=NOW + timedelta(seconds=5))
        assert second["ver"] == first["ver"] and second["changed"] == 0

    def test_change_produces_delta_frame(self) -> None:
        """变化 → 版本号 +1，delta 仅含变化账号（28 卡片级小报文）。"""
        wall = make_wall()
        first = wall.aggregate(now=NOW)
        wall.heartbeat(7, Heartbeat(ts=NOW - timedelta(seconds=1), sessions_today=999))
        second = wall.aggregate(now=NOW + timedelta(seconds=5))
        assert second["ver"] == first["ver"] + 1
        assert set(second["changes"]) == {7}

    def test_frame_full_then_delta_then_ack(self) -> None:
        """WS 帧序列：无版本 → full；落后版本 → delta；最新版本 → ack。"""
        wall = make_wall()
        first = wall.aggregate(now=NOW)
        full = wall.frame(None)
        assert full["type"] == "full" and len(full["snapshot"]) == 28
        wall.heartbeat(7, Heartbeat(ts=NOW - timedelta(seconds=1), sessions_today=999))
        wall.aggregate(now=NOW + timedelta(seconds=5))
        delta = wall.frame(first["ver"])
        assert delta["type"] == "delta" and set(delta["changes"]) == {7}
        assert wall.frame(delta["ver"]) == {"type": "ack"}

    def test_too_stale_client_gets_full_frame(self) -> None:
        """降级：版本过于落后（超出增量日志覆盖）→ 重发全量帧（断线恢复兜底）。"""
        wall = make_wall(max_delta_log=1)
        wall.heartbeat(28, Heartbeat(ts=NOW))
        stale_ver = wall.aggregate(now=NOW)["ver"]
        for i in (1, 2):
            wall.heartbeat(i, Heartbeat(ts=NOW, sessions_today=i))
            wall.aggregate(now=NOW)
        assert wall.frame(stale_ver)["type"] == "full"


class TestDownAlert:
    """边界/告警：失联告警（FR-AG-007 路由，§3.5 alerts）。"""

    def test_down_alert_after_two_cycles_with_recovery(self) -> None:
        """连续 2 周期 down 才触发 mid 告警；恢复后连击清零不再重复告警。"""
        wall = make_wall()

        def beat_others(moment: datetime) -> None:
            """其余 27 账号心跳正常续跳（隔离 28 号失联变量，时钟推进后不连带失联）。"""
            for i in range(1, 28):
                wall.heartbeat(i, Heartbeat(ts=moment - timedelta(seconds=10),
                                            sessions_today=i, ops_today=i))

        wall.heartbeat(28, Heartbeat(ts=NOW))
        wall.aggregate(now=NOW)
        beat_others(NOW + timedelta(seconds=50))
        wall.aggregate(now=NOW + timedelta(seconds=50))               # 第 1 周期 down
        assert wall.alerts == []
        beat_others(NOW + timedelta(seconds=100))
        wall.aggregate(now=NOW + timedelta(seconds=100))              # 第 2 周期仍 down
        assert len(wall.alerts) == 1
        assert (wall.alerts[0]["type"], wall.alerts[0]["level"]) == ("instance_down", "mid")
        wall.heartbeat(28, Heartbeat(ts=NOW + timedelta(seconds=110)))
        beat_others(NOW + timedelta(seconds=120))
        wall.aggregate(now=NOW + timedelta(seconds=120))
        assert wall.health_of(28) == "ok"
        beat_others(NOW + timedelta(seconds=180))
        wall.aggregate(now=NOW + timedelta(seconds=180))
        beat_others(NOW + timedelta(seconds=240))
        wall.aggregate(now=NOW + timedelta(seconds=240))
        assert len(wall.alerts) == 1
