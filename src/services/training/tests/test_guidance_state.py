# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""引导状态与克制门控单测（NFR-USA-03，迭代 1.1；SR-NFR-USA-03 §6.2/§9 test_gate_*）。

覆盖：
- should_show 四口径：无记录→显示；未终结（中断）→显示；已终结且主版本未变→
  不显示；已终结且已看主版本 < 当前主版本（界面改版）→ 重显一次；
- mark_done / mark_skipped：终态落库字段（seen_version/done/dismissed/last_step_seq）；
- 次版本升级不重显（1.0 看过 → 1.1 不弹）；主版本升级重显后再次跳过 → 不再弹；
- seen_version 透传：迟到上报（旧版本页面回调）如实记录所看版本；
- localStorage 契约：键名 / JSON 形态（to/from 往返 + 旧载荷容错）；
- InMemoryGuidanceStateStore：按（学员 × 导览）隔离、覆盖写幂等。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.training.core.guidance.config import FIRST_LOGIN_TOUR, TourConfig
from services.training.core.guidance.state import (
    InMemoryGuidanceStateStore,
    TraineeGuidanceState,
    from_localstorage_payload,
    localstorage_key,
    mark_done,
    mark_skipped,
    should_show,
    to_localstorage_payload,
)


def _tour(version: str, reshow_on_major: bool = True) -> TourConfig:
    """构造指定版本的测试导览（沿用内置首登导览的结构与策略骨架）。"""
    return FIRST_LOGIN_TOUR.model_copy(
        update={"version": version, "skip_policy": FIRST_LOGIN_TOUR.skip_policy.model_copy(
            update={"reshow_on_major": reshow_on_major}
        )}
    )


# --------------------------------------------------------------------------- should_show 四口径


class TestShouldShowGate:
    """克制门控：一切引导可跳过、回访不打扰、仅主版本升级重显一次。"""

    def test_first_visit_shows(self) -> None:
        """无历史记录（新学员）→ 显示（开箱可用测试的初始态）。"""
        store = InMemoryGuidanceStateStore()
        assert should_show(store, FIRST_LOGIN_TOUR, trainee_id=1) is True

    def test_interrupted_tour_shows_again(self) -> None:
        """有记录但未终结（dismissed=False，上次中途关闭）→ 显示（续走不算打扰）。"""
        store = InMemoryGuidanceStateStore()
        store.save(
            TraineeGuidanceState(trainee_id=1, tour_id="first-login", seen_version="1.0", dismissed=False)
        )
        assert should_show(store, FIRST_LOGIN_TOUR, trainee_id=1) is True

    def test_returning_user_not_reshown(self) -> None:
        """已终结（完成或跳过）且版本未变 → 不自动弹（test_gate_returning_user）。"""
        store = InMemoryGuidanceStateStore()
        mark_done(store, FIRST_LOGIN_TOUR, trainee_id=1)
        assert should_show(store, FIRST_LOGIN_TOUR, trainee_id=1) is False
        mark_skipped(store, FIRST_LOGIN_TOUR, trainee_id=2)
        assert should_show(store, FIRST_LOGIN_TOUR, trainee_id=2) is False

    def test_major_bump_reshows_once(self) -> None:
        """主版本升级（1.0→2.0 界面改版）→ 重显一次；再关后同主版本不再弹。"""
        store = InMemoryGuidanceStateStore()
        mark_skipped(store, _tour("1.0"), trainee_id=1)  # 学员在 v1.0 时跳过
        assert should_show(store, _tour("2.0"), trainee_id=1) is True  # v2.0 上线重显
        mark_skipped(store, _tour("2.0"), trainee_id=1)  # 学员再次跳过
        assert should_show(store, _tour("2.3"), trainee_id=1) is False  # v2.x 次版本不再弹

    def test_minor_bump_does_not_reshow(self) -> None:
        """次版本升级（1.0→1.1 文案/步骤微调）→ 不重显（不打扰口径）。"""
        store = InMemoryGuidanceStateStore()
        mark_done(store, _tour("1.0"), trainee_id=1)
        assert should_show(store, _tour("1.1"), trainee_id=1) is False

    def test_seen_version_smaller_minor_still_same_major(self) -> None:
        """已看 1.2，当前 1.0 之外的同主版本各形态：一律不重显（口径只看主版本）。"""
        store = InMemoryGuidanceStateStore()
        store.save(
            TraineeGuidanceState(trainee_id=1, tour_id="first-login", seen_version="1.2", dismissed=True)
        )
        assert should_show(store, _tour("1.0"), trainee_id=1) is False

    def test_reshow_disabled_by_policy(self) -> None:
        """skip_policy.reshow_on_major=False：主版本升级也不重显（克制总闸可关）。"""
        store = InMemoryGuidanceStateStore()
        mark_done(store, _tour("1.0"), trainee_id=1)
        assert should_show(store, _tour("2.0", reshow_on_major=False), trainee_id=1) is False


# --------------------------------------------------------------------------- mark_done / mark_skipped


