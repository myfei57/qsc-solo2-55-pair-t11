"""冷热罐流量斜率切换：平过渡、罐况定模式、回读部分失败、过冲台账、审计一致。"""

from __future__ import annotations

import pytest

from cspplant.config import default_config
from cspplant.salt.mode import FAST, INHIBITED, SLOW, select_mode


def _prepare(platform, *, recirc_closed: bool = True):
    """建立追日 + 冷盐循环，并把回流阀隔离到位。"""

    platform.start()
    platform.track(800.0, 720.0)
    platform.circulate()
    platform.salt.close_recirc()
    if recirc_closed:
        for _ in range(20):
            platform.salt.step_recirc(1.0)
        assert platform.salt.recirc.is_closed()


def test_normal_handoff_holds_flow_and_pressure(platform):
    _prepare(platform)
    flow_before = platform.salt.flow_kg_s()
    report = platform.changeover()
    assert report["ok"] is True
    assert report["mode"] in (SLOW, FAST)
    # 过渡期间母管流量掉不下去、也不抬上去。
    assert report["flow_undershoot_kg_s"] < platform.config.salt.handoff_flow_band_kg_s
    assert report["flow_overshoot_kg_s"] < platform.config.salt.handoff_flow_band_kg_s
    assert report["flow_min_kg_s"] >= flow_before - platform.config.salt.handoff_flow_band_kg_s
    # 高负荷最怕压力塌：压力最低点仍在目标附近。
    assert report["pressure_min_mpa"] > 1.5
    assert report["peak_pressure_mpa"] < platform.config.salt.header_pressure_max_mpa


def test_mode_is_decided_by_tank_margin_not_operator(platform):
    limits = platform.config.salt
    salt = platform.salt
    # 罐况充足：平缓。
    mode = select_mode(limits, salt.cold_tank, salt.hot_tank, limits.flow_baseline_kg_s)
    assert mode.mode == SLOW
    # 把冷罐压到只剩快速窗口余量：快速。
    fast_mass = limits.flow_baseline_kg_s * (limits.handoff_fast_window_s + 10.0)
    guard_kg = salt.cold_tank.capacity_kg * (
        limits.cold_level_min_pct + limits.handoff_level_guard_pct
    ) / 100.0
    salt.cold_tank.set_level_pct(100.0 * (guard_kg + fast_mass) / salt.cold_tank.capacity_kg)
    mode = select_mode(limits, salt.cold_tank, salt.hot_tank, limits.flow_baseline_kg_s)
    assert mode.mode == FAST
    assert "cold_tank_limiting" in mode.reasons


def test_tanks_at_limits_inhibit_hard_switch(platform):
    _prepare(platform)
    # 冷罐抽到下限，硬切必须被拒绝并给出罐况原因。
    platform.salt.cold_tank.set_level_pct(platform.config.salt.cold_level_min_pct)
    report = platform.changeover()
    assert report["ok"] is False
    assert report["mode"] == INHIBITED
    assert "handoff_inhibited_by_tank_margin" in report["violations"]
    # 拒绝不动设备：冷泵仍在运，热泵未启。
    assert platform.salt.cold_pump.running() is True
    assert platform.salt.hot_pump.running() is False


def test_hot_tank_nearly_full_takes_alternative_route(platform):
    _prepare(platform)
    # 热罐将满：切换禁止，走"别的走法"——回流阀安全位保持，供降负荷／等待恢复。
    platform.salt.hot_tank.set_level_pct(platform.config.salt.hot_level_max_pct)
    report = platform.changeover()
    assert report["ok"] is False
    assert report["outcome"] == "rejected"
    # 再循环旁路是显式可用的安全走法。
    platform.salt.open_recirc()
    for _ in range(20):
        platform.salt.step_recirc(1.0)
    assert platform.salt.recirc.is_open()


def test_fast_mode_uses_steeper_slope_and_shorter_window(platform):
    limits = platform.config.salt
    salt = platform.salt
    _prepare(platform)
    guard_kg = salt.cold_tank.capacity_kg * (
        limits.cold_level_min_pct + limits.handoff_level_guard_pct
    ) / 100.0
    fast_mass = limits.flow_baseline_kg_s * (limits.handoff_fast_window_s + 10.0)
    salt.cold_tank.set_level_pct(100.0 * (guard_kg + fast_mass) / salt.cold_tank.capacity_kg)
    report = platform.changeover()
    assert report["ok"] is True
    assert report["mode"] == FAST
    assert report["duration_s"] <= limits.handoff_fast_window_s + limits.handoff_confirm_s + 30.0


