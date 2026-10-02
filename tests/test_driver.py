"""Fixture-sequence tests for the full/empty pure state
machine, and for timestamp-command validation -- no MQTT broker or ADC.

Prior art: tests/test_calibration.py (fixture input, pure function, no I/O).
"""

import json
from datetime import datetime, timedelta

from node_tank.calibration import Calibration
from node_tank.config import TankConfig
from node_tank.driver import (
    FullEmptyState,
    TankState,
    _compute_full_empty_state,
    _compute_volume_since_latch,
    _handle_timestamp_set_command,
    _migrate_timestamp_field,
    _normalize_timestamp_str,
)


def make_tank(
    *,
    capacity_l: float = 100.0,
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
        calibration=Calibration(
            fixed_resistor=220, reference_voltage=3.3, sensor_min=0.0, sensor_max=190.0
        ),
        alarm_direction=None,
        alarm_threshold=None,
        alarm_restore=None,
        alarm_delay_s=alarm_delay_s,
        temp_sensor_id=None,
        full_threshold_pct=full_threshold_pct,
        empty_threshold_pct=empty_threshold_pct,
    )


# --- _compute_full_empty_state --------------------------------------------


def _assert_is_recent_local_timestamp(value: str) -> None:
    """The committed value is a full ISO-8601 timestamp with a local UTC
    offset, close to real wall-clock "now" (volume-timestamp-telemetry
    ticket 01 -- committed via datetime.now().astimezone(), not derived
    from the synthetic `now` float the tests pass in)."""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert abs(parsed - datetime.now().astimezone()) < timedelta(seconds=5)


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
    _assert_is_recent_local_timestamp(full)
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


# --- timestamp validation / command handling -------------------------------


def test_normalize_timestamp_str_accepts_full_timestamp():
    value = "2020-01-01T10:00:00-05:00"
    assert _normalize_timestamp_str(value) == value


def test_normalize_timestamp_str_accepts_bare_date_and_normalizes_to_local_midnight():
    normalized = _normalize_timestamp_str("2020-01-01")
    parsed = datetime.fromisoformat(normalized)
    assert parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == (2020, 1, 1)
    assert (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0)


def test_normalize_timestamp_str_rejects_malformed_string():
    assert _normalize_timestamp_str("not-a-date") is None
    assert _normalize_timestamp_str("2020/01/01") is None


def test_normalize_timestamp_str_rejects_future_value():
    future_year = datetime.now().year + 5
    assert _normalize_timestamp_str(f"{future_year}-01-01") is None
    assert _normalize_timestamp_str(f"{future_year}-01-01T00:00:00+00:00") is None


class _FakePublisher:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))


def test_handle_timestamp_set_command_rejects_malformed_value(monkeypatch, tmp_path):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    tank = make_tank()
    tank_state = TankState()
    publisher = _FakePublisher()
    _handle_timestamp_set_command(
        publisher, tank, tank_state, "last_full_at", json.dumps("not-a-date")
    )
    assert publisher.published == []
    assert tank_state.last_full_at is None


def test_handle_timestamp_set_command_rejects_future_value(monkeypatch, tmp_path):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    future_year = datetime.now().year + 5
    tank = make_tank()
    tank_state = TankState()
    publisher = _FakePublisher()
    _handle_timestamp_set_command(
        publisher, tank, tank_state, "last_empty_at", json.dumps(f"{future_year}-01-01")
    )
    assert publisher.published == []
    assert tank_state.last_empty_at is None


def test_handle_timestamp_set_command_accepts_bare_date_and_persists_normalized(
    monkeypatch, tmp_path
):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    tank = make_tank()
    tank_state = TankState()
    publisher = _FakePublisher()
    _handle_timestamp_set_command(
        publisher, tank, tank_state, "last_full_at", json.dumps("2020-06-15")
    )
    parsed = datetime.fromisoformat(tank_state.last_full_at)
    assert (parsed.year, parsed.month, parsed.day) == (2020, 6, 15)
    assert parsed.tzinfo is not None
    assert publisher.published == [
        ("renewvan/tank/fresh/last_full_at", json.dumps(tank_state.last_full_at))
    ]
    # Single round-trip persistence check (not exhaustive file-format testing).
    assert driver_mod._load_state_field("fresh", "last_full_at") == tank_state.last_full_at


