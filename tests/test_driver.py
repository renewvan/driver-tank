"""Fixture-sequence tests for the flow-rate and full/empty pure state
machines, and for date-command validation -- no MQTT broker or ADC.

Prior art: tests/test_calibration.py (fixture input, pure function, no I/O).
"""
import json

from node_tank.calibration import Calibration
from node_tank.config import TankConfig
from node_tank.driver import (
    FlowState,
    FullEmptyState,
    TankState,
    _compute_flow_rates,
    _compute_full_empty_state,
    _handle_date_set_command,
    _validate_date_str,
)


def make_tank(
    *,
    capacity_l: float = 100.0,
    flow_min_delta_pct: float = 0.3,
    flow_idle_timeout_s: float = 30.0,
    full_threshold_pct: float = 99.0,
    empty_threshold_pct: float = 1.0,
    alarm_delay_s: float = 0.0,
) -> TankConfig:
    return TankConfig(
        id="fresh",
        name="Fresh Water Tank",
        channel=0,
        fluid_type="fresh_water",
        capacity_l=capacity_l,
        update_interval_ms=3000,
        calibration=Calibration(fixed_resistor=220, reference_voltage=3.3, sensor_min=0.0, sensor_max=190.0),
        alarm_direction=None,
        alarm_threshold=None,
        alarm_restore=None,
        alarm_delay_s=alarm_delay_s,
        temp_sensor_id=None,
        flow_min_delta_pct=flow_min_delta_pct,
        flow_idle_timeout_s=flow_idle_timeout_s,
        full_threshold_pct=full_threshold_pct,
        empty_threshold_pct=empty_threshold_pct,
    )


# --- _compute_flow_rates --------------------------------------------------


def test_first_read_publishes_zero_before_any_edge():
    tank = make_tank()
    fill, drain, state = _compute_flow_rates(tank, 50.0, now=1000.0, flow_state=FlowState())
    assert (fill, drain) == (0.0, 0.0)
    assert state.edge_level_pct == 50.0
    assert state.edge_time == 1000.0


def test_still_level_publishes_zero_zero():
    tank = make_tank()
    state = FlowState(edge_level_pct=50.0, edge_time=1000.0)
    fill, drain, state = _compute_flow_rates(tank, 50.0, now=1010.0, flow_state=state)
    assert (fill, drain) == (0.0, 0.0)


def test_qualifying_rise_publishes_fill_rate_and_zeroes_drain():
    tank = make_tank(capacity_l=100.0, flow_min_delta_pct=0.3)
    state = FlowState(edge_level_pct=50.0, edge_time=0.0)
    # +10% over 60s -> 10/100*100L / 1min = 10 L/min
    fill, drain, state = _compute_flow_rates(tank, 60.0, now=60.0, flow_state=state)
    assert fill == 10.0
    assert drain == 0.0
    assert state.edge_level_pct == 60.0
    assert state.edge_time == 60.0
    assert state.last_fill_rate == 10.0


def test_qualifying_fall_publishes_drain_rate_and_zeroes_fill():
    tank = make_tank(capacity_l=100.0, flow_min_delta_pct=0.3)
    state = FlowState(edge_level_pct=60.0, edge_time=0.0)
    fill, drain, state = _compute_flow_rates(tank, 50.0, now=60.0, flow_state=state)
    assert fill == 0.0
    assert drain == 10.0
    assert state.last_drain_rate == 10.0


def test_subthreshold_delta_does_not_move_edge_or_change_rate():
    tank = make_tank(flow_min_delta_pct=0.5)
    state = FlowState(edge_level_pct=50.0, edge_time=0.0, last_fill_rate=3.0, last_drain_rate=0.0)
    fill, drain, new_state = _compute_flow_rates(tank, 50.2, now=5.0, flow_state=state)
    assert (fill, drain) == (3.0, 0.0)
    assert new_state.edge_level_pct == 50.0
    assert new_state.edge_time == 0.0


def test_idle_timeout_zeroes_rates_without_moving_edge():
    tank = make_tank(flow_idle_timeout_s=30.0, flow_min_delta_pct=0.5)
    state = FlowState(edge_level_pct=50.0, edge_time=0.0, last_fill_rate=5.0, last_drain_rate=0.0)
    fill, drain, new_state = _compute_flow_rates(tank, 50.1, now=31.0, flow_state=state)
    assert (fill, drain) == (0.0, 0.0)
    assert new_state.edge_level_pct == 50.0
    assert new_state.edge_time == 0.0


def test_resumed_flow_after_idle_timeout_measured_from_last_real_edge():
    tank = make_tank(capacity_l=100.0, flow_min_delta_pct=0.3, flow_idle_timeout_s=30.0)
    state = FlowState(edge_level_pct=50.0, edge_time=0.0)
    # Idle timeout fires at t=31, edge stays at (50.0, 0.0).
    _, _, state = _compute_flow_rates(tank, 50.1, now=31.0, flow_state=state)
    # Flow resumes: level rises to 60% at t=60 -> measured against the edge at t=0,
    # not against the idle-timeout instant.
    fill, drain, state = _compute_flow_rates(tank, 60.0, now=60.0, flow_state=state)
    assert fill == 10.0
    assert drain == 0.0
    assert state.edge_time == 60.0


# --- _compute_full_empty_state --------------------------------------------


