"""盐泵。

泵按升速斜率逼近目标流量：启泵不是瞬间满流，停泵也不是瞬间断流。流量由
``step`` 显式推进。正常操作必须等泵真正停下来之后才允许启另一台，否则两台泵
会在母管上对顶；唯一的例外是切换编排：它在斜率交叉阶段协调两台泵的流量目标，
并通过水力模型的 ``coordinated`` 通道关掉对顶折扣。

回读用于切换编排的部分失败判定：``inject_flow_fault`` 后流量回读返回 ``None``
（传感器掉线），``inject_stuck`` 后指令推进但回读冻结（执行器不响应）。
"""

from __future__ import annotations

from ..config import SaltLimits
from ..errors import InterlockError


class SaltPump:
    """一台盐泵。"""

    def __init__(self, name: str, limits: SaltLimits, target_flow_kg_s: float | None = None) -> None:
        self._name = name
        self._limits = limits
        self._target = float(limits.flow_baseline_kg_s if target_flow_kg_s is None else target_flow_kg_s)
        self._flow = 0.0
        self._demand = False
        self._starts = 0
        self._stops = 0
        self._fault = False
        self._stuck = False
        self._last_readback = 0.0

    @property
    def name(self) -> str:
        return self._name

    @property
    def limits(self) -> SaltLimits:
        return self._limits

    def start(self, target_flow_kg_s: float | None = None) -> float:
        """启泵，返回启泵后的目标流量；可显式指定本次启动的流量目标。"""

        if self._demand:
            raise InterlockError(f"{self._name} 已在运行", component="salt")
        if target_flow_kg_s is not None:
            self.set_target_flow(target_flow_kg_s)
        self._demand = True
        self._starts += 1
        return self._target

    def stop(self) -> float:
        """停泵，返回停泵后的目标流量（0）。"""

        if not self._demand:
            raise InterlockError(f"{self._name} 未在运行", component="salt")
        self._demand = False
        self._stops += 1
        return 0.0

    def set_target_flow(self, kg_s: float) -> float:
        """运行中改变流量目标（供切换编排协调份额）。"""

        target = max(0.0, float(kg_s))
        self._target = target
        return self._target

    def step(self, dt: float) -> float:
        """按升速斜率推进一个步长，返回推进后的流量。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        target = self._target if self._demand else 0.0
        span = self._limits.pump_ramp_kg_s2 * dt
        gap = target - self._flow
        if abs(gap) <= span:
            self._flow = target
        else:
            self._flow += span if gap > 0 else -span
        if not self._stuck:
            self._last_readback = self._flow
        return self._flow

    def settle(self, dt: float = 1.0, max_steps: int = 600) -> float:
        """按固定步长推进到目标流量，返回流量。"""

        for _ in range(max_steps):
            self.step(dt)
            if self.at_target():
                break
        return self._flow

    def flow_kg_s(self) -> float:
        """返回当前流量（指令侧真值）。"""

        return self._flow

    def readback_flow_kg_s(self) -> float | None:
        """返回流量回读；传感器掉线时返回 ``None``。"""

        if self._fault:
            return None
        return self._last_readback

    def inject_flow_fault(self, fault: bool = True) -> None:
        """注入／消除流量传感器掉线故障（供测试与故障演练）。"""

        self._fault = bool(fault)

    def inject_stuck(self, stuck: bool = True) -> None:
        """注入／消除执行器卡死：指令继续推进但回读冻结。"""

        self._stuck = bool(stuck)
        if not self._stuck:
            self._last_readback = self._flow

    def target_flow_kg_s(self) -> float:
        """返回目标流量。"""

        return self._target if self._demand else 0.0

    def running(self) -> bool:
        """泵是否处于启动状态。"""

        return self._demand

    def idle(self) -> bool:
        """泵是否已经完全停稳。"""

        return not self._demand and self._flow <= 1e-9

    def at_target(self) -> bool:
        """流量是否已经到达目标。"""

        return abs(self._flow - self.target_flow_kg_s()) < 1e-9

    def starts(self) -> int:
        """返回累计启泵次数。"""

        return self._starts

    def stops(self) -> int:
        """返回累计停泵次数。"""

        return self._stops

    def as_dict(self) -> dict:
        return {
            "name": self._name,
            "running": self._demand,
            "flow_kg_s": round(self._flow, 3),
            "readback_flow_kg_s": None if self._fault else round(self._last_readback, 3),
            "target_flow_kg_s": round(self.target_flow_kg_s(), 3),
            "sensor_fault": self._fault,
            "actuator_stuck": self._stuck,
            "starts": self._starts,
            "stops": self._stops,
        }