def test_handle_timestamp_set_command_accepts_full_timestamp(monkeypatch, tmp_path):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    tank = make_tank()
    tank_state = TankState()
    publisher = _FakePublisher()
    value = "2020-06-15T08:30:00-04:00"
    _handle_timestamp_set_command(publisher, tank, tank_state, "last_empty_at", json.dumps(value))
    assert tank_state.last_empty_at == value
    assert publisher.published == [("renewvan/tank/fresh/last_empty_at", json.dumps(value))]


# --- state-file upgrade path (_migrate_timestamp_field) -------------------


def test_migrate_timestamp_field_upgrades_legacy_bare_date_in_place(monkeypatch, tmp_path):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    state_file = tmp_path / "fresh.json"
    state_file.write_text(json.dumps({"last_full_date": "2020-06-15"}))

    result = _migrate_timestamp_field("fresh", "last_full_at")

    parsed = datetime.fromisoformat(result)
    assert (parsed.year, parsed.month, parsed.day) == (2020, 6, 15)
    assert parsed.tzinfo is not None

    on_disk = json.loads(state_file.read_text())
    assert on_disk["last_full_at"] == result
    assert "last_full_date" not in on_disk


def test_migrate_timestamp_field_reads_new_key_directly_when_already_upgraded(
    monkeypatch, tmp_path
):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    state_file = tmp_path / "fresh.json"
    value = "2021-03-01T09:00:00-05:00"
    state_file.write_text(json.dumps({"last_full_at": value}))

    assert _migrate_timestamp_field("fresh", "last_full_at") == value


def test_migrate_timestamp_field_returns_none_when_no_state_file(tmp_path, monkeypatch):
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    assert _migrate_timestamp_field("fresh", "last_full_at") is None


# --- _compute_volume_since_latch -------------------------------------------


def test_volume_since_latch_is_zero_at_the_commit_level():
    tank = make_tank(capacity_l=100.0)
    full, empty = _compute_volume_since_latch(
        tank, level_pct=99.5, level_pct_at_full_commit=99.5, level_pct_at_empty_commit=None
    )
    assert full == 0.0
    assert empty == 0.0


def test_volume_since_latch_computes_net_delta_from_full_commit():
    tank = make_tank(capacity_l=100.0)
    # Drained 20 points off a full commit at 99.5 -> 20% of 100L = 20L.
    full, empty = _compute_volume_since_latch(
        tank, level_pct=79.5, level_pct_at_full_commit=99.5, level_pct_at_empty_commit=None
    )
    assert full == 20.0
    assert empty == 0.0


def test_volume_since_latch_computes_net_delta_from_empty_commit():
    tank = make_tank(capacity_l=100.0)
    # Filled 15 points up from an empty commit at 0.5 -> 15% of 100L = 15L.
    full, empty = _compute_volume_since_latch(
        tank, level_pct=15.5, level_pct_at_full_commit=None, level_pct_at_empty_commit=0.5
    )
    assert full == 0.0
    assert empty == 15.0


def test_volume_since_latch_partial_top_off_shrinks_the_figure():
    tank = make_tank(capacity_l=100.0)
    # Drained to 70L used, then topped off back up to 10L used off the same
    # full-commit anchor -- shrinks, no separate reset-on-refill bookkeeping.
    drained, _ = _compute_volume_since_latch(
        tank, level_pct=30.0, level_pct_at_full_commit=100.0, level_pct_at_empty_commit=None
    )
    topped_off, _ = _compute_volume_since_latch(
        tank, level_pct=90.0, level_pct_at_full_commit=100.0, level_pct_at_empty_commit=None
    )
    assert drained == 70.0
    assert topped_off == 10.0
    assert topped_off < drained


