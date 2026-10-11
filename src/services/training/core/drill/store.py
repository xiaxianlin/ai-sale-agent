# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练存储层（FR-TR-003，SR-TR-003 §3/§4：演练记录与校准台账的存储接缝）。

- :class:`DrillStore`——存储协议（鸭子类型注入接缝，生产替换 PG 形态：
  exercise 表共用 FR-TR-002、drill_case / score_calibration 见 SR §4 DDL）；
- :class:`InMemoryDrillStore`——进程内线程安全实现（demo/测试缺省可用）：
  结果按 id 存取 + (trainee_id, request_id) 幂等索引 + 学员隔离清单 + 校准台账追加。

取号口径：结果 ``RES-{seq:04d}``、校准 ``CAL-{seq:04d}``，进程内全局唯一递增
（生产由数据库序列承接）；缺失键统一返回 None（服务层转 TR-2003）。
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # 仅类型检查期导入（避免与 service 模块运行期循环依赖）
    from services.training.core.drill.service import CalibrationSample, DrillResult

#: 结果 id 前缀与位宽（RES-0001 起递增）
RESULT_ID_PREFIX: str = "RES"
#: 校准采样 id 前缀与位宽（CAL-0001 起递增）
CALIBRATION_ID_PREFIX: str = "CAL"
_ID_WIDTH: int = 4


class DrillStore(Protocol):
    """演练存储协议（结果 + 校准台账；案例库由 DrillCaseBank 承担，不在此层）。"""

    def next_result_id(self) -> str:
        """分配下一个结果 id（线程安全，全局唯一递增）。"""
        ...

    def next_calibration_id(self) -> str:
        """分配下一个校准采样 id（线程安全，全局唯一递增）。"""
        ...

    def save_result(self, result: DrillResult) -> None:
        """保存/覆盖评分结果。"""
        ...

    def get_result(self, result_id: str) -> DrillResult | None:
        """按 id 取结果；缺失返回 None。"""
        ...

    def find_result_by_request(self, trainee_id: int, request_id: str) -> DrillResult | None:
        """按 (trainee_id, request_id) 幂等索引查结果（会话隔离：键含学员）。"""
        ...

    def list_results(self, trainee_id: int) -> list[DrillResult]:
        """列举指定学员的演练结果（仅本人数据）。"""
        ...

    def save_calibration(self, sample: CalibrationSample) -> None:
        """追加校准采样入台账。"""
        ...

    def list_calibrations(self) -> list[CalibrationSample]:
        """全量校准台账（按写入序）。"""
        ...


class InMemoryDrillStore:
    """进程内内存实现（demo/测试）：锁保护取号与写入，缺失键返回 None。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[str, Any] = {}
        self._by_request: dict[tuple[int, str], str] = {}
        self._calibrations: list[Any] = []
        self._result_seq = 0
        self._calibration_seq = 0

    # ── 取号 ────────────────────────────────────────────────────────────────
    def next_result_id(self) -> str:
        with self._lock:
            self._result_seq += 1
            return f"{RESULT_ID_PREFIX}-{self._result_seq:0{_ID_WIDTH}d}"

    def next_calibration_id(self) -> str:
        with self._lock:
            self._calibration_seq += 1
            return f"{CALIBRATION_ID_PREFIX}-{self._calibration_seq:0{_ID_WIDTH}d}"

    # ── 结果 ────────────────────────────────────────────────────────────────
    def save_result(self, result: DrillResult) -> None:
        with self._lock:
            self._results[result.result_id] = result
            self._by_request[(result.trainee_id, result.request_id)] = result.result_id

    def get_result(self, result_id: str) -> DrillResult | None:
        with self._lock:
            return self._results.get(result_id)

    def find_result_by_request(self, trainee_id: int, request_id: str) -> DrillResult | None:
        with self._lock:
            result_id = self._by_request.get((trainee_id, request_id))
            return self._results.get(result_id) if result_id is not None else None

    def list_results(self, trainee_id: int) -> list[DrillResult]:
        with self._lock:
            ordered = sorted(self._results.values(), key=lambda r: r.result_id)
        return [r for r in ordered if r.trainee_id == trainee_id]

    # ── 校准台账 ────────────────────────────────────────────────────────────
    def save_calibration(self, sample: CalibrationSample) -> None:
        with self._lock:
            self._calibrations.append(sample)

    def list_calibrations(self) -> list[CalibrationSample]:
        with self._lock:
            return list(self._calibrations)


__all__ = ["DrillStore", "InMemoryDrillStore"]