class TestMarkTerminalStates:
    """终态标记：跳过与完成同权重落库（不区分对待）。"""

    def test_mark_done_fields(self) -> None:
        """完成态：done=True、dismissed=True、seen_version=当前版本、last_step_seq=末步。"""
        store = InMemoryGuidanceStateStore()
        state = mark_done(store, FIRST_LOGIN_TOUR, trainee_id=7)
        assert state.done is True
        assert state.dismissed is True
        assert state.seen_version == FIRST_LOGIN_TOUR.version
        assert state.last_step_seq == len(FIRST_LOGIN_TOUR.steps)
        assert store.load("first-login", 7) == state

    def test_mark_skipped_fields(self) -> None:
        """跳过态：done=False、dismissed=True、携带中断步骤号（流失分析）。"""
        store = InMemoryGuidanceStateStore()
        state = mark_skipped(store, FIRST_LOGIN_TOUR, trainee_id=7, last_step_seq=3)
        assert state.done is False
        assert state.dismissed is True
        assert state.last_step_seq == 3

    def test_seen_version_passthrough(self) -> None:
        """seen_version 透传：迟到上报（改版后旧页面回调）如实记录所看版本，
        门控随之给出「再显一次」结论（已看 1.0 < 当前 2.0）。"""
        store = InMemoryGuidanceStateStore()
        state = mark_skipped(store, _tour("2.0"), trainee_id=9, seen_version="1.0")
        assert state.seen_version == "1.0"
        assert should_show(store, _tour("2.0"), trainee_id=9) is True

    def test_bad_seen_version_rejected(self) -> None:
        """非法 seen_version 串拒收（TourVersionError 为 ValueError 子类）。"""
        store = InMemoryGuidanceStateStore()
        with pytest.raises(ValueError):
            mark_skipped(store, FIRST_LOGIN_TOUR, trainee_id=9, seen_version="v1.0")


# --------------------------------------------------------------------------- localStorage 契约


class TestLocalstorageContract:
    """前端本地态契约：键名 guide:seen:{trainee_id}:{tour_id} + JSON 形态。"""

    def test_key_naming(self) -> None:
        """键名按学员 × 导览隔离（SR §4 约定）。"""
        assert localstorage_key(101, "first-login") == "guide:seen:101:first-login"

    def test_payload_shape(self) -> None:
        """JSON 形态五键：major/minor/dismissed/done/at（毫秒时间戳）。"""
        state = TraineeGuidanceState(
            trainee_id=101, tour_id="first-login", seen_version="1.0",
            done=False, dismissed=True,
        )
        payload = to_localstorage_payload(state)
        assert set(payload) == {"major", "minor", "dismissed", "done", "at"}
        assert payload["major"] == 1 and payload["minor"] == 0
        assert payload["dismissed"] is True and payload["done"] is False
        assert isinstance(payload["at"], int) and payload["at"] > 0

    def test_payload_roundtrip(self) -> None:
        """服务端态 → 本地 JSON → 服务端态：版本/终态字段往返一致。"""
        state = TraineeGuidanceState(
            trainee_id=101, tour_id="first-login", seen_version="2.3",
            done=True, dismissed=True,
        )
        restored = from_localstorage_payload(101, "first-login", to_localstorage_payload(state))
        assert restored.seen_version == "2.3"
        assert restored.done is True and restored.dismissed is True

    def test_legacy_payload_tolerated(self) -> None:
        """旧版载荷缺 minor/done 字段：按缺省回填（minor=0、done=False）不报错。"""
        restored = from_localstorage_payload(101, "first-login", {"major": 2, "dismissed": True})
        assert restored.seen_version == "2.0"
        assert restored.done is False and restored.dismissed is True

    def test_payload_without_major_rejected(self) -> None:
        """缺 major 的载荷视为损坏数据：ValueError（前端按未看过处理）。"""
        with pytest.raises(ValueError):
            from_localstorage_payload(101, "first-login", {"dismissed": True})


# --------------------------------------------------------------------------- 内存存储


class TestInMemoryStore:
    """内存实现：按（学员 × 导览）隔离、覆盖写幂等、frozen 快照语义。"""

    def test_isolated_by_trainee_and_tour(self) -> None:
        """学员 1 与学员 2、first-login 与其他导览互不影响。"""
        store = InMemoryGuidanceStateStore()
        mark_done(store, FIRST_LOGIN_TOUR, trainee_id=1)
        assert store.load("first-login", 2) is None
        assert store.load("other-tour", 1) is None
        assert store.load("first-login", 1) is not None

    def test_overwrite_write(self) -> None:
        """save 覆盖写：同键后写胜出（终态以最后一次上报为准）。"""
        store = InMemoryGuidanceStateStore()
        mark_done(store, FIRST_LOGIN_TOUR, trainee_id=1)
        mark_skipped(store, FIRST_LOGIN_TOUR, trainee_id=1, last_step_seq=2)
        final = store.load("first-login", 1)
        assert final is not None and final.done is False and final.last_step_seq == 2
        assert store.size() == 1


# --------------------------------------------------------------------------- 模型边界（守护 CI 的另一半）


class TestStateModelValidation:
    """TraineeGuidanceState 字段校验：脏数据入库即拒。"""

    @pytest.mark.parametrize("bad_version", ["1", "1.0.0", "x.y"])
    def test_bad_seen_version_rejected_at_model(self, bad_version: str) -> None:
        """非法 seen_version → ValidationError。"""
        with pytest.raises(ValidationError):
            TraineeGuidanceState(trainee_id=1, tour_id="t", seen_version=bad_version)

    def test_frozen_state(self) -> None:
        """终态不可变（frozen）：篡改即抛错——审计口径的只读保障。"""
        state = TraineeGuidanceState(trainee_id=1, tour_id="t", seen_version="1.0")
        with pytest.raises(ValidationError):
            state.done = True  # type: ignore[misc]
