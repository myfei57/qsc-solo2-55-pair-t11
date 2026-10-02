"""熔盐回路。

冷盐罐、热盐罐、两台盐泵、再循环回流阀与母管组成循环。冷热罐切换走流量斜率
交叉状态机：切换期间两台泵短时重叠，出泵流量按份额退、进泵按份额进，母管总
流量始终夹在基线带内，流量压力不断档；模式（平缓／快速／禁止）由罐况裁决。
手动并泵仍然禁止——两泵对顶会把压力顶到保护值。流量基线由标定给出，联锁
门限从基线推导。
"""

from __future__ import annotations

from .calibrate import CalibrationRecord, FlowCalibrator
from .handoff import TankHandoff
from .handoff_journal import HandoffJournal
from .hydraulic import HydraulicLoop
from .loop import SaltLoop
from .mode import FAST, INHIBITED, SLOW, HandoffMode, select_mode
from .persist import SaltPersistence
from .pump import SaltPump
from .recirc import RecirculationValve
from .record import HandoffRecord
from .snapshot import SaltSnapshot
from .tank import SaltTank

__all__ = [
    "CalibrationRecord",
    "FlowCalibrator",
    "TankHandoff",
    "HandoffJournal",
    "HandoffMode",
    "HandoffRecord",
    "HydraulicLoop",
    "SaltLoop",
    "SaltPersistence",
    "SaltPump",
    "SaltSnapshot",
    "SaltTank",
    "RecirculationValve",
    "SLOW",
    "FAST",
    "INHIBITED",
    "select_mode",
]
