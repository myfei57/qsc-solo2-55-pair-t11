"""切换记录与台账。

每次切换（含被拒绝、退回安全位、走再循环旁路）都生成同构的一条
:class:`HandoffRecord``：控制台展示、审计流水、切换台账三处读的是这同一份
记录，杜绝"界面说成功、台账说失败"的口径分叉。过冲字段（流量／压力相对基线
的最大上下偏离量）逐次留痕，方便回头比较哪次切换最平。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

INCOMING = "cold_to_hot"

# 终态。
COMPLETED = "completed"
ROLLED_BACK = "rolled_back"
SAFE_HOLD = "safe_hold"
REJECTED = "rejected"


@dataclass
class HandoffRecord:
    """一次切换的完整记录。"""

    handoff_id: str
    ts: float
    direction: str = INCOMING
    mode: str = ""
    state: str = ""
    outcome: str = ""
    ok: bool = False
    duration_s: float = 0.0
    window_s: float = 0.0
    available_s: float = 0.0
    load_mw: float = 0.0
    flow_baseline_kg_s: float = 0.0
    flow_min_kg_s: float = 0.0
    flow_max_kg_s: float = 0.0
    flow_overshoot_kg_s: float = 0.0
    flow_undershoot_kg_s: float = 0.0
    pressure_target_mpa: float = 0.0
    pressure_min_mpa: float = 0.0
    pressure_max_mpa: float = 0.0
    pressure_overshoot_mpa: float = 0.0
    pressure_undershoot_mpa: float = 0.0
    peak_pressure_mpa: float = 0.0
    max_pumps_running: int = 0
    incoming_final_share: float = 0.0
    violations: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        def r(value: float, ndigits: int = 3) -> float:
            return round(float(value), ndigits)

        return {
            "handoff_id": self.handoff_id,
            "ts": r(self.ts),
            "direction": self.direction,
            "mode": self.mode,
            "state": self.state,
            "outcome": self.outcome,
            "ok": bool(self.ok),
            "duration_s": r(self.duration_s),
            "window_s": r(self.window_s),
            "available_s": r(self.available_s),
            "load_mw": r(self.load_mw),
            "flow_baseline_kg_s": r(self.flow_baseline_kg_s),
            "flow_min_kg_s": r(self.flow_min_kg_s),
            "flow_max_kg_s": r(self.flow_max_kg_s),
            "flow_overshoot_kg_s": r(self.flow_overshoot_kg_s),
            "flow_undershoot_kg_s": r(self.flow_undershoot_kg_s),
            "pressure_target_mpa": r(self.pressure_target_mpa),
            "pressure_min_mpa": r(self.pressure_min_mpa),
            "pressure_max_mpa": r(self.pressure_max_mpa),
            "pressure_overshoot_mpa": r(self.pressure_overshoot_mpa),
            "pressure_undershoot_mpa": r(self.pressure_undershoot_mpa),
            "peak_pressure_mpa": r(self.peak_pressure_mpa),
            "max_pumps_running": int(self.max_pumps_running),
            "incoming_final_share": r(self.incoming_final_share, 4),
            "violations": list(self.violations),
            "steps": list(self.steps),
            "trace": list(self.trace),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HandoffRecord":
        """从台账条目还原记录。"""

        record = cls(handoff_id=str(raw.get("handoff_id", "")), ts=float(raw.get("ts", 0.0)))
        for key in (
            "direction",
            "mode",
            "state",
            "outcome",
            "duration_s",
            "window_s",
            "available_s",
            "load_mw",
            "flow_baseline_kg_s",
            "flow_min_kg_s",
            "flow_max_kg_s",
            "flow_overshoot_kg_s",
            "flow_undershoot_kg_s",
            "pressure_target_mpa",
            "pressure_min_mpa",
            "pressure_max_mpa",
            "pressure_overshoot_mpa",
            "pressure_undershoot_mpa",
            "peak_pressure_mpa",
            "incoming_final_share",
        ):
            if key in raw:
                setattr(record, key, raw[key])
        record.ok = bool(raw.get("ok", False))
        record.max_pumps_running = int(raw.get("max_pumps_running", 0))
        record.violations = list(raw.get("violations", []))
        record.steps = list(raw.get("steps", []))
        record.trace = list(raw.get("trace", []))
        return record
