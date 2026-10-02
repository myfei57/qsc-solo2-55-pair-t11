"""冷热罐无断档切换顺序。

过渡基准**定死按流量斜率控制**：切换窗口内两泵处于受控的负荷交接状态
（coordinated load-sharing），冷泵按配置斜率把流量让下来、热泵按同一斜率把
流量接上去，两条指令之和始终锚定基线流量——任何一步母管净流量都不得低于
``handoff_flow_floor_kg_s``。时长窗口（``handoff_max_window_s`` /
``handoff_fast_window_s``）只做看门狗上限，绝不作为控制目标。

罐况决定档位：热罐已到上限或冷罐已到下限属于硬联锁，直接拒绝，要求改走别的
走法（先向用户/调度报出）；任一侧余量小于 ``handoff_extreme_margin_pct`` 时
走快速档（更陡斜率、更短看门狗），尽快结束让盐窗口；其余情况走平滑档。

时序写死，不允许乱序：预检 → 启热泵并列 → 协同让负荷（冷降热升）→ 冷泵退出
→ 确认热流。泵或阀回读失败、超压、流量塌底、看门狗超时都算部分失败，编排器
按现场情况退回安全位：好泵接管（退回冷泵或继续由热泵承载）或整机隔离。

每一次切换（含失败）都把流量／压力过冲、最低点、时长、档位与退回位置记成
:class:`HandoffReport`，既进返回报告也落切换流水，供控制台展示与事后逐回比对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import SaltLimits
from ..errors import InterlockError
from ..receiver.header import SaltHeader
from ..receiver.valve import InletValve
from .hydraulic import HydraulicLoop
from .pump import SaltPump
from .tank import SaltTank

# 切换各阶段最多推进的步数。
MAX_STEPS = 1800

# 过渡控制基准，定死为流量斜率；时长只做看门狗。
TRANSITION_BASIS = "flow_slope"

MODE_SMOOTH = "smooth"
MODE_FAST = "fast"

# 退回安全位的三种落点。
SAFE_HOT = "hot_pump_carrying"
SAFE_COLD = "cold_pump_carrying"
SAFE_ISOLATED = "tripped_isolated"
SAFE_NONE = "none"


@dataclass(frozen=True)
class HandoffReport:
    """一次切换的结果。"""

    ok: bool
    mode: str = MODE_SMOOTH
    basis: str = TRANSITION_BASIS
    duration_s: float = 0.0
    steps: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    aborted: bool = False
    safe_position: str = SAFE_NONE
    baseline_kg_s: float = 0.0
    peak_pressure_mpa: float = 0.0
    pressure_overshoot_mpa: float = 0.0
    min_flow_kg_s: float = 0.0
    max_flow_kg_s: float = 0.0
    flow_dip_kg_s: float = 0.0
    flow_overshoot_kg_s: float = 0.0
    max_pumps_running: int = 0
    transfer_kg: float = 0.0
    readback_failures: list[str] = field(default_factory=list)
    cold_level_pct: float = 0.0
    hot_level_pct: float = 0.0
    trace: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "basis": self.basis,
            "duration_s": round(self.duration_s, 3),
            "steps": self.steps,
            "violations": self.violations,
            "aborted": self.aborted,
            "safe_position": self.safe_position,
            "baseline_kg_s": round(self.baseline_kg_s, 3),
            "peak_pressure_mpa": round(self.peak_pressure_mpa, 3),
            "pressure_overshoot_mpa": round(self.pressure_overshoot_mpa, 3),
            "min_flow_kg_s": round(self.min_flow_kg_s, 3),
            "max_flow_kg_s": round(self.max_flow_kg_s, 3),
            "flow_dip_kg_s": round(self.flow_dip_kg_s, 3),
            "flow_overshoot_kg_s": round(self.flow_overshoot_kg_s, 3),
            "max_pumps_running": self.max_pumps_running,
            "transfer_kg": round(self.transfer_kg, 2),
            "readback_failures": self.readback_failures,
            "cold_level_pct": round(self.cold_level_pct, 3),
            "hot_level_pct": round(self.hot_level_pct, 3),
            "trace": self.trace,
        }


IsolateFn = Callable[[str], None]


class TankHandoff:
    """按流量斜率执行冷盐泵 → 热盐泵的无断档切换。"""

    def __init__(self, limits: SaltLimits, isolate: IsolateFn | None = None) -> None:
        self._limits = limits
        self._isolate = isolate
        self._changeovers = 0
        self._records: list[dict] = []

    @property
    def limits(self) -> SaltLimits:
        return self._limits

    def records(self, limit: int | None = None) -> list[dict]:
        """返回已执行的切换记录（最新的在末尾；``limit`` 取最近若干条）。"""

        if limit is None:
            return list(self._records)
        return list(self._records[-max(0, limit) :])

    def seed_records(self, records: list[dict]) -> None:
        """从落盘流水回放历史记录（仅启动恢复时调用）。"""

        self._records = [dict(record) for record in records]
        self._changeovers = sum(1 for record in self._records if record.get("ok"))

    def changeover(
        self,
        cold_pump: SaltPump,
        hot_pump: SaltPump,
        cold_tank: SaltTank,
        hot_tank: SaltTank,
        header: SaltHeader,
        hydraulic: HydraulicLoop,
        dt: float = 1.0,
        valve: InletValve | None = None,
        op_id: str = "",
    ) -> HandoffReport:
        """执行一次冷盐泵 → 热盐泵的无断档切换。"""

        ctx = _Context(self._limits, cold_pump, hot_pump, cold_tank, hot_tank, header, hydraulic, dt)

        violations = self._preflight(ctx, valve)
        if violations:
            report = ctx.report(violations=violations, safe_position=SAFE_NONE)
            self._store(report, op_id)
            return report

        ctx.steps.append(
            {
                "component": "salt",
                "action": "handoff_preflight",
                "detail": {
                    "mode": ctx.mode,
                    "basis": TRANSITION_BASIS,
                    "cold_margin_pct": round(ctx.cold_margin, 3),
                    "hot_margin_pct": round(ctx.hot_margin, 3),
                    "slope_kg_s2": round(ctx.slope, 3),
                    "window_s": round(ctx.window_s, 3),
                },
            }
        )

        # 时序第一步：启热泵并列（此刻它的流量仍为 0，不会与冷泵对顶）。
        hot_pump.start()
        ctx.max_running = 2
        ctx.steps.append(
            {
                "component": "salt",
                "action": "start_hot_pump",
                "detail": {"target_flow_kg_s": round(ctx.baseline, 3)},
            }
        )

        # 时序第二步：按流量斜率协同让负荷，冷降热升、总和锚定基线。
        abort = self._coordinated_transfer(ctx, valve)
        if abort is not None:
            report = self._fail_safe(ctx, abort, valve)
            self._store(report, op_id)
            return report

        # 时序第三步：冷泵流量已让到 0，此刻才允许停冷泵。
        cold_pump.stop()
        ctx.steps.append(
            {
                "component": "salt",
                "action": "stop_cold_pump",
                "detail": {"cold_flow_kg_s": round(cold_pump.flow_kg_s(), 3)},
            }
        )

        # 时序第四步：热泵独自把流量顶到基线并确认。
        abort = self._confirm_hot(ctx)
        if abort is not None:
            report = self._fail_safe(ctx, abort, valve)
            self._store(report, op_id)
            return report

        ctx.steps.append(
            {
                "component": "salt",
                "action": "confirm_hot_flow",
                "detail": {
                    "net_flow_kg_s": round(hydraulic.net_flow_kg_s(), 3),
                    "target_flow_kg_s": round(hot_pump.target_flow_kg_s(), 3),
                },
            }
        )

        report = ctx.report(violations=[], safe_position=SAFE_HOT)
        if report.violations:
            report = self._fail_safe(ctx, report.violations[0], valve)
        else:
            self._changeovers += 1
        self._store(report, op_id)
        return report

    def changeovers(self) -> int:
        """返回累计成功切换次数。"""

        return self._changeovers

    # ------------------------------------------------------------------

    def _preflight(self, ctx: "_Context", valve: InletValve | None) -> list[str]:
        """切换前的硬联锁与罐况档位判定。"""

        violations: list[str] = []
        if ctx.hot_tank.headroom_kg() <= 0.0:
            violations.append("hot_tank_full")
        if ctx.cold_tank.at_min():
            violations.append("cold_tank_empty")
        if ctx.hot_pump.running():
            violations.append("hot_pump_already_running")
        if not ctx.cold_pump.running():
            violations.append("cold_pump_not_running")
        if ctx.cold_pump.readback_fault():
            violations.append("cold_pump_readback_fault")
        if ctx.hot_pump.readback_fault():
            violations.append("hot_pump_readback_fault")
        if valve is not None and valve.readback_fault():
            violations.append("inlet_valve_readback_fault")
        if violations:
            return violations

        if valve is not None and not valve.is_open():
            return ["inlet_valve_not_open"]

        # 罐况定档：任一罐接近极限就快速切换，尽快结束让盐窗口。
        ctx.cold_margin = ctx.cold_tank.drawable_pct()
        ctx.hot_margin = ctx.hot_tank.headroom_pct()
        if min(ctx.cold_margin, ctx.hot_margin) < self._limits.handoff_extreme_margin_pct:
            ctx.mode = MODE_FAST
            ctx.slope = self._limits.handoff_fast_slope_kg_s2
            ctx.window_s = self._limits.handoff_fast_window_s
        return []

    def _coordinated_transfer(self, ctx: "_Context", valve: InletValve | None) -> str | None:
        """冷泵斜坡让负荷、热泵斜坡接负荷；返回违约代码，``None`` 表示正常。"""

        elapsed = 0.0
        while True:
            ctx.advance += ctx.slope * ctx.dt
            if ctx.advance >= ctx.baseline:
                ctx.advance = ctx.baseline
            cold_target = ctx.baseline - ctx.advance
            hot_target = ctx.advance

            ctx.cold_pump.step_toward(cold_target, ctx.slope, ctx.dt)
            ctx.hot_pump.step_toward(hot_target, ctx.slope, ctx.dt)
            elapsed += ctx.dt

            # 回读核对：指令推进了、读值没跟上，即部分失败。
            cold_code = self._pump_readback(ctx, ctx.cold_pump, "cold_pump")
            hot_code = self._pump_readback(ctx, ctx.hot_pump, "hot_pump")
            if cold_code:
                return cold_code
            if hot_code:
                return hot_code
            if valve is not None and (valve.readback_fault() or not valve.is_open()):
                ctx.readback_failures.append("inlet_valve")
                return "inlet_valve_readback_fault"

            flow = ctx.hydraulic.collect([ctx.cold_pump, ctx.hot_pump], coordinated=True)
            pumps = sum(1 for pump in (ctx.cold_pump, ctx.hot_pump) if pump.running())
            ctx.max_running = max(ctx.max_running, pumps)
            pressure = ctx.header.update(flow, pumps, ctx.dt, coordinated=True)
            ctx.observe(flow, pressure, elapsed)
            tank_block = self._move_salt(ctx, flow)
            if tank_block:
                return tank_block

            if flow < self._limits.handoff_flow_floor_kg_s:
                return "flow_floor_breached"
            if pressure >= self._limits.header_pressure_target_mpa + self._limits.handoff_pressure_overshoot_mpa:
                return "pressure_overshot"
            if elapsed > ctx.window_s:
                return "handoff_window_exceeded"
            if ctx.advance >= ctx.baseline and ctx.hot_pump.commanded_flow_kg_s() >= ctx.baseline - 1e-9:
                ctx.duration_s = elapsed
                return None

    def _confirm_hot(self, ctx: "_Context") -> str | None:
        """冷泵退出后，热泵独自升满并确认流量。"""

        for _ in range(MAX_STEPS):
            flow = ctx.hydraulic.update([ctx.cold_pump, ctx.hot_pump], ctx.dt)
            pumps = sum(1 for pump in (ctx.cold_pump, ctx.hot_pump) if pump.running())
            pressure = ctx.header.update(flow, pumps, ctx.dt)
            ctx.observe(flow, pressure, ctx.duration_s)
            ctx.duration_s += ctx.dt
            self._move_salt(ctx, flow)
            code = self._pump_readback(ctx, ctx.hot_pump, "hot_pump")
            if code:
                return code
            if flow < self._limits.handoff_flow_floor_kg_s:
                return "flow_floor_breached"
            if ctx.header.overpressure():
                return "header_overpressure"
            if ctx.duration_s > ctx.window_s:
                return "handoff_window_exceeded"
            if ctx.hot_pump.at_target() and flow >= ctx.baseline - self._limits.handoff_readback_tolerance_kg_s:
                return None
        return "hot_flow_not_established"

    def _pump_readback(self, ctx: "_Context", pump: SaltPump, device: str) -> str | None:
        """核对泵内部实际流量与回读值；偏差超公差即回读失败。"""

        if pump.readback_fault() or (
            pump.running()
            and abs(pump.commanded_flow_kg_s() - pump.flow_kg_s())
            > self._limits.handoff_readback_tolerance_kg_s
        ):
            ctx.readback_failures.append(device)
            return f"{device}_readback_fault"
        return None

    def _move_salt(self, ctx: "_Context", flow: float) -> str | None:
        """按净流量记账冷→热的盐量；罐位顶到边界即返回违约代码。"""

        if flow <= 0.0:
            return None
        moved = min(flow * ctx.dt, ctx.cold_tank.level_kg())
        try:
            drawn = ctx.cold_tank.draw(moved)
            ctx.hot_tank.fill(drawn)
            ctx.transfer_kg += drawn
        except InterlockError:
            if ctx.cold_tank.at_min():
                return "cold_tank_limit_reached"
            return "hot_tank_limit_reached"
        return None

    def _fail_safe(self, ctx: "_Context", reason: str, valve: InletValve | None) -> HandoffReport:
        """部分失败后退回安全位，并把退回动作记进时序。"""

        violations = list(ctx.violation_seed(reason))
        cold, hot = ctx.cold_pump, ctx.hot_pump
        safe = SAFE_ISOLATED

        if reason == "cold_pump_readback_fault":
            # 冷泵不可信：切掉冷泵，热泵继续顶上（切换方向上的安全落点）。
            self._stop_quietly(cold)
            safe = self._carry(hot, ctx)
        elif reason == "hot_pump_readback_fault":
            # 热泵不可信：切掉热泵，冷泵退回基线继续承载，整次切换作废。
            self._stop_quietly(hot)
            safe = self._carry(cold, ctx)
        elif reason == "cold_tank_limit_reached":
            # 冷罐已见底：冷泵无盐可抽，只能由热泵接管（若还接不住就隔离）。
            self._stop_quietly(cold)
            safe = self._carry(hot, ctx)
        elif reason == "hot_tank_limit_reached":
            # 热罐已到上限：再灌就溢流，整机隔离止流。
            self._isolate_all(ctx, valve)
            safe = SAFE_ISOLATED
        elif reason in ("pressure_overshot", "header_overpressure", "inlet_valve_readback_fault"):
            # 超压或阀位不可信：整机隔离，停两台泵、关母管、进口阀归零。
            self._isolate_all(ctx, valve)
            safe = SAFE_ISOLATED
        else:
            # 流量塌底 / 看门狗超时：热泵健康就让它接管，否则整机隔离。
            if hot.running() and not hot.readback_fault():
                self._stop_quietly(cold)
                safe = self._carry(hot, ctx)
                if safe != SAFE_HOT:
                    self._isolate_all(ctx, valve)
                    safe = SAFE_ISOLATED
            else:
                self._isolate_all(ctx, valve)
                safe = SAFE_ISOLATED

        ctx.steps.append(
            {
                "component": "salt",
                "action": "handoff_failsafe",
                "detail": {"reason": reason, "safe_position": safe},
            }
        )
        return ctx.report(violations=violations, aborted=True, safe_position=safe)

    def _stop_quietly(self, pump: SaltPump) -> None:
        """停泵但不因"本来就没在运行"再抛异常。"""

        if pump.running():
            pump.stop()

    def _carry(self, pump: SaltPump, ctx: "_Context") -> str:
        """让一台健康泵尽快独自承载基线流量。

        恢复段用快速斜率（而非泵的常规升速斜率），把单泵失流造成的缺口压到
        最短；恢复期不套用切换窗口的流量下限——它本来就是从半路往回补，真正
        的硬保护只剩超压。恢复过程仍受编排器协同，不施加对顶折扣。
        """

        if not pump.running():
            return SAFE_ISOLATED
        other = ctx.cold_pump if pump is ctx.hot_pump else ctx.hot_pump
        pump.set_flow_target(ctx.baseline)
        slope = self._limits.handoff_fast_slope_kg_s2
        for _ in range(MAX_STEPS):
            pump.step_toward(ctx.baseline, slope, ctx.dt)
            other.step(ctx.dt)
            if self._pump_readback(ctx, pump, "hot_pump" if pump is ctx.hot_pump else "cold_pump"):
                return SAFE_ISOLATED
            flow = ctx.hydraulic.collect([ctx.cold_pump, ctx.hot_pump], coordinated=True)
            pumps = sum(1 for item in (ctx.cold_pump, ctx.hot_pump) if item.running())
            pressure = ctx.header.update(flow, pumps, ctx.dt, coordinated=True)
            ctx.observe(flow, pressure, ctx.duration_s)
            ctx.duration_s += ctx.dt
            self._move_salt(ctx, flow)
            if ctx.header.overpressure():
                return SAFE_ISOLATED
            if pump.at_target() and flow >= ctx.baseline - self._limits.handoff_readback_tolerance_kg_s:
                return SAFE_HOT if pump is ctx.hot_pump else SAFE_COLD
        return SAFE_ISOLATED

    def _isolate_all(self, ctx: "_Context", valve: InletValve | None) -> None:
        """整机隔离：停两台泵、关母管、进口阀归零。"""

        self._stop_quietly(ctx.hot_pump)
        self._stop_quietly(ctx.cold_pump)
        for _ in range(MAX_STEPS):
            flow = ctx.hydraulic.update([ctx.cold_pump, ctx.hot_pump], ctx.dt)
            ctx.header.update(flow, 0, ctx.dt)
            if ctx.cold_pump.idle() and ctx.hot_pump.idle():
                break
        ctx.header.close()
        if valve is not None:
            valve.set_target(0.0)
        if self._isolate is not None:
            self._isolate("handoff_failsafe")

    def _store(self, report: HandoffReport, op_id: str) -> None:
        """把这次切换（含失败与退回）记进切换流水。"""

        record = report.as_dict()
        record["op_id"] = op_id
        self._records.append(record)


class _Context:
    """一次切换过程中的可变工作集。"""

    def __init__(
        self,
        limits: SaltLimits,
        cold_pump: SaltPump,
        hot_pump: SaltPump,
        cold_tank: SaltTank,
        hot_tank: SaltTank,
        header: SaltHeader,
        hydraulic: HydraulicLoop,
        dt: float,
    ) -> None:
        self.limits = limits
        self.cold_pump = cold_pump
        self.hot_pump = hot_pump
        self.cold_tank = cold_tank
        self.hot_tank = hot_tank
        self.header = header
        self.hydraulic = hydraulic
        self.dt = dt
        self.baseline = limits.flow_baseline_kg_s
        self.mode = MODE_SMOOTH
        self.slope = limits.handoff_flow_slope_kg_s2
        self.window_s = limits.handoff_max_window_s
        self.cold_margin = 0.0
        self.hot_margin = 0.0
        self.advance = 0.0
        self.duration_s = 0.0
        self.transfer_kg = 0.0
        self.max_running = 1 if cold_pump.running() else 0
        self.steps: list[dict] = []
        self.readback_failures: list[str] = []
        self.peak = header.pressure_mpa()
        self.min_flow = float("inf")
        self.max_flow = 0.0
        self._points: list[dict] = []

    def observe(self, flow: float, pressure: float, elapsed: float) -> None:
        """每步记录峰值／最低点与一条供控制台展示的轨迹点。"""

        self.peak = max(self.peak, pressure)
        self.min_flow = min(self.min_flow, flow)
        self.max_flow = max(self.max_flow, flow)
        self._points.append(
            {
                "t_s": round(elapsed, 3),
                "cold_kg_s": round(self.cold_pump.flow_kg_s(), 3),
                "hot_kg_s": round(self.hot_pump.flow_kg_s(), 3),
                "net_kg_s": round(flow, 3),
                "pressure_mpa": round(pressure, 3),
            }
        )

    def violation_seed(self, reason: str) -> list[str]:
        """失败时可能伴随的收尾违约项。"""

        result = [reason]
        if self.header.overpressure():
            result.append("header_overpressure")
        return result

    def report(
        self,
        *,
        violations: list[str],
        safe_position: str,
        aborted: bool = False,
    ) -> HandoffReport:
        points = getattr(self, "_points", [])
        min_flow = 0.0 if self.min_flow == float("inf") else self.min_flow
        target = self.limits.header_pressure_target_mpa
        final_violations = list(violations)
        if not aborted and self.header.overpressure() and "header_overpressure" not in final_violations:
            final_violations.append("header_overpressure")
        return HandoffReport(
            ok=not final_violations and not aborted,
            mode=self.mode,
            basis=TRANSITION_BASIS,
            duration_s=self.duration_s,
            steps=self.steps,
            violations=final_violations,
            aborted=aborted,
            safe_position=safe_position,
            baseline_kg_s=self.baseline,
            peak_pressure_mpa=self.peak,
            pressure_overshoot_mpa=max(0.0, self.peak - target),
            min_flow_kg_s=min_flow,
            max_flow_kg_s=self.max_flow,
            flow_dip_kg_s=max(0.0, self.baseline - min_flow),
            flow_overshoot_kg_s=max(0.0, self.max_flow - self.baseline),
            max_pumps_running=self.max_running,
            transfer_kg=self.transfer_kg,
            readback_failures=self.readback_failures,
            cold_level_pct=self.cold_tank.level_pct(),
            hot_level_pct=self.hot_tank.level_pct(),
            trace=points,
        )