def test_sustained_crossing_into_full_band_commits_once():
    tank = make_tank(full_threshold_pct=99.0, alarm_delay_s=5.0)
    state = FullEmptyState()
    # Enter the band: starts the timer, no commit yet.
    full, empty, state = _compute_full_empty_state(tank, 99.5, now=0.0, full_empty_state=state)
    assert full is None
    assert state.full_delay_start == 0.0
    assert state.full_latched is False
    # Delay elapses while still in band: commits.
    full, empty, state = _compute_full_empty_state(tank, 99.5, now=5.0, full_empty_state=state)
    assert full == __import__("datetime").date.today().isoformat()
    assert state.full_latched is True


def test_retreat_before_delay_cancels_pending_commit():
    tank = make_tank(full_threshold_pct=99.0, alarm_delay_s=5.0)
    state = FullEmptyState()
    full, empty, state = _compute_full_empty_state(tank, 99.5, now=0.0, full_empty_state=state)
    assert full is None
    # Retreat out of band before the delay elapses.
    full, empty, state = _compute_full_empty_state(tank, 90.0, now=2.0, full_empty_state=state)
    assert full is None
    assert state.full_delay_start is None
    assert state.full_latched is False
    # Even past the original delay window, no commit happens because the timer was cancelled.
    full, empty, state = _compute_full_empty_state(tank, 90.0, now=10.0, full_empty_state=state)
    assert full is None


def test_staying_latched_does_not_recommit():
    tank = make_tank(full_threshold_pct=99.0, alarm_delay_s=0.0)
    state = FullEmptyState()
    full, empty, state = _compute_full_empty_state(tank, 99.5, now=0.0, full_empty_state=state)
    assert full is not None
    assert state.full_latched is True
    # Repeated in-band reads: no re-commit.
    full, empty, state = _compute_full_empty_state(tank, 99.9, now=1.0, full_empty_state=state)
    assert full is None
    full, empty, state = _compute_full_empty_state(tank, 100.0, now=2.0, full_empty_state=state)
    assert full is None


def test_leaving_and_reentering_band_rearms_and_can_commit_again():
    tank = make_tank(full_threshold_pct=99.0, alarm_delay_s=0.0)
    state = FullEmptyState()
    full, empty, state = _compute_full_empty_state(tank, 99.5, now=0.0, full_empty_state=state)
    assert full is not None
    # Leave the band: re-arms.
    full, empty, state = _compute_full_empty_state(tank, 90.0, now=1.0, full_empty_state=state)
    assert full is None
    assert state.full_latched is False
    # Re-enter: commits again.
    full, empty, state = _compute_full_empty_state(tank, 99.5, now=2.0, full_empty_state=state)
    assert full is not None


def test_zero_delay_commits_immediately_on_crossing():
    tank = make_tank(empty_threshold_pct=1.0, alarm_delay_s=0.0)
    state = FullEmptyState()
    full, empty, state = _compute_full_empty_state(tank, 0.5, now=0.0, full_empty_state=state)
    assert empty is not None
    assert state.empty_latched is True


def test_full_and_empty_latches_are_independent():
    tank = make_tank(full_threshold_pct=99.0, empty_threshold_pct=1.0, alarm_delay_s=0.0)
    state = FullEmptyState()
    full, empty, state = _compute_full_empty_state(tank, 99.5, now=0.0, full_empty_state=state)
    assert full is not None
    assert empty is None
    assert state.empty_latched is False


# --- date validation / command handling -----------------------------------


def test_validate_date_str_accepts_well_formed_past_date():
    assert _validate_date_str("2020-01-01") is True


def test_validate_date_str_rejects_malformed_string():
    assert _validate_date_str("not-a-date") is False
    assert _validate_date_str("2020/01/01") is False


def test_validate_date_str_rejects_future_date():
    future_year = __import__("datetime").date.today().year + 5
    assert _validate_date_str(f"{future_year}-01-01") is False


class _FakePublisher:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))


def test_handle_date_set_command_rejects_malformed_date(monkeypatch, tmp_path):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    tank = make_tank()
    tank_state = TankState()
    publisher = _FakePublisher()
    _handle_date_set_command(publisher, tank, tank_state, "last_full_date", json.dumps("not-a-date"))
    assert publisher.published == []
    assert tank_state.last_full_date is None


def test_handle_date_set_command_rejects_future_date(monkeypatch, tmp_path):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    future_year = __import__("datetime").date.today().year + 5
    tank = make_tank()
    tank_state = TankState()
    publisher = _FakePublisher()
    _handle_date_set_command(
        publisher, tank, tank_state, "last_empty_date", json.dumps(f"{future_year}-01-01")
    )
    assert publisher.published == []
    assert tank_state.last_empty_date is None


def test_handle_date_set_command_accepts_and_persists_valid_date(monkeypatch, tmp_path):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    tank = make_tank()
    tank_state = TankState()
    publisher = _FakePublisher()
    _handle_date_set_command(publisher, tank, tank_state, "last_full_date", json.dumps("2020-06-15"))
    assert tank_state.last_full_date == "2020-06-15"
    assert publisher.published == [("renewvan/tank/fresh/last_full_date", json.dumps("2020-06-15"))]
    # Single round-trip persistence check (not exhaustive file-format testing).
    assert driver_mod._load_state_field("fresh", "last_full_date") == "2020-06-15"
