"""切换模式选择：按罐况定，不按操作员意愿定。

切换过渡只有一种控制方式——流量斜率交叉，但斜率快慢和看门狗窗口要服从罐况。
把冷罐"还能抽多久"与热罐"还能灌多久"折算成安全对灌时长 ``t_avail``：

* ``t_avail`` 足够走完平缓窗口（长窗口、缓斜率）→ ``slow``，平缓过渡；
* 只够走完快速窗口（短窗口、陡斜率）→ ``fast``，快速切换保安全；
* 连快速窗口都不够，或罐已经越限 → ``inhibited``，禁止硬切，必须走别的走法
  （再循环旁路保持、降负荷或保持原状）。

高负荷只影响记录与告警（压力更脆），不改变模式归属——模式严格按罐况。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SaltLimits
from .tank import SaltTank

SLOW = "slow"
FAST = "fast"
INHIBITED = "inhibited"


@dataclass(frozen=True)
class HandoffMode:
    """一次切换的模式裁决结果。"""

    mode: str
    share_ramp_per_s: float
    window_s: float
    available_s: float
    cold_drawable_kg: float
    hot_headroom_kg: float
    reasons: tuple[str, ...]

    @property
    def inhibited(self) -> bool:
        return self.mode == INHIBITED

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "share_ramp_per_s": round(self.share_ramp_per_s, 4),
            "window_s": round(self.window_s, 3),
            "available_s": round(self.available_s, 3),
            "cold_drawable_kg": round(self.cold_drawable_kg, 1),
            "hot_headroom_kg": round(self.hot_headroom_kg, 1),
            "reasons": list(self.reasons),
        }


def select_mode(
    limits: SaltLimits,
    cold_tank: SaltTank,
    hot_tank: SaltTank,
    required_flow_kg_s: float,
) -> HandoffMode:
    """按罐位余量裁决切换模式。

    冷罐可抽量取下限加保护带之上的存量；热罐可灌量取上限减保护带之下的余量。
    两者除以切换所需总流量，得到各自允许的对灌秒数，取小者参与比较。
    """

    flow = max(1e-6, float(required_flow_kg_s))
    guard = limits.handoff_level_guard_pct / 100.0
    cold_floor_kg = cold_tank.capacity_kg * (limits.cold_level_min_pct / 100.0 + guard)
    hot_ceiling_kg = hot_tank.capacity_kg * (limits.hot_level_max_pct / 100.0 - guard)

    cold_drawable = max(0.0, cold_tank.level_kg() - cold_floor_kg)
    hot_headroom = max(0.0, hot_ceiling_kg - hot_tank.level_kg())

    t_cold = cold_drawable / flow
    t_hot = hot_headroom / flow
    t_avail = min(t_cold, t_hot)

    reasons: list[str] = []
    if cold_tank.at_min():
        reasons.append("cold_tank_at_min")
    if hot_tank.at_max():
        reasons.append("hot_tank_at_max")
    if t_cold < t_hot:
        reasons.append("cold_tank_limiting")
    else:
        reasons.append("hot_tank_limiting")

    if reasons and ("cold_tank_at_min" in reasons or "hot_tank_at_max" in reasons):
        mode, ramp, window = INHIBITED, 0.0, 0.0
    elif t_avail >= limits.handoff_slow_window_s:
        mode, ramp, window = SLOW, limits.handoff_share_ramp_per_s, limits.handoff_slow_window_s
    elif t_avail >= limits.handoff_fast_window_s:
        mode, ramp, window = FAST, limits.handoff_fast_share_ramp_per_s, limits.handoff_fast_window_s
    else:
        mode, ramp, window = INHIBITED, 0.0, 0.0
        reasons.append("margin_shorter_than_fast_window")

    return HandoffMode(
        mode=mode,
        share_ramp_per_s=ramp,
        window_s=window,
        available_s=t_avail,
        cold_drawable_kg=cold_drawable,
        hot_headroom_kg=hot_headroom,
        reasons=tuple(reasons),
    )
