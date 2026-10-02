"""熔盐回路控制器。

把罐、泵、水力模型、回流阀、切换编排、台账、标定与持久化串成一个回路。正常
循环时推进两台泵并汇总净流量，同时按流量增减冷热罐存量；切换走
:class:`~cspplant.salt.handoff.TankHandoff` 的流量斜率交叉状态机——切换进行中
``step`` 直接委托给状态机推进，避免同一周期把泵推进两次。标定结果直接改变
联锁门限。
"""

from __future__ import annotations

import threading

from ..config import SaltLimits
from ..errors import InterlockError
from ..eventbus import Event
from ..receiver.header import SaltHeader
from ..receiver.thermal import ThermalModel
from ..receiver.threshold import ThresholdModel
from .calibrate import CalibrationRecord, FlowCalibrator
from .handoff import TankHandoff
from .handoff_journal import HandoffJournal
from .hydraulic import HydraulicLoop
from .persist import SaltPersistence
from .pump import SaltPump
from .recirc import RecirculationValve
from .record import REJECTED, HandoffRecord
from .snapshot import SaltSnapshot
from .tank import SaltTank

COLD_TANK_CAPACITY_KG = 900_000.0
HOT_TANK_CAPACITY_KG = 900_000.0


class SaltLoop:
    """冷热罐、盐泵、回流阀与母管的回路控制器。"""

    def __init__(
        self,
        limits: SaltLimits,
        threshold: ThresholdModel,
        thermal: ThermalModel,
        header: SaltHeader,
        persistence: SaltPersistence,
        bus=None,
        clock=None,
        journal: HandoffJournal | None = None,
    ) -> None:
        self._limits = limits
        self._threshold = threshold
        self._thermal = thermal
        self._header = header
        self._persistence = persistence
        self._bus = bus
        self._clock = clock
        self._cold_tank = SaltTank("cold-tank", COLD_TANK_CAPACITY_KG, COLD_TANK_CAPACITY_KG * 0.82)
        self._hot_tank = SaltTank("hot-tank", HOT_TANK_CAPACITY_KG, HOT_TANK_CAPACITY_KG * 0.12)
        self._cold_pump = SaltPump("cold-pump", limits)
        self._hot_pump = SaltPump("hot-pump", limits)
        self._hydraulic = HydraulicLoop(limits)
        # 回流阀默认在安全位（开）；循环建立后由操作员／序列隔离（关）。
        self._recirc = RecirculationValve(limits)
        self._journal = journal
        self._handoff = TankHandoff(
            limits,
            cold_pump=self._cold_pump,
            hot_pump=self._hot_pump,
            cold_tank=self._cold_tank,
            hot_tank=self._hot_tank,
            header=self._header,
            hydraulic=self._hydraulic,
            recirc=self._recirc,
            emit=self._emit_handoff,
            account=self._account_levels,
        )
        self._calibrator = FlowCalibrator(limits, threshold)
        self._lock = threading.RLock()
        self._restore()

    def _restore(self) -> None:
        """重启后恢复罐位与流量基线。"""

        levels = self._persistence.read_levels()
        if levels is not None:
            self._cold_tank.set_level_pct(levels["cold_pct"])
            self._hot_tank.set_level_pct(levels["hot_pct"])
        baseline = self._persistence.read_baseline()
        if baseline is not None and baseline["baseline_kg_s"] > 0:
            self._threshold.calibrate(baseline["baseline_kg_s"])

    def _emit_handoff(self, action: str, **payload) -> None:
        """切换状态机事件 → 事件总线（审计由此落盘）。"""

        if self._bus is None:
            return
        self._bus.publish(Event.of("salt", "salt", action, **payload))

    def _account_levels(self, moved: float) -> None:
        """切换每步的罐位记账同步落盘。"""

        self._persistence.save_levels(self._cold_tank.level_pct(), self._hot_tank.level_pct(), 0.0)

    @property
    def limits(self) -> SaltLimits:
        return self._limits

    @property
    def cold_tank(self) -> SaltTank:
        return self._cold_tank

    @property
    def hot_tank(self) -> SaltTank:
        return self._hot_tank

    @property
    def cold_pump(self) -> SaltPump:
        return self._cold_pump

    @property
    def hot_pump(self) -> SaltPump:
        return self._hot_pump

    @property
    def recirc(self) -> RecirculationValve:
        return self._recirc

    @property
    def hydraulic(self) -> HydraulicLoop:
        return self._hydraulic

    @property
    def handoff(self) -> TankHandoff:
        return self._handoff

    @property
    def handoff_journal(self) -> HandoffJournal | None:
        return self._journal

    @property
    def calibrator(self) -> FlowCalibrator:
        return self._calibrator

    @property
    def threshold(self) -> ThresholdModel:
        return self._threshold

    def start_cold(self) -> float:
        """启冷泵。"""

        with self._lock:
            if self._handoff.active():
                raise InterlockError("切换正在进行，禁止手动启冷泵", component="salt")
            if self._cold_tank.at_min():
                raise InterlockError("冷盐罐已到下限，禁止启冷泵", component="salt")
            return self._cold_pump.start()

    def stop_cold(self) -> float:
        """停冷泵。"""

        with self._lock:
            if self._handoff.active():
                raise InterlockError("切换正在进行，禁止手动停冷泵", component="salt")
            return self._cold_pump.stop()

    def start_hot(self) -> float:
        """启热泵；冷泵仍在运转且没有切换在途即拒绝（对顶保护）。"""

        with self._lock:
            if self._handoff.active():
                raise InterlockError("切换正在进行，启热泵由切换编排接管", component="salt")
            if self._cold_pump.running():
                raise InterlockError("冷盐泵仍在运行，禁止手动启热盐泵", component="salt")
            return self._hot_pump.start()

    def stop_hot(self) -> float:
        """停热泵。"""

        with self._lock:
            if self._handoff.active():
                raise InterlockError("切换正在进行，禁止手动停热泵", component="salt")
            return self._hot_pump.stop()

    def open_recirc(self) -> float:
        """把回流阀驱向安全位（开）。"""

        with self._lock:
            if self._handoff.active():
                raise InterlockError("切换正在进行，回流阀由切换编排接管", component="salt")
            return self._recirc.open()

    def close_recirc(self) -> float:
        """隔离再循环回路（关）。"""

        with self._lock:
            if self._handoff.active():
                raise InterlockError("切换正在进行，回流阀由切换编排接管", component="salt")
            return self._recirc.close()

    def step_recirc(self, dt: float = 1.0) -> float:
        """推进回流阀行程，返回推进后的开度。"""

        with self._lock:
            return self._recirc.step(dt)

    def inject_fault(self, device: str, kind: str, active: bool = True) -> dict:
        """注入／消除现场设备故障，供故障演练与回读部分失败测试。

        ``device`` 取 ``cold_pump``／``hot_pump``／``recirc``；``kind`` 取
        ``sensor``（回读掉线）或 ``stuck``（执行器卡死、回读冻结）。
        """

        with self._lock:
            devices = {
                "cold_pump": self._cold_pump,
                "hot_pump": self._hot_pump,
                "recirc": self._recirc,
            }
            target = devices.get(device)
            if target is None:
                raise InterlockError(f"未知的盐侧设备: {device}", component="salt")
            if kind == "sensor":
                target.inject_flow_fault(active) if hasattr(target, "inject_flow_fault") else target.inject_position_fault(active)
            elif kind == "stuck":
                target.inject_stuck(active)
            else:
                raise InterlockError(f"未知的故障类型: {kind}", component="salt")
            return {"device": device, "kind": kind, "active": bool(active)}

    def verify_handoff_consistency(self, audit_ids: set[str] | None = None) -> dict:
        """核对切换台账与审计／操作编号是否一一对应。"""

        if self._journal is None:
            return {"ok": True, "missing_in_journal": [], "journal_count": 0}
        if audit_ids is None:
            return {"ok": True, "missing_in_journal": [], "journal_count": self._journal.count()}
        missing = self._journal.verify_ids(audit_ids)
        return {"ok": not missing, "missing_in_journal": missing, "journal_count": self._journal.count()}

    def pumps_running(self) -> int:
        """返回在运泵数量。"""

        return sum(1 for pump in (self._cold_pump, self._hot_pump) if pump.running())

    def flow_kg_s(self) -> float:
        """返回最近一次汇总的净流量。"""

        return self._hydraulic.net_flow_kg_s()

    def step(self, dt: float) -> dict:
        """推进一个循环步。切换在途时委托给切换状态机，保证泵只被推进一次。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            if self._handoff.active():
                record = self._handoff.tick(dt)
                return {
                    "flow_kg_s": round(self._hydraulic.net_flow_kg_s(), 3),
                    "moved_kg": 0.0,
                    "pressure_mpa": round(self._header.pressure_mpa(), 3),
                    "pumps_running": self.pumps_running(),
                    "overpressure": self._header.overpressure(),
                    "handoff_state": record.state,
                    "handoff_active": True,
                }
            flow = self._hydraulic.update([self._cold_pump, self._hot_pump], dt)
            self._recirc.step(dt)
            moved = 0.0
            if flow > 0.0:
                try:
                    moved = self._cold_tank.draw(flow * dt)
                    self._hot_tank.fill(moved)
                except InterlockError:
                    moved = 0.0
            pressure = self._header.update(flow, self.pumps_running(), dt)
            self._persistence.save_levels(self._cold_tank.level_pct(), self._hot_tank.level_pct(), 0.0)
            return {
                "flow_kg_s": round(flow, 3),
                "moved_kg": round(moved, 3),
                "pressure_mpa": round(pressure, 3),
                "pumps_running": self.pumps_running(),
                "overpressure": self._header.overpressure(),
                "handoff_state": self._handoff.state(),
                "handoff_active": False,
            }

    def begin_changeover(
        self,
        handoff_id: str,
        ts: float,
        *,
        load_mw: float = 0.0,
        target_flow_kg_s: float | None = None,
    ) -> HandoffRecord:
        """发起一次切换（异步推进），返回首个周期的记录。"""

        with self._lock:
            return self._handoff.begin(
                handoff_id,
                ts,
                load_mw=load_mw,
                target_flow_kg_s=target_flow_kg_s,
            )

    def changeover_tick(self, dt: float = 1.0) -> HandoffRecord:
        """推进在途切换一个周期；到达终态时把记录写入台账。"""

        with self._lock:
            record = self._handoff.tick(dt)
            if not self._handoff.active():
                self._file_record(record)
            return record

    def abort_changeover(self, reason: str = "operator_abort") -> bool:
        """人工中止在途切换，退回安全位。"""

        with self._lock:
            return self._handoff.abort_to_recirc(reason)

    def changeover(self, dt: float = 1.0, handoff_id: str = "handoff", ts: float = 0.0,
                   load_mw: float = 0.0) -> HandoffRecord:
        """同步执行完一次冷热罐切换，返回记录。"""

        with self._lock:
            record = self._handoff.begin(handoff_id, ts, load_mw=load_mw)
            if record.outcome == REJECTED:
                self._file_record(record)
                return record
            steps = 0
            while self._handoff.active() and steps < 100_000:
                self._handoff.tick(dt)
                steps += 1
            self._file_record(self._handoff.record())
            return self._handoff.record()

    def _file_record(self, record: HandoffRecord | None) -> None:
        """切换记录落台账（审计事件在状态机推进时已逐条落审计流水）。"""

        if record is None or self._journal is None:
            return
        # 只增不改：同一编号已在台账里就不重复写。
        if self._journal.by_id(record.handoff_id) is None:
            self._journal.append(record)

    def calibrate_flow(self, measured_kg_s: float, now: float, operator: str = "console") -> CalibrationRecord:
        """按实测流量重新标定基线并落盘。"""

        with self._lock:
            record = self._calibrator.calibrate(measured_kg_s, now, operator)
            self._persistence.save_baseline(record.baseline_kg_s, record.epoch)
            return record

    def threshold_kg_s(self) -> float:
        """返回当前低流量联锁门限。"""

        return self._threshold.threshold_kg_s()

    def disturb(self, heat_fraction: float) -> dict:
        """注入一次热负荷扰动：热罐多进盐、冷罐多出盐。"""

        with self._lock:
            delta = max(0.0, min(1.0, float(heat_fraction))) * 2_000.0
            self._cold_tank.draw(delta)
            added = self._hot_tank.fill(delta)
            return {
                "moved_kg": round(added, 1),
                "cold_level_pct": round(self._cold_tank.level_pct(), 3),
                "hot_level_pct": round(self._hot_tank.level_pct(), 3),
                "temperature_c": round(self._thermal.value_c(), 2),
            }

    def status(self) -> dict:
        """汇总熔盐回路状态。"""

        current = self._handoff.record()
        return {
            "cold_tank": self._cold_tank.as_dict(),
            "hot_tank": self._hot_tank.as_dict(),
            "cold_pump": self._cold_pump.as_dict(),
            "hot_pump": self._hot_pump.as_dict(),
            "recirc_valve": self._recirc.as_dict(),
            "net_flow_kg_s": round(self._hydraulic.net_flow_kg_s(), 3),
            "opposed_steps": self._hydraulic.opposed_steps(),
            "hydraulic_updates": self._hydraulic.updates(),
            "header": self._header.status().as_dict(),
            "header_admits": self._header.admits(),
            "header_overpressure_events": self._header.overpressure_events(),
            "baseline_kg_s": round(self._threshold.baseline_kg_s, 3),
            "threshold_kg_s": round(self._threshold.threshold_kg_s(), 3),
            "threshold_evaluations": self._threshold.evaluations(),
            "epoch": self._threshold.epoch,
            "changeovers": self._handoff.changeovers(),
            "handoff_active": self._handoff.active(),
            "handoff_state": self._handoff.state(),
            "current_handoff": current.as_dict() if current is not None else None,
            "handoff_history": (
                [record.as_dict() for record in self._journal.records()] if self._journal is not None else []
            ),
            "calibrations": [record.as_dict() for record in self.calibrator.records()],
        }

    def snapshot(self) -> SaltSnapshot:
        """返回熔盐回路状态快照对象。"""

        return SaltSnapshot(
            cold_level_pct=self._cold_tank.level_pct(),
            hot_level_pct=self._hot_tank.level_pct(),
            cold_flow_kg_s=self._cold_pump.flow_kg_s(),
            hot_flow_kg_s=self._hot_pump.flow_kg_s(),
            net_flow_kg_s=self._hydraulic.net_flow_kg_s(),
            pumps_running=self.pumps_running(),
            opposed_steps=self._hydraulic.opposed_steps(),
            baseline_kg_s=self._threshold.baseline_kg_s,
            threshold_kg_s=self._threshold.threshold_kg_s(),
            epoch=self._threshold.epoch,
        )
