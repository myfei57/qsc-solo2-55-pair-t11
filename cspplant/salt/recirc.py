"""再循环回流阀。

切换的极端罐况（热罐将满、冷罐见底）下不能硬切：回流阀打开后母管盐量经再
循环回路保持流动，吸热器不致断流，同时停止冷热罐之间的倒灌，为降负荷或等待
罐位恢复争取时间。阀门按行程速度动作，安全位是"开"（失气／失电打开，保住
母管流量）。

回读与泵一致：``inject_position_fault`` 模拟位置传感器掉线（回读 ``None``），
``inject_stuck`` 模拟执行器卡死（指令推进、回读冻结）。
"""

from __future__ import annotations

from ..config import SaltLimits

OPEN = 100.0
CLOSED = 0.0


class RecirculationValve:
    """再循环回流调节阀，安全位为全开。"""

    def __init__(self, limits: SaltLimits, position_pct: float = OPEN) -> None:
        self._limits = limits
        self._position = max(0.0, min(100.0, float(position_pct)))
        self._target = self._position
        self._readback = self._position
        self._moves = 0
        self._fault = False
        self._stuck = False

    @property
    def limits(self) -> SaltLimits:
        return self._limits

    def set_target(self, pct: float) -> float:
        """下达开度目标，返回生效的目标值。"""

        self._target = max(0.0, min(100.0, float(pct)))
        return self._target

    def open(self) -> float:
        """驱向安全位（全开）。"""

        return self.set_target(OPEN)

    def close(self) -> float:
        """下达全关目标（切换前隔离再循环回路）。"""

        return self.set_target(CLOSED)

    def step(self, dt: float) -> float:
        """按行程速度推进一个步长，返回推进后的开度。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        span = self._limits.recirc_valve_travel_pct_s * dt
        gap = self._target - self._position
        if abs(gap) <= span:
            self._position = self._target
        else:
            self._position += span if gap > 0 else -span
            self._moves += 1
        if not self._stuck:
            self._readback = self._position
        return self._position

    def position_pct(self) -> float:
        """返回当前指令开度。"""

        return self._position

    def readback_pct(self) -> float | None:
        """返回位置回读；传感器掉线时返回 ``None``。"""

        if self._fault:
            return None
        return self._readback

    def target_pct(self) -> float:
        """返回当前目标开度。"""

        return self._target

    def at_target(self, tolerance_pct: float = 0.5) -> bool:
        """回读是否已经停在目标开度（以回读为准）。"""

        readback = self.readback_pct()
        return readback is not None and abs(readback - self._target) <= tolerance_pct

    def is_open(self, threshold_pct: float = 95.0) -> bool:
        """回读是否达到全开阈值。"""

        readback = self.readback_pct()
        return readback is not None and readback >= threshold_pct

    def is_closed(self, threshold_pct: float = 5.0) -> bool:
        """回读是否达到全关阈值。"""

        readback = self.readback_pct()
        return readback is not None and readback <= threshold_pct

    def inject_position_fault(self, fault: bool = True) -> None:
        """注入／消除位置传感器掉线故障。"""

        self._fault = bool(fault)

    def inject_stuck(self, stuck: bool = True) -> None:
        """注入／消除执行器卡死。"""

        self._stuck = bool(stuck)
        if not self._stuck:
            self._readback = self._position

    def moves(self) -> int:
        """返回累计被限速的行程步数。"""

        return self._moves

    def as_dict(self) -> dict:
        return {
            "name": "recirculation-valve",
            "position_pct": round(self._position, 3),
            "readback_pct": None if self._fault else round(self._readback, 3),
            "target_pct": round(self._target, 3),
            "safe_position": "open",
            "sensor_fault": self._fault,
            "actuator_stuck": self._stuck,
            "moves": self._moves,
        }
