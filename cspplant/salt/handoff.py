"""冷热罐切换编排：流量斜率交叉的状态机。

旧做法是"先停冷泵、等停稳、再启热泵"，中间母管流量整段掉到零，高负荷下压力
跟着塌。新编排把过渡方式**定死为流量斜率交叉**：切换期间两台泵短时重叠，出泵
流量目标按份额下降、进泵按份额上升，两者之和始终夹在基线带内；交叉是否完成
看进泵实测份额（不看时间），看门狗窗口超时只触发退回安全位，绝不用"时间到"
推动状态。

模式（``slow``／``fast``／``inhibited``）由 :mod:`~cspplant.salt.mode` 按罐况
裁决：罐况允许就长窗口缓斜率平缓过渡，罐况紧就短窗口陡斜率快速切换保安全，
罐况不够快速窗口或罐已越限则拒绝硬切（``begin`` 直接返回 ``inhibited`` 违约），
由调用方走再循环旁路保持／降负荷等别的走法。

时序写死在状态机里，任何外部调用都不能跳步：
    预检 → 关回流阀(回读) → 启进泵(回读建流) → 流量斜率交叉 →
    停出泵(回读停稳) → 流量压力双确认停留 → 完成
回读失败按部分失败处理：进泵建流之前（未提交）退回原状态（出泵恢复、回流阀
打开）；建流之后（已提交）推进到进泵单泵安全位；回流阀任何回读失败一律按其
安全位（开）处理并保持。
"""

from __future__ import annotations

from ..config import SaltLimits
from ..errors import InterlockError
from ..receiver.header import SaltHeader
from .hydraulic import HydraulicLoop
from .mode import FAST, INHIBITED, SLOW, HandoffMode, select_mode
from .pump import SaltPump
from .recirc import RecirculationValve
from .record import COMPLETED, REJECTED, ROLLED_BACK, SAFE_HOLD, HandoffRecord
from .tank import SaltTank

# 状态机状态。
IDLE = "idle"
PRECHECK = "precheck"
VALVE_CLOSING = "valve_closing"
INCOMING_STARTING = "incoming_starting"
CROSSING = "crossing"
OUTGOING_STOPPING = "outgoing_stopping"
SETTLING = "settling"
ROLLING_BACK = "rolling_back"
SAFE_HOLDING = "safe_holding"
DONE = "done"

# 非交叉阶段的看门狗（秒）：动作阶段条件达成即走，这里只兜底。
PHASE_WATCHDOG_S = 60.0
SETTLE_WATCHDOG_S = 90.0
# 交叉阶段判定出泵已断流的份额上限。
OUTGOING_CUTOFF_SHARE = 0.05
# 进泵建流确认门限（占基线比例）。
INCOMING_MIN_FLOW_FRACTION = 0.1
# 轨迹采样间隔（秒）。
TRACE_PERIOD_S = 5.0
# 回读冻结即判故障的连续步数下限由配置的 stale_s 换算。

_TERMINAL = (DONE,)
_ACTIVE = {PRECHECK, VALVE_CLOSING, INCOMING_STARTING, CROSSING, OUTGOING_STOPPING, SETTLING,
           ROLLING_BACK, SAFE_HOLDING}