def test_readback_loss_before_commit_rolls_back(platform):
    _prepare(platform)
    # 在热泵建流之前打掉其流量回读：未提交 → 退回原状（冷泵恢复、回流阀打开）。
    platform.salt.hot_pump.inject_flow_fault(True)
    report = platform.changeover()
    assert report["ok"] is False
    assert report["outcome"] == "rolled_back"
    assert any("readback" in item for item in report["violations"])
    assert platform.salt.cold_pump.running() is True
    assert platform.salt.hot_pump.running() is False
    assert platform.salt.recirc.is_open()


def test_readback_loss_after_commit_advances_to_safe_position(platform):
    _prepare(platform)
    handoff = platform.salt.handoff
    # 推进到交叉中段（已过提交点）再让冷泵回读冻结：已提交 → 进泵单泵安全位
    # 或升级全停安全保持，出泵绝不能继续压在母管上。
    platform.begin_changeover()
    ticks = 0
    while handoff.state() != "crossing" and ticks < 400:
        platform.changeover_step(1.0)
        ticks += 1
    assert handoff.state() == "crossing"
    platform.salt.cold_pump.inject_stuck(True)
    while handoff.active():
        platform.changeover_step(1.0)
    record = handoff.record()
    assert record.outcome in ("completed", "safe_hold")
    assert platform.salt.cold_pump.running() is False


def test_window_watchdog_times_out_to_safe_position(platform):
    _prepare(platform)
    # 让进泵在交叉阶段永远跟不上份额（卡死），窗口必须触发退回而非时间到硬切。
    handoff = platform.salt.handoff
    platform.begin_changeover()
    ticks = 0
    while handoff.state() != "crossing" and ticks < 400:
        platform.changeover_step(1.0)
        ticks += 1
    platform.salt.hot_pump.inject_stuck(True)
    while handoff.active():
        platform.changeover_step(1.0)
    record = handoff.record()
    assert record.ok is False
    assert any("window_exceeded" in item or "readback" in item for item in record.violations)


def test_overshoot_is_recorded_every_run(platform):
    _prepare(platform)
    first = platform.changeover()
    # 立刻再切一次需要反向条件，这里只验证台账：每次切换都有一条同构记录。
    journal = platform.salt.handoff_journal
    assert journal.count() == 1
    stored = journal.latest()
    for key in (
        "flow_undershoot_kg_s",
        "flow_overshoot_kg_s",
        "pressure_undershoot_mpa",
        "pressure_overshoot_mpa",
        "duration_s",
        "mode",
        "outcome",
    ):
        assert key in stored.as_dict()
    assert stored.handoff_id == first["handoff_id"]


def test_journal_and_audit_share_the_same_handoff_id(platform):
    _prepare(platform)
    report = platform.changeover()
    op_id = report["handoff_id"]
    journal = platform.salt.handoff_journal
    assert journal.by_id(op_id) is not None
    # 审计流水里同一编号下有开始、阶段与终态事件。
    entries = platform.audit.entries(limit=400)
    changeover_events = [
        item for item in entries if str(item["payload"].get("handoff_id", "")) == op_id
    ]
    actions = {item["action"] for item in changeover_events}
    assert "handoff_begin" in actions
    assert "changeover_complete" in actions
    # 审计里汇总的成功与否与台账一致。
    assert journal.by_id(op_id).ok is report["ok"]


def test_live_changeover_is_observable_on_each_step(platform):
    _prepare(platform)
    platform.begin_changeover()
    seen_states = set()
    while platform.salt.handoff.active():
        payload = platform.changeover_step(1.0)
        seen_states.add(payload["state"])
    assert "crossing" in seen_states
    assert "settling" in seen_states


def test_manual_hot_start_still_rejected_while_cold_running(platform):
    platform.start()
    platform.salt.start_cold()
    with pytest.raises(Exception):
        platform.salt.start_hot()


def test_journal_audit_reconciliation_endpoint(platform):
    from cspplant.console.api import Api

    _prepare(platform)
    platform.changeover()
    api = Api(platform)
    code, body = api.handle("GET", "/api/handoffs/verify", {}, {})
    assert code == 200
    assert body["ok"] is True
    assert body["missing_in_journal"] == []
    assert body["audit_handoff_ids"] >= 1


def test_high_load_normal_switch_does_not_collapse_pressure(platform):
    _prepare(platform)
    platform.dispatch(100.0, "fixture")
    for _ in range(30):
        platform.cycle(1.0)
    report = platform.changeover(load_mw=30.0)
    assert report["ok"] is True
    assert report["load_mw"] == 30.0
    assert report["pressure_min_mpa"] > 1.5
    assert report["flow_min_kg_s"] >= platform.config.salt.flow_baseline_kg_s - 4.0

