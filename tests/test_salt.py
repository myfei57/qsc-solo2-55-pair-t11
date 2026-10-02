"""盐泵、水力、切换顺序与标定。"""

from __future__ import annotations

import pytest

from cspplant.config import default_config
from cspplant.errors import InterlockError
from cspplant.salt.hydraulic import HydraulicLoop
from cspplant.salt.pump import SaltPump
from cspplant.salt.tank import SaltTank


def test_pump_ramps_at_configured_rate():
    limits = default_config().salt
    pump = SaltPump("cold-pump", limits)
    pump.start()
    assert pump.flow_kg_s() == 0.0
    assert pump.step(1.0) == pytest.approx(limits.pump_ramp_kg_s2)
    pump.settle()
    assert pump.flow_kg_s() == pytest.approx(limits.flow_baseline_kg_s)
    assert pump.at_target() is True
    pump.stop()
    pump.settle()
    assert pump.idle() is True
    assert pump.starts() == 1 and pump.stops() == 1


def test_pump_rejects_double_start():
    pump = SaltPump("cold-pump", default_config().salt)
    pump.start()
    with pytest.raises(InterlockError):
        pump.start()


def test_two_pumps_are_opposed_in_hydraulics():
    limits = default_config().salt
    cold = SaltPump("cold-pump", limits)
    hot = SaltPump("hot-pump", limits)
    loop = HydraulicLoop(limits)
    cold.start()
    for _ in range(120):
        loop.update([cold, hot], 1.0)
    single = loop.net_flow_kg_s()
    hot.start()
    for _ in range(120):
        loop.update([cold, hot], 1.0)
    assert loop.net_flow_kg_s() < single * 1.4
    assert loop.opposed_steps() > 0
    assert loop.updates() == 240


def test_tank_levels_and_bounds():
    tank = SaltTank("cold-tank", 1000.0, 500.0, min_pct=20.0, max_pct=80.0)
    assert tank.level_pct() == pytest.approx(50.0)
    tank.draw(100.0)
    assert tank.level_kg() == pytest.approx(400.0)
    tank.fill(1000.0)
    assert tank.level_pct() == pytest.approx(80.0)
    assert tank.at_max() is True
    with pytest.raises(InterlockError):
        tank.fill(10.0)