class TankHandoff:
    """流量斜率交叉切换状态机。

    事件回调 ``emit`` 由上层（SaltLoop）注入：阶段推进、回读失败、终态都通过它
    发布事件总线，审计与台账因此拿到的是同一份事实。
    """

    def __init__(
        self,
        limits: SaltLimits,
        *,
        cold_pump: SaltPump,
        hot_pump: SaltPump,
        cold_tank: SaltTank,
        hot_tank: SaltTank,
        header: SaltHeader,
        hydraulic: HydraulicLoop,
        recirc: RecirculationValve,
        emit=None,
        account=None,
    ) -> None:
        self._limits = limits
        self._cold_pump = cold_pump
        self._hot_pump = hot_pump
        self._cold_tank = cold_tank
        self._hot_tank = hot_tank
        self._header = header
        self._hydraulic = hydraulic
        self._recirc = recirc
        self._emit = emit if emit is not None else lambda *args, **kwargs: None
        self._account = account if account is not None else lambda moved: None
        self._state = IDLE
        self._changeovers = 0
        self._record: HandoffRecord | None = None
        self._mode: HandoffMode | None = None
        self._share = 0.0
        self._elapsed = 0.0
        self._phase_elapsed = 0.0
        self._settle_ok_s = 0.0
        self._last_trace = 0.0
        self._committed = False
        self._watchdog_violation = ""

    # ---- 查询 ---------------------------------------------------------

    @property
    def limits(self) -> SaltLimits:
        return self._limits

    def active(self) -> bool:
        """切换是否正在进行（含退回／保持未终态）。"""

        return self._state in _ACTIVE

    def state(self) -> str:
        return self._state

    def record(self) -> HandoffRecord | None:
        """返回当前／最近一次切换记录。"""

        return self._record

    def changeovers(self) -> int:
        """累计成功切换次数。"""

        return self._changeovers

    # ---- 入口 ---------------------------------------------------------

    def begin(
        self,
        handoff_id: str,
        ts: float,
        *,
        load_mw: float = 0.0,
        target_flow_kg_s: float | None = None,
    ) -> HandoffRecord:
        """发起一次冷泵 → 热泵切换；不满足联锁或罐况禁止时返回拒绝记录。"""

        if self.active():
            raise InterlockError("已有切换正在进行，禁止重入", component="salt")

        violations: list[str] = []
        if not self._cold_pump.running():
            violations.append("cold_pump_not_running")
        if self._hot_pump.running():
            violations.append("hot_pump_already_running")

        target = float(target_flow_kg_s or self._limits.flow_baseline_kg_s)
        mode = select_mode(self._limits, self._cold_tank, self._hot_tank, target)
        if mode.inhibited:
            violations.append("handoff_inhibited_by_tank_margin")

        record = HandoffRecord(
            handoff_id=handoff_id,
            ts=float(ts),
            mode=mode.mode,
            state=PRECHECK,
            window_s=mode.window_s,
            available_s=mode.available_s,
            load_mw=float(load_mw),
            flow_baseline_kg_s=target,
            pressure_target_mpa=self._limits.header_pressure_target_mpa,
            flow_min_kg_s=self._hydraulic.net_flow_kg_s(),
            flow_max_kg_s=self._hydraulic.net_flow_kg_s(),
            pressure_min_mpa=self._header.pressure_mpa(),
            pressure_max_mpa=self._header.pressure_mpa(),
        )
        record.violations = violations
        record.steps.append(
            {
                "component": "salt",
                "action": "precheck",
                "detail": {
                    "mode": mode.mode,
                    "available_s": round(mode.available_s, 1),
                    "window_s": round(mode.window_s, 1),
                    "reasons": ",".join(mode.reasons),
                },
            }
        )
        self._record = record

        if violations:
            record.state = IDLE
            record.outcome = REJECTED
            record.ok = False
            self._emit(
                "handoff_rejected",
                severity="alarm",
                message=f"切换被拒绝：{','.join(violations)}",
                handoff_id=handoff_id,
                mode=mode.mode,
                violation=";".join(violations),
                available_s=round(mode.available_s, 1),
            )
            return record

        self._mode = mode
        self._state = VALVE_CLOSING
        self._share = 0.0
        self._elapsed = 0.0
        self._phase_elapsed = 0.0
        self._settle_ok_s = 0.0
        self._last_trace = 0.0
        self._committed = False
        self._watchdog_violation = ""
        self._recirc.close()
        self._emit(
            "handoff_begin",
            severity="notice",
            message=f"切换开始（{mode.mode} 模式）",
            handoff_id=handoff_id,
            mode=mode.mode,
            window_s=round(mode.window_s, 1),
            available_s=round(mode.available_s, 1),
            load_mw=round(float(load_mw), 3),
        )
        self._step_event("close_recirc_valve", {"target_pct": 0.0})
        return record

    def abort_to_recirc(self, reason: str = "operator_abort") -> bool:
        """人工中止：进回流水位保持安全位。未开始或已终态返回 False。"""

        if not self.active():
            return False
        self._fail(reason, forward=self._committed)
        return True

    # ---- 推进 ---------------------------------------------------------

    def tick(self, dt: float) -> HandoffRecord:
        """推进一个控制周期。未在切换时抛错（调用方应先看 active()）。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        if not self.active() or self._record is None or self._mode is None:
            raise InterlockError("当前没有进行中的切换", component="salt")

        self._elapsed += dt
        self._phase_elapsed += dt
        failure = self._readback_failure()

        if self._state == VALVE_CLOSING:
            self._advance_actuators(dt, coordinated=False)
            if failure:
                self._fail(failure, forward=False)
            elif self._recirc.is_closed():
                self._enter(INCOMING_STARTING)
                # 进泵以零流量目标并网，随即进入斜率交叉，总流量一刻不抬升。
                self._hot_pump.start(0.0)
                self._step_event("start_hot_pump", {"target_flow_kg_s": 0.0})
            elif self._phase_elapsed > PHASE_WATCHDOG_S:
                self._fail("recirc_valve_close_timeout", forward=False)

        elif self._state == INCOMING_STARTING:
            # 与交叉阶段同一套份额驱动：进泵涨多少、出泵就退多少。
            self._share = min(1.0, self._share + self._mode.share_ramp_per_s * dt)
            self._drive_crossover()
            self._advance_actuators(dt, coordinated=True)
            hot_flow = self._hot_pump.readback_flow_kg_s()
            min_flow = self._record.flow_baseline_kg_s * INCOMING_MIN_FLOW_FRACTION
            if failure:
                self._fail(failure, forward=False)
            elif hot_flow is not None and hot_flow >= min_flow:
                # 进泵已稳定出流，切换不可再整体撤回——提交点。
                self._committed = True
                self._enter(CROSSING)
                self._step_event("flow_crossover_begin", {"incoming_flow_kg_s": round(hot_flow, 3)})
            elif self._phase_elapsed > PHASE_WATCHDOG_S:
                self._fail("incoming_pump_failed_to_establish", forward=False)

        elif self._state == CROSSING:
            # 流量斜率：进泵份额按模式斜率上升，出泵=1-进泵；总流量夹在基线。
            self._share = min(1.0, self._share + self._mode.share_ramp_per_s * dt)
            self._drive_crossover()
            self._advance_actuators(dt, coordinated=True)
            total = self._hydraulic.net_flow_kg_s()
            cold_flow = self._cold_pump.readback_flow_kg_s()
            hot_flow = self._hot_pump.readback_flow_kg_s()
            incoming_share = hot_flow / total if total > 1e-9 and hot_flow is not None else 0.0
            if failure:
                self._fail(failure, forward=True)
            elif (
                incoming_share >= self._limits.handoff_takeover_share
                and cold_flow is not None
                and cold_flow <= self._record.flow_baseline_kg_s * OUTGOING_CUTOFF_SHARE
            ):
                self._enter(OUTGOING_STOPPING)
                self._cold_pump.stop()
                self._step_event("stop_cold_pump", {"incoming_share": round(incoming_share, 3)})
            elif self._phase_elapsed > self._mode.window_s:
                # 窗口是看门狗：超时不硬切，按部分失败退回安全位。
                self._fail("crossover_window_exceeded", forward=True)
            elif total < self._limits.low_flow_threshold(self._record.flow_baseline_kg_s):
                self._fail("header_flow_below_interlock", forward=True)

        elif self._state == OUTGOING_STOPPING:
            self._hot_pump.set_target_flow(self._record.flow_baseline_kg_s)
            self._advance_actuators(dt, coordinated=True)
            cold_rb = self._cold_pump.readback_flow_kg_s()
            if failure:
                self._fail(failure, forward=True)
            elif not self._cold_pump.running() and cold_rb is not None and cold_rb <= 1e-6:
                self._enter(SETTLING)
                self._step_event("confirm_flow_pressure", {})
            elif self._phase_elapsed > PHASE_WATCHDOG_S:
                self._fail("outgoing_pump_not_stopped", forward=True)

        elif self._state == SETTLING:
            self._hot_pump.set_target_flow(self._record.flow_baseline_kg_s)
            self._advance_actuators(dt, coordinated=False)
            flow_ok = (
                abs(self._hydraulic.net_flow_kg_s() - self._record.flow_baseline_kg_s)
                <= self._limits.handoff_flow_band_kg_s
            )
            pressure = self._header.pressure_mpa()
            pressure_ok = (
                pressure <= self._limits.header_pressure_max_mpa
                and pressure >= self._limits.header_pressure_target_mpa - self._limits.flow_tolerance_kg_s * 0.1
            )
            if failure:
                self._fail(failure, forward=True)
            elif flow_ok and pressure_ok:
                self._settle_ok_s += dt
                if self._settle_ok_s >= self._limits.handoff_confirm_s:
                    self._complete()
            else:
                self._settle_ok_s = 0.0
                if self._phase_elapsed > SETTLE_WATCHDOG_S:
                    self._fail("settle_confirm_timeout", forward=True)

        elif self._state == ROLLING_BACK:
            # 未提交的退回：出泵拉回基线、进泵解列、回流阀打开。
            self._cold_pump.set_target_flow(self._record.flow_baseline_kg_s)
            if self._hot_pump.running():
                self._hot_pump.stop()
            self._recirc.open()
            self._advance_actuators(dt, coordinated=False)
            cold_rb = self._cold_pump.readback_flow_kg_s()
            cold_back = (
                cold_rb is not None
                and abs(cold_rb - self._record.flow_baseline_kg_s) <= self._limits.handoff_flow_band_kg_s
            )
            if cold_back and self._recirc.is_open():
                self._finish_failure(ROLLED_BACK)
            elif self._phase_elapsed > SETTLE_WATCHDOG_S:
                # 连原状都回不去，升级为全停安全保持。
                self._enter(SAFE_HOLDING)
                self._phase_elapsed = 0.0

        elif self._state == SAFE_HOLDING:
            if self._cold_pump.running():
                self._cold_pump.stop()
            if self._hot_pump.running():
                self._hot_pump.stop()
            self._recirc.open()
            self._advance_actuators(dt, coordinated=False)
            if self._phase_elapsed > self._limits.handoff_confirm_s:
                self._finish_failure(SAFE_HOLD)

        self._sample_trace(dt)
        return self._record

    # ---- 内部 ---------------------------------------------------------

    def _enter(self, state: str) -> None:
        self._state = state
        self._phase_elapsed = 0.0
        if self._record is not None:
            self._record.state = state

    def _drive_crossover(self) -> None:
        """按份额给两台泵下发互补流量目标，把总流量夹在基线。"""

        assert self._record is not None
        baseline = self._record.flow_baseline_kg_s
        incoming_target = baseline * self._share
        outgoing_target = baseline * (1.0 - self._share)
        self._hot_pump.set_target_flow(incoming_target)
        self._cold_pump.set_target_flow(outgoing_target)

    def _advance_actuators(self, dt: float, *, coordinated: bool) -> None:
        """推进泵、汇流量、记罐账、更新母管压力与过冲统计。"""

        assert self._record is not None
        flow = self._hydraulic.update([self._cold_pump, self._hot_pump], dt, coordinated=coordinated)
        self._recirc.step(dt)
        # 切换期间盐仍在冷罐 → 热罐之间流动；再循环保持阶段由调用方停止记账。
        moved = min(flow * dt, self._cold_tank.level_kg())
        try:
            drawn = self._cold_tank.draw(moved)
            self._hot_tank.fill(drawn)
            self._account(drawn)
        except InterlockError as error:
            # 罐在切换中触限：立即按已提交处理，进安全保持。
            self._fail(error.message, forward=True)
            return
        pressure = self._header.update(flow, self._pumps_running(), dt, coordinated=coordinated)

        rec = self._record
        rec.max_pumps_running = max(rec.max_pumps_running, self._pumps_running())
        rec.flow_min_kg_s = min(rec.flow_min_kg_s, flow)
        rec.flow_max_kg_s = max(rec.flow_max_kg_s, flow)
        rec.flow_overshoot_kg_s = max(rec.flow_overshoot_kg_s, flow - rec.flow_baseline_kg_s)
        rec.flow_undershoot_kg_s = max(rec.flow_undershoot_kg_s, rec.flow_baseline_kg_s - flow)
        rec.pressure_min_mpa = min(rec.pressure_min_mpa, pressure)
        rec.pressure_max_mpa = max(rec.pressure_max_mpa, pressure)
        rec.peak_pressure_mpa = rec.pressure_max_mpa
        rec.pressure_overshoot_mpa = max(
            rec.pressure_overshoot_mpa, pressure - rec.pressure_target_mpa
        )
        rec.pressure_undershoot_mpa = max(
            rec.pressure_undershoot_mpa, rec.pressure_target_mpa - pressure
        )

    def _pumps_running(self) -> int:
        return sum(1 for pump in (self._cold_pump, self._hot_pump) if pump.running())

    def _readback_failure(self) -> str:
        """汇总本周期回读失败：传感器掉线立即报；回读冻结超过 stale_s 报。"""

        stale = self._limits.handoff_readback_stale_s
        cold = self._cold_pump.readback_flow_kg_s()
        hot = self._hot_pump.readback_flow_kg_s()
        if cold is None and self._cold_pump.running():
            return "cold_pump_readback_lost"
        if hot is None and self._hot_pump.running():
            return "hot_pump_readback_lost"
        valve = self._recirc.readback_pct()
        if valve is None:
            return "recirc_valve_readback_lost"
        # 冻结检测：指令在动、回读不动，且持续超过一个泵升速节拍的若干倍。
        # 用泵内部真值与回读差值超过一个 stale 窗口的应有行程来判定。
        for pump, tag in ((self._cold_pump, "cold"), (self._hot_pump, "hot")):
            rb = pump.readback_flow_kg_s()
            if rb is None or not pump.running():
                continue
            if abs(pump.flow_kg_s() - rb) > self._limits.pump_ramp_kg_s2 * stale:
                return f"{tag}_pump_readback_stuck"
        if self._recirc.position_pct() != self._recirc.target_pct():
            rb_v = self._recirc.readback_pct()
            if rb_v is not None and abs(self._recirc.position_pct() - rb_v) > (
                self._limits.recirc_valve_travel_pct_s * stale
            ):
                return "recirc_valve_readback_stuck"
        return ""

    def _fail(self, violation: str, *, forward: bool) -> None:
        """按提交点选择退回方向：未提交退回原状，已提交推进安全位。"""

        if self._record is None:
            return
        if violation not in self._record.violations:
            self._record.violations.append(violation)
        self._emit(
            "handoff_partial_failure",
            severity="warning",
            message=f"切换部分失败：{violation}，{'推进安全位' if forward else '退回原状态'}",
            handoff_id=self._record.handoff_id,
            state=self._state,
            violation=violation,
            forward=forward,
        )
        if forward:
            # 进泵单泵安全位：进泵拉基线、出泵解列、回流阀保持关闭后转入确认；
            # 若进泵本身不可信（回读丢失），直接全停 + 回流阀打开保持。
            if violation in ("hot_pump_readback_lost", "hot_pump_readback_stuck"):
                self._enter(SAFE_HOLDING)
            else:
                if self._cold_pump.running():
                    self._cold_pump.stop()
                self._hot_pump.set_target_flow(self._record.flow_baseline_kg_s)
                self._enter(SETTLING)
                self._settle_ok_s = 0.0
                self._phase_elapsed = 0.0
        else:
            self._enter(ROLLING_BACK)

    def _complete(self) -> None:
        assert self._record is not None
        self._record.state = DONE
        self._record.outcome = COMPLETED
        self._record.ok = True
        self._record.duration_s = self._elapsed
        total = self._hydraulic.net_flow_kg_s()
        self._record.incoming_final_share = (
            self._hot_pump.flow_kg_s() / total if total > 1e-9 else 0.0
        )
        self._state = DONE
        self._changeovers += 1
        self._step_event(
            "handoff_complete",
            {
                "duration_s": round(self._elapsed, 1),
                "flow_overshoot_kg_s": round(self._record.flow_overshoot_kg_s, 3),
                "flow_undershoot_kg_s": round(self._record.flow_undershoot_kg_s, 3),
                "pressure_overshoot_mpa": round(self._record.pressure_overshoot_mpa, 3),
                "pressure_undershoot_mpa": round(self._record.pressure_undershoot_mpa, 3),
            },
        )
        self._emit(
            "changeover_complete",
            severity="notice",
            message="切换完成",
            handoff_id=self._record.handoff_id,
            duration_s=round(self._elapsed, 1),
            flow_overshoot_kg_s=round(self._record.flow_overshoot_kg_s, 3),
            flow_undershoot_kg_s=round(self._record.flow_undershoot_kg_s, 3),
            pressure_overshoot_mpa=round(self._record.pressure_overshoot_mpa, 3),
            pressure_undershoot_mpa=round(self._record.pressure_undershoot_mpa, 3),
        )

    def _finish_failure(self, outcome: str) -> None:
        assert self._record is not None
        self._record.state = DONE
        self._record.outcome = outcome
        self._record.ok = False
        self._record.duration_s = self._elapsed
        self._state = DONE
        self._step_event(
            "handoff_safe_position",
            {"outcome": outcome, "violations": ";".join(self._record.violations)},
        )
        self._emit(
            "changeover_safe_position",
            severity="warning",
            message=f"切换已退回安全位：{outcome}",
            handoff_id=self._record.handoff_id,
            outcome=outcome,
            violations=";".join(self._record.violations),
        )

    def _step_event(self, action: str, detail: dict) -> None:
        assert self._record is not None
        self._record.steps.append(
            {
                "component": "salt",
                "action": action,
                "detail": {"at_s": round(self._elapsed, 1), "state": self._state, **detail},
            }
        )

    def _sample_trace(self, dt: float) -> None:
        assert self._record is not None
        if self._elapsed - self._last_trace < TRACE_PERIOD_S and self._state != DONE:
            return
        self._last_trace = self._elapsed
        self._record.trace.append(
            {
                "t_s": round(self._elapsed, 1),
                "state": self._state,
                "cold_flow_kg_s": round(self._cold_pump.flow_kg_s(), 3),
                "hot_flow_kg_s": round(self._hot_pump.flow_kg_s(), 3),
                "flow_kg_s": round(self._hydraulic.net_flow_kg_s(), 3),
                "pressure_mpa": round(self._header.pressure_mpa(), 3),
                "recirc_pct": round(self._recirc.position_pct(), 2),
                "share": round(self._share, 3),
                "cold_level_pct": round(self._cold_tank.level_pct(), 3),
                "hot_level_pct": round(self._hot_tank.level_pct(), 3),
            }
        )

    # ---- 兼容旧同步入口 ----------------------------------------------

    def changeover(
        self,
        cold_pump: SaltPump,
        hot_pump: SaltPump,
        cold_tank: SaltTank,
        hot_tank: SaltTank,
        header: SaltHeader,
        hydraulic: HydraulicLoop,
        dt: float = 1.0,
        handoff_id: str = "handoff",
        ts: float = 0.0,
        load_mw: float = 0.0,
    ) -> HandoffRecord:
        """同步跑完一次切换，返回记录（供脚本与测试使用）。"""

        record = self.begin(handoff_id, ts, load_mw=load_mw)
        if record.outcome == REJECTED:
            return record
        steps = 0
        while self.active() and steps < 100_000:
            self.tick(dt)
            steps += 1
        return self._record  # type: ignore[return-value]
