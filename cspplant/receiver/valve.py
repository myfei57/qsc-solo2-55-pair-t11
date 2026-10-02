"""吸热器进口调节阀。

阀门按行程速度控制开度，从 0% 到 100% 需要若干步。目标值一律夹到 0–100，
并且只能由 ``set_target`` 改动：上游谁想改开度都得走这个方法，避免两处代码
各写一个字段导致执行器看到两个目标。
"""

from __future__ import annotations

from ..config import ReceiverLimits


class InletValve:
    """进口调节阀。"""

    def __init__(self, limits: ReceiverLimits, position_pct: float = 0.0) -> None:
        self._limits = limits
        self._position = max(0.0, min(100.0, float(position_pct)))
        self._target = self._position
        self._moves = 0
        self._readback_fault: float | None = None

    @property
    def limits(self) -> ReceiverLimits:
        return self._limits

    def set_target(self, pct: float) -> float:
        """下达开度目标，返回生效的目标值。"""

        self._target = max(0.0, min(100.0, float(pct)))
        return self._target

    def step(self, dt: float) -> float:
        """按行程速度推进一个步长，返回推进后的开度。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        span = self._limits.valve_travel_pct_s * dt
        gap = self._target - self._position
        if abs(gap) <= span:
            self._position = self._target
        else:
            self._position += span if gap > 0 else -span
            self._moves += 1
        return self._position

    def position_pct(self) -> float:
        """返回当前开度回读（故障时上报卡住的读值）。"""

        if self._readback_fault is not None:
            return self._readback_fault
        return self._position

    def target_pct(self) -> float:
        """返回当前目标开度。"""

        return self._target

    def at_target(self) -> bool:
        """回读开度是否已经停在目标开度。"""

        return abs(self.position_pct() - self._target) < 1e-9

    def is_open(self, threshold_pct: float = 95.0) -> bool:
        """开度回读是否达到给定阈值。"""

        return self.position_pct() >= threshold_pct

    def set_readback_fault(self, reading_pct: float | None = 0.0) -> float | None:
        """注入／清除回读故障；返回注入后的固定读值（``None`` 表示已清除）。"""

        self._readback_fault = None if reading_pct is None else max(0.0, min(100.0, float(reading_pct)))
        return self._readback_fault

    def readback_fault(self) -> bool:
        """阀门是否处于回读故障状态。"""

        return self._readback_fault is not None

    def moves(self) -> int:
        """返回累计被限速的行程步数。"""

        return self._moves