def test_changeover_is_bumpless_on_flow_slope(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    before_flow = platform.salt.flow_kg_s()
    report = platform.changeover()
    assert report["ok"] is True
    assert report["violations"] == []
    # 过渡基准定死为流量斜率，时长只是看门狗。
    assert report["basis"] == "flow_slope"
    # 协同窗口内两泵受控并列（负荷交接），但不构成非协同对顶。
    assert report["max_pumps_running"] == 2
    assert platform.salt.hydraulic.opposed_steps() == 0
    # 整个切换过程净流量不断档、母管压力不塌不超。
    assert report["min_flow_kg_s"] >= platform.config.salt.handoff_flow_floor_kg_s
    assert report["flow_dip_kg_s"] == 0.0
    assert report["peak_pressure_mpa"] < platform.config.salt.header_pressure_max_mpa
    assert report["pressure_overshoot_mpa"] <= platform.config.salt.handoff_pressure_overshoot_mpa
    assert abs(platform.salt.flow_kg_s() - before_flow) < 1e-6
    # 时序写死：启热泵在停冷泵之前，且先预检。
    actions = [step["action"] for step in report["steps"]]
    assert actions.index("handoff_preflight") < actions.index("start_hot_pump")
    assert actions.index("start_hot_pump") < actions.index("stop_cold_pump")
    assert actions.index("stop_cold_pump") < actions.index("confirm_hot_flow")
    assert platform.salt.cold_pump.running() is False
    assert platform.salt.hot_pump.running() is True
    # 轨迹逐点都在流量下限之上，供控制台展示。
    assert report["trace"]
    assert all(point["net_kg_s"] >= platform.config.salt.handoff_flow_floor_kg_s for point in report["trace"])


def test_changeover_uses_fast_slope_near_tank_limits(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    # 冷罐距下限只剩 3 个百分点（< 极端余量 5%），应选快速档。
    platform.salt.cold_tank.set_level_pct(platform.config.salt.cold_level_min_pct + 3.0)
    report = platform.changeover()
    assert report["ok"] is True
    assert report["mode"] == "fast"
    assert report["flow_dip_kg_s"] == 0.0
    # 快速档比平滑档更快结束让盐窗口。
    smooth_seconds = platform.config.salt.flow_baseline_kg_s / platform.config.salt.handoff_flow_slope_kg_s2
    assert report["duration_s"] < smooth_seconds


def test_changeover_rejected_at_hard_tank_limits(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    platform.salt.cold_tank.set_level_pct(platform.config.salt.cold_level_min_pct)
    report = platform.changeover()
    assert report["ok"] is False
    assert "cold_tank_empty" in report["violations"]
    # 硬联锁拒绝时现场设备保持原状（冷泵仍在运）。
    assert platform.salt.cold_pump.running() is True
    assert platform.salt.hot_pump.running() is False


def test_changeover_records_overshoot_for_later_comparison(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    report = platform.changeover()
    records = platform.salt.handoff_records()
    assert len(records) == 1
    record = records[-1]
    for key in (
        "mode",
        "duration_s",
        "peak_pressure_mpa",
        "pressure_overshoot_mpa",
        "flow_dip_kg_s",
        "flow_overshoot_kg_s",
        "min_flow_kg_s",
        "safe_position",
        "op_id",
    ):
        assert key in record
    assert record["pressure_overshoot_mpa"] == report["pressure_overshoot_mpa"]
    assert record["op_id"] == report["op_id"]


def test_changeover_cold_pump_readback_fault_hot_pump_takes_over(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    calls = {"n": 0}
    real_step = platform.salt.cold_pump.step_toward

    def faulting_step(target, slope, dt):  # 切换到一半冷泵回读卡死
        calls["n"] += 1
        result = real_step(target, slope, dt)
        if calls["n"] == 10:
            platform.salt.cold_pump.set_readback_fault(0.0)
        return result

    platform.salt.cold_pump.step_toward = faulting_step
    report = platform.changeover()
    assert report["ok"] is False
    assert report["aborted"] is True
    assert "cold_pump" in report["readback_failures"]
    assert "cold_pump_readback_fault" in report["violations"]
    # 退回安全位：切掉冷泵，健康热泵接管恢复基线流量。
    assert report["safe_position"] == "hot_pump_carrying"
    assert platform.salt.cold_pump.running() is False
    assert platform.salt.hot_pump.running() is True
    assert platform.salt.flow_kg_s() == pytest.approx(platform.config.salt.flow_baseline_kg_s, abs=3.0)


def test_changeover_hot_pump_readback_fault_cold_pump_keeps_carrying(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    calls = {"n": 0}
    real_step = platform.salt.hot_pump.step_toward

    def faulting_step(target, slope, dt):  # 切换到一半热泵回读卡死
        calls["n"] += 1
        result = real_step(target, slope, dt)
        if calls["n"] == 10:
            platform.salt.hot_pump.set_readback_fault(0.0)
        return result

    platform.salt.hot_pump.step_toward = faulting_step
    report = platform.changeover()
    assert report["ok"] is False
    assert report["aborted"] is True
    assert report["safe_position"] == "cold_pump_carrying"
    assert platform.salt.hot_pump.running() is False
    assert platform.salt.cold_pump.running() is True
    assert platform.salt.flow_kg_s() == pytest.approx(platform.config.salt.flow_baseline_kg_s, abs=3.0)


def test_changeover_valve_readback_fault_isolates(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    calls = {"n": 0}
    real_update = platform.receiver.header.update

    def faulting_update(flow, pumps, dt, *, coordinated=False):  # 阀回读中途丢失
        calls["n"] += 1
        if calls["n"] == 5:
            platform.receiver.valve.set_readback_fault(20.0)
        return real_update(flow, pumps, dt, coordinated=coordinated)

    platform.receiver.header.update = faulting_update
    report = platform.changeover()
    assert report["ok"] is False
    assert report["aborted"] is True
    assert "inlet_valve_readback_fault" in report["violations"]
    assert report["safe_position"] == "tripped_isolated"
    # 整机隔离：两泵停、母管关、进口阀归零。
    assert platform.salt.pumps_running() == 0
    assert platform.receiver.header.state() == "closed"
    assert platform.receiver.valve.target_pct() == 0.0


def test_changeover_requires_running_cold_pump(platform):
    platform.start()
    report = platform.changeover()
    assert report["ok"] is False
    assert "cold_pump_not_running" in report["violations"]


def test_changeover_journal_matches_audit(platform):
    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    report = platform.changeover()
    op_id = report["op_id"]
    # 切换流水（含过冲）与审计汇总用同一个 op_id，数字口径一致。
    complete = [
        entry
        for entry in platform.audit.entries(limit=200)
        if entry["action"] == "changeover_complete" and entry["payload"].get("op_id") == op_id
    ]
    assert len(complete) == 1
    payload = complete[0]["payload"]
    assert payload["ok"] is report["ok"]
    assert payload["violations"] == report["violations"]
    journal = next(item for item in platform.salt.handoff_records() if item["op_id"] == op_id)
    assert journal["peak_pressure_mpa"] == round(report["peak_pressure_mpa"], 3)
    assert journal["pressure_overshoot_mpa"] == round(report["pressure_overshoot_mpa"], 3)
    assert journal["flow_dip_kg_s"] == round(report["flow_dip_kg_s"], 3)


def test_start_hot_pump_rejected_while_cold_running(platform):
    platform.start()
    platform.salt.start_cold()
    with pytest.raises(InterlockError):
        platform.salt.start_hot()


def test_calibration_moves_interlock_threshold(platform):
    platform.start()
    platform.salt.start_cold()
    platform.salt.cold_pump.settle()
    before = platform.salt.threshold_kg_s()
    record = platform.calibrate_flow(72.0, "shift-lead")
    assert record["epoch"] == 1
    assert record["baseline_kg_s"] == pytest.approx(72.0)
    assert platform.salt.threshold_kg_s() > before
    assert platform.receiver.threshold.threshold_kg_s() == pytest.approx(platform.salt.threshold_kg_s())


def test_salt_levels_persist_across_restart(platform, clock):
    from cspplant.app import build_plant

    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    for _ in range(60):
        platform.cycle(1.0)
    cold_pct = platform.salt.cold_tank.level_pct()
    hot_pct = platform.salt.hot_tank.level_pct()

    restarted = build_plant(default_config(), clock=clock, storage_root=platform.store.root)
    assert restarted.salt.cold_tank.level_pct() == pytest.approx(cold_pct, abs=1e-6)
    assert restarted.salt.hot_tank.level_pct() == pytest.approx(hot_pct, abs=1e-6)


def test_handoff_records_replay_across_restart(platform, clock):
    from cspplant.app import build_plant

    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    report = platform.changeover()
    assert report["ok"] is True

    restarted = build_plant(default_config(), clock=clock, storage_root=platform.store.root)
    records = restarted.salt.handoff_records()
    assert len(records) == 1
    assert records[0]["op_id"] == report["op_id"]
    assert records[0]["pressure_overshoot_mpa"] == round(report["pressure_overshoot_mpa"], 3)
    assert records[0]["basis"] == "flow_slope"