def test_volume_since_latch_is_zero_with_no_anchor_yet():
    tank = make_tank(capacity_l=100.0)
    # Covers both "never yet full/empty" and the upgrade case (a state file
    # with last_full_at but no level_pct_at_full_commit).
    full, empty = _compute_volume_since_latch(
        tank, level_pct=55.0, level_pct_at_full_commit=None, level_pct_at_empty_commit=None
    )
    assert (full, empty) == (0.0, 0.0)


# --- volume-since-latch restart durability (read_and_publish integration) -


class _FakeADC:
    def __init__(self, voltage: float):
        self.voltage = voltage

    def read_voltage(self, channel: int, pga) -> float:
        return self.voltage


def test_volume_since_full_l_survives_a_restart(monkeypatch, tmp_path):
    """A fresh TankState seeded with a persisted level_pct_at_full_commit/
    last_full_at pair (simulating a restart) reproduces the same
    volume_since_full_l a live process would have shown pre-restart --
    the anchor is identity data (persisted), not process-lifetime state."""
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    tank = make_tank(capacity_l=100.0)

    # Pre-restart: a tank that committed full at level_pct=99.5, then drained.
    pre_restart_state = TankState(
        last_full_at="2020-06-15T00:00:00-04:00",
        level_pct_at_full_commit=99.5,
    )
    pre_restart_volume, _ = _compute_volume_since_latch(
        tank,
        level_pct=70.0,
        level_pct_at_full_commit=pre_restart_state.level_pct_at_full_commit,
        level_pct_at_empty_commit=None,
    )

    # Simulate the restart: state/<tank_id>.json holds the persisted anchor,
    # a fresh TankState is constructed and loaded the way run() does.
    driver_mod._save_state_field("fresh", "last_full_at", "2020-06-15T00:00:00-04:00")
    driver_mod._save_state_field("fresh", "level_pct_at_full_commit", 99.5)
    post_restart_state = TankState()
    post_restart_state.last_full_at = driver_mod._migrate_timestamp_field("fresh", "last_full_at")
    post_restart_state.level_pct_at_full_commit = driver_mod._load_state_field(
        "fresh", "level_pct_at_full_commit"
    )

    publisher = _FakePublisher()
    adc = _FakeADC(voltage=0.0)  # irrelevant: Calibration.read is stubbed below
    from node_tank.calibration import Calibration, Status

    monkeypatch.setattr(Calibration, "read", lambda self, voltage: (70.0, Status.OK))
    driver_mod.read_and_publish(publisher, adc, tank, post_restart_state, {}, now=0.0)

    published = dict(publisher.published)
    post_restart_volume = json.loads(published["renewvan/tank/fresh/volume_since_full_l"])
    assert post_restart_volume == round(pre_restart_volume, 2)


def test_volume_since_full_l_is_zero_when_upgrading_without_an_anchor(monkeypatch, tmp_path):
    """A state file with last_full_at but no level_pct_at_full_commit (an
    install upgrading from before this feature existed) publishes 0 until
    the next latch commit re-establishes both keys together."""
    import node_tank.driver as driver_mod

    monkeypatch.setattr(driver_mod, "_STATE_DIR", tmp_path)
    tank = make_tank(capacity_l=100.0, full_threshold_pct=99.0)

    driver_mod._save_state_field("fresh", "last_full_at", "2020-06-15T00:00:00-04:00")
    tank_state = TankState()
    tank_state.last_full_at = driver_mod._migrate_timestamp_field("fresh", "last_full_at")
    tank_state.level_pct_at_full_commit = driver_mod._load_state_field(
        "fresh", "level_pct_at_full_commit"
    )
    assert tank_state.level_pct_at_full_commit is None

    publisher = _FakePublisher()
    adc = _FakeADC(voltage=0.0)  # irrelevant: Calibration.read is stubbed below
    from node_tank.calibration import Calibration, Status

    monkeypatch.setattr(Calibration, "read", lambda self, voltage: (70.0, Status.OK))
    driver_mod.read_and_publish(publisher, adc, tank, tank_state, {}, now=0.0)

    published = dict(publisher.published)
    assert json.loads(published["renewvan/tank/fresh/volume_since_full_l"]) == 0.0
