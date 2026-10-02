"""现场故障注入（演练／测试专用）。

真实现场的回读故障来自硬件：指令下去了、变送器回读卡住或丢失，控制器必须能
退回安全位。代码里没法凭空造硬件故障，因此把"注入回读卡死"收敛到这个显式
模块：演练编排与测试通过它模拟泵或阀的回读故障，生产控制路径绝不调用它。

控制器侧只认 :meth:`readback_fault` 与回读偏差，不关心故障是怎么来的，因此
这里的注入不会改动任何控制时序。
"""

from __future__ import annotations

from ..receiver.valve import InletValve
from .pump import SaltPump


def inject_pump_readback_fault(pump: SaltPump, reading_kg_s: float = 0.0) -> float:
    """把一台盐泵的流量回读钉在给定读值上，返回该读值。"""

    return pump.set_readback_fault(reading_kg_s)


def clear_pump_readback_fault(pump: SaltPump) -> None:
    """清除一台盐泵的回读故障。"""

    pump.set_readback_fault(None)


def inject_valve_readback_fault(valve: InletValve, reading_pct: float = 0.0) -> float:
    """把进口阀的开度回读钉在给定读值上，返回该读值。"""

    return valve.set_readback_fault(reading_pct)


def clear_valve_readback_fault(valve: InletValve) -> None:
    """清除进口阀的回读故障。"""

    valve.set_readback_fault(None)
