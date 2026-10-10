# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""学员引导状态：跳过态/完成态存储 + 克制门控 should_show（NFR-USA-03，迭代 1.1）。

模块职责（SR-NFR-USA-03 §2 克制原则 + §6.2 useTour 门控的后端镜像）：
- ``TraineeGuidanceState``：按「学员 × 导览」记录的终态——已看版本（seen_version）、
  是否完成（done）、是否已终止（dismissed，完成或跳过均置 True）；
- ``GuidanceStateStore``：存储协议（load/save），生产为 DB/Redis 实现，
  本迭代交付 :class:`InMemoryGuidanceStateStore`（线程安全内存实现，测试与本地开发用）；
- 门控三函数：``should_show``（克制门控）/ ``mark_done``（完成）/ ``mark_skipped``（跳过）。

克制门控规则（should_show，SR §2「一切引导可跳过；回访用户不再自动弹出；
仅 tour 主版本升级允许重显一次」的精确口径）：
1. 无历史记录（新学员/清过缓存）→ 显示；
2. 有记录但未终止（dismissed=False，上次中途关闭）→ 显示（续走，不算打扰）；
3. 已终止（完成或跳过）且导览 skip_policy.reshow_on_major=True
   且 已看主版本 < 当前主版本（= 界面改版）→ 重显一次；
4. 其余（同主版本的次版本升级、已看过当前主版本）→ 不显示。

localStorage 对接契约（前端本地态，与后端 store 双口径一致；见 web/training/src/
components/GuidanceTour.vue 的同构实现）：
- 键名：``guide:seen:{trainee_id}:{tour_id}``（按学员按导览隔离，SR §4 约定）；
- JSON 形态：``{"major": 1, "minor": 0, "dismissed": true, "done": false,
  "at": 1760000000000}``——major/minor 为已看版本拆解，at 为毫秒时间戳；
- 前端在导览销毁时写本地并 POST /api/v1/training/guidance/tour/state 上报，
  后端 store 落服务端态（多端/清缓存兜底）；should_show 判定前端先查本地、
  再以服务端 GET /tour?trainee_id= 返回为准，两端规则同源（本模块）。

依据文档：
- docs/07-实现方案/NFR/SR-NFR-USA-03-引导式交互.md §2（克制原则）、§4（localStorage 键）、
  §6.2（useTour 门控与重显一次）、§9（test_gate_* 单测口径）
- docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）
"""
from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.training.core.guidance.config import TourConfig, parse_tour_version


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（秒精度）：状态 updated_at 统一口径。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TraineeGuidanceState(BaseModel):
    """学员 × 导览的引导终态（服务端权威态；前端 localStorage 为同构镜像）。

    字段口径：
    - ``seen_version``：学员看完/跳过时所处的导览版本（major.minor）；
    - ``done``：True=完整走完（tour_finished），False=跳过/中途终止（tour_skipped）；
    - ``dismissed``：终态标记（完成或跳过均置 True；False 表示上次中断未终结）；
    - ``last_step_seq``：终止时所处步骤号（中断步骤，供流失分析，SR §6.2）；
    - ``updated_at``：最近一次状态变更时间（ISO-8601 UTC）。
    """

    model_config = ConfigDict(frozen=True)

    trainee_id: int = Field(ge=1, description="学员 ID")
    tour_id: str = Field(min_length=1, description="导览标识")
    seen_version: str = Field(min_length=1, description="已看版本（major.minor）")
    done: bool = Field(default=False, description="True=完整走完；False=跳过或中途终止")
    dismissed: bool = Field(default=False, description="终态标记：完成/跳过 True，中断 False")
    last_step_seq: int | None = Field(default=None, ge=1, description="终止时步骤号（流失分析）")
    updated_at: str = Field(default_factory=_now_iso, description="最近变更时间（ISO-8601 UTC）")

    @field_validator("seen_version")
    @classmethod
    def _seen_version_format(cls, value: str) -> str:
        """已看版本串格式校验（与 TourConfig.version 同规约）。"""
        parse_tour_version(value)
        return value

    @property
    def major(self) -> int:
        """已看主版本（重显判定口径）。"""
        return parse_tour_version(self.seen_version)[0]

    @property
    def minor(self) -> int:
        """已看次版本。"""
        return parse_tour_version(self.seen_version)[1]


class GuidanceStateStore(Protocol):
    """引导状态存储协议：生产实现为 DB/Redis，本迭代交付内存实现。

    协议刻意最小（load/save 两个方法）：键恒为 (tour_id, trainee_id)，
    实现方自行决定物理布局（表结构/Redis 键），上层（门控与服务）零耦合。
    """

    def load(self, tour_id: str, trainee_id: int) -> TraineeGuidanceState | None:
        """取某学员某导览的状态；无记录返回 None。"""
        ...  # pragma: no cover - Protocol 桩

    def save(self, state: TraineeGuidanceState) -> None:
        """写入/覆盖某学员某导览的状态（幂等覆盖写）。"""
        ...  # pragma: no cover - Protocol 桩


class InMemoryGuidanceStateStore:
    """内存实现（线程安全）：单元测试、API 集成测试与本地开发用。

    布局：dict[(trainee_id, tour_id)] → TraineeGuidanceState；读路径无锁取快照
    （模型 frozen，引用共享安全），写路径持锁。
    """

    def __init__(self, initial: Mapping[tuple[int, str], TraineeGuidanceState] | None = None) -> None:
        self._lock = threading.Lock()
        self._states: dict[tuple[int, str], TraineeGuidanceState] = dict(initial or {})

    def load(self, tour_id: str, trainee_id: int) -> TraineeGuidanceState | None:
        """取状态快照；无记录返回 None。"""
        with self._lock:
            return self._states.get((trainee_id, tour_id))

    def save(self, state: TraineeGuidanceState) -> None:
        """写入/覆盖状态（幂等）。"""
        with self._lock:
            self._states[(state.trainee_id, state.tour_id)] = state

    def size(self) -> int:
        """当前记录条数（测试断言用）。"""
        with self._lock:
            return len(self._states)


# --------------------------------------------------------------------------- 克制门控三函数


def should_show(store: GuidanceStateStore, tour: TourConfig, trainee_id: int) -> bool:
    """克制门控：是否应向该学员自动弹出该导览（规则见模块 docstring 四条口径）。

    - 无记录或未终结（dismissed=False）→ True（新学员续走）；
    - 已终结：仅 ``skip_policy.reshow_on_major`` 允许且已看主版本 < 当前主版本
      时 True（界面改版重显一次）；否则 False（次版本升级/已看过当前主版本不打扰）。
    - 注意：手动入口（页脚「使用指引」）不受本门控限制，由前端直接调用 start()。
    """
    record = store.load(tour.tour_id, trainee_id)
    if record is None or not record.dismissed:
        return True
    if tour.skip_policy.reshow_on_major and record.major < tour.major:
        return True
    return False


def mark_done(
    store: GuidanceStateStore,
    tour: TourConfig,
    trainee_id: int,
    seen_version: str | None = None,
) -> TraineeGuidanceState:
    """标记完成（tour_finished）：记录已看版本（缺省=当前版本）、终态、done=True。

    ``seen_version`` 由前端上报时透传（localStorage 镜像口径）：迟到上报可能
    落后于当前配置版本（改版后旧页面才回调），如实记录所看版本——重显判定
    （should_show）以「已看主版本 < 当前主版本」为准，自然得到「再显一次」结论。
    """
    seen = seen_version if seen_version is not None else tour.version
    parse_tour_version(seen)  # 格式校验：非法已看版本串直接拒收（TourVersionError）
    state = TraineeGuidanceState(
        trainee_id=trainee_id,
        tour_id=tour.tour_id,
        seen_version=seen,
        done=True,
        dismissed=True,
        last_step_seq=len(tour.steps),
        updated_at=_now_iso(),
    )
    store.save(state)
    return state


def mark_skipped(
    store: GuidanceStateStore,
    tour: TourConfig,
    trainee_id: int,
    last_step_seq: int | None = None,
    seen_version: str | None = None,
) -> TraineeGuidanceState:
    """标记跳过（tour_skipped）：记录已看版本、终态、done=False 与中断步骤号。

    跳过与完成同权重落终态（不惩罚性重弹，SR §6.2 onDestroyed 不区分对待）；
    ``last_step_seq`` 为终止时所处步骤（1..n），供流失分析定位引导缺口。
    """
    seen = seen_version if seen_version is not None else tour.version
    parse_tour_version(seen)  # 格式校验：非法已看版本串直接拒收（TourVersionError）
    state = TraineeGuidanceState(
        trainee_id=trainee_id,
        tour_id=tour.tour_id,
        seen_version=seen,
        done=False,
        dismissed=True,
        last_step_seq=last_step_seq,
        updated_at=_now_iso(),
    )
    store.save(state)
    return state


# --------------------------------------------------------------------------- localStorage 契约


def to_localstorage_payload(state: TraineeGuidanceState) -> dict[str, Any]:
    """服务端态 → 前端 localStorage JSON 形态（契约见模块 docstring）。

    键名由 :func:`localstorage_key` 生成，本函数只产出值（JSON 可序列化 dict）；
    ``at`` 为毫秒时间戳（前端 Date.now() 同口径，此处由 updated_at 近似回填）。
    """
    parsed = datetime.fromisoformat(state.updated_at).timestamp()
    return {
        "major": state.major,
        "minor": state.minor,
        "dismissed": state.dismissed,
        "done": state.done,
        "at": int(parsed * 1000),
    }


def from_localstorage_payload(
    trainee_id: int,
    tour_id: str,
    payload: Mapping[str, Any],
) -> TraineeGuidanceState:
    """前端 localStorage JSON 形态 → 服务端态（上报 /tour/state 的解析口径）。

    容错口径：旧版载荷缺 minor/done 字段时按缺省回填（major 缺失视为从未记录，
    抛 ValueError）；键名外的多余字段忽略——前端演进不破坏后端解析。
    """
    if "major" not in payload:
        raise ValueError(f"localStorage 载荷缺少 major 字段：{dict(payload)!r}")
    major = int(payload["major"])
    minor = int(payload.get("minor", 0))
    return TraineeGuidanceState(
        trainee_id=trainee_id,
        tour_id=tour_id,
        seen_version=f"{major}.{minor}",
        done=bool(payload.get("done", False)),
        dismissed=bool(payload.get("dismissed", True)),
        updated_at=_now_iso(),
    )


def localstorage_key(trainee_id: int, tour_id: str) -> str:
    """前端 localStorage 键名（契约）：``guide:seen:{trainee_id}:{tour_id}``。"""
    return f"guide:seen:{trainee_id}:{tour_id}"


def epoch_ms_now() -> int:
    """当前毫秒时间戳（契约文档化用；前端 Date.now() 同口径）。"""
    return int(time.time() * 1000)
