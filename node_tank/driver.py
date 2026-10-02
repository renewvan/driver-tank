"""Publish loop: ADC read -> calibration math -> renewvan/tank/<id>/<property>.

Field/topic shape per hub's schema/tank.schema.json and
docs/porting-dbus-to-mqtt-node.md: fluid_type/capacity_l published
retained at startup (identity fields, rarely change), level_pct/status
republished retained on every read (live fields).

Additional publishers per spec (tickets 02, 05, 06):
- alarm_state: live field if alarm is configured
- temperature_c: live field if DS18B20 is configured
- last_inspected_at: identity field, command via /set topic (unretained)

Persistence boundary (ticket 06): config.py's config.ini is install-time
and load-only, never machine-written (see config.py's docstring). This
module introduces a *separate*, machine-written state/<tank_id>.json
directory for the one piece of runtime state that must survive a
restart (last_inspected_at) -- distinct from config, never read by
config.py, gitignored like config.ini.

Timestamp precision (volume-timestamp-telemetry ticket 01): last_full_at/
last_empty_at/last_inspected_at are full ISO-8601 timestamps with the
system's local UTC offset (not bare YYYY-MM-DD dates) -- renamed from
last_full_date/last_empty_date/last_inspected_date, which this module no
longer publishes. State files written by older versions (bare dates
under the old key names) are upgraded in place on first read; see
_migrate_timestamp_field.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

from node_tank.adc import ADS1115
from node_tank.config import AppConfig, TankConfig
from node_tank.publisher import Publisher
from node_tank.temperature import DS18B20

logger = logging.getLogger(__name__)

_PGA_DEFAULT = 4.096

# State directory for machine-written persistence (ticket 06)
_STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# Renamed field pairs (new key -> legacy pre-rename key) for the one-time
# state-file upgrade in _migrate_timestamp_field (volume-timestamp-telemetry
# ticket 01).
_TIMESTAMP_FIELD_LEGACY_KEYS = {
    "last_inspected_at": "last_inspected_date",
    "last_full_at": "last_full_date",
    "last_empty_at": "last_empty_date",
}


@dataclass
class AlarmState:
    """Per-tank alarm state tracking (ticket 02)."""

    previous_state: str = "ok"  # "ok" or "alarm"
    delay_timer_start: float | None = None  # Time when threshold was crossed


@dataclass
class FullEmptyState:
    """Per-tank last-full/last-empty one-shot latch tracking
    (flow-rate-full-empty-telemetry ticket 02)."""

    full_latched: bool = False
    full_delay_start: float | None = None
    empty_latched: bool = False
    empty_delay_start: float | None = None


@dataclass
class TankState:
    """Per-tank runtime state."""

    alarm: AlarmState = field(default_factory=AlarmState)
    last_inspected_at: str | None = None  # Most recent persisted timestamp (ISO-8601, local offset)
    full_empty: FullEmptyState = field(default_factory=FullEmptyState)
    last_full_at: str | None = None  # Most recent persisted timestamp (ISO-8601, local offset)
    last_empty_at: str | None = None  # Most recent persisted timestamp (ISO-8601, local offset)
    # Volume-since-latch anchors (volume-timestamp-telemetry ticket 02): level_pct
    # at the moment last_full_at/last_empty_at last committed. Persisted (unlike
    # FullEmptyState) so volume_since_full_l/volume_since_empty_l survive
    # a restart -- None means "no commit yet" (or a pre-upgrade state file with no
    # anchor), and the derived field publishes 0 until the next commit.
    level_pct_at_full_commit: float | None = None
    level_pct_at_empty_commit: float | None = None


def _topic(tank_id: str, prop: str) -> str:
    return f"renewvan/tank/{tank_id}/{prop}"


def _ensure_state_dir() -> None:
    """Ensure state directory exists for persistence (ticket 06)."""
    _STATE_DIR.mkdir(exist_ok=True, parents=True)


def _read_state_file(tank_id: str) -> dict:
    """Load state/<tank_id>.json as a dict, or {} if absent/unreadable
    (ticket 06). Shared by _load_state_field and _migrate_timestamp_field
    (code-review finding, volume-timestamp-telemetry: both independently
    duplicated this existence-check + parse shape) -- one read path, two
    call sites.
    """
    state_file = _STATE_DIR / f"{tank_id}.json"
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except Exception as e:
        logger.warning(f"Failed to load state for tank={tank_id}: {e}")
        return {}


def _load_state_field(tank_id: str, field_name: str) -> str | float | None:
    """Load a single persisted field from state/<tank_id>.json (ticket 06).

    Generic across last_inspected_at, last_full_at, last_empty_at (str) and
    level_pct_at_full_commit/level_pct_at_empty_commit (float,
    volume-timestamp-telemetry ticket 02) -- all five share one file, one
    read/write helper (flow-rate-full-empty-telemetry ticket 02).
    """
    return _read_state_file(tank_id).get(field_name)


def _save_state_field(tank_id: str, field_name: str, value: str | float) -> None:
    """Persist a single field to state/<tank_id>.json, merging with existing keys.

    Read-modify-write so last_inspected_at/last_full_at/last_empty_at/
    level_pct_at_full_commit/level_pct_at_empty_commit (written independently,
    at different times) don't clobber each other in the shared file
    (flow-rate-full-empty-telemetry ticket 02).
    """
    state_file = _STATE_DIR / f"{tank_id}.json"
    try:
        data = {}
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
            except Exception:
                data = {}
        data[field_name] = value
        state_file.write_text(json.dumps(data))
    except Exception as e:
        logger.error(f"Failed to save state for tank={tank_id}: {e}")


def _normalize_timestamp_str(value: str) -> str | None:
    """Validate and normalize a /set payload or persisted state value to a
    full ISO-8601 timestamp with the system's local UTC offset (replaces
    _validate_date_str; volume-timestamp-telemetry ticket 01).

    Accepts either a full ISO-8601 timestamp (any offset, or bare) or a
    bare YYYY-MM-DD date. A value with no offset (the bare-date case, or a
    bare date+time) is presumed to already be local time and gets the
    system's current UTC offset attached -- no timezone configuration is
    introduced, matching the codebase's existing local-time convention.
    Rejects malformed input and anything after "now".

    Shared by last_inspected_at, last_full_at, last_empty_at /set command
    handling and by the state-file load-time upgrade path (see
    _migrate_timestamp_field) -- one normalization routine, no separate
    date-only vs. timestamp branches elsewhere.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()

    if parsed > datetime.now().astimezone():
        return None

    return parsed.isoformat(timespec="seconds")


def _migrate_timestamp_field(tank_id: str, new_key: str) -> str | None:
    """Load a persisted timestamp field, upgrading a legacy bare-date value
    (under the new key, or a pre-rename legacy key) to a full ISO-8601
    timestamp under the new key, rewriting state/<tank_id>.json in place
    (volume-timestamp-telemetry ticket 01).

    After this runs once per tank_id, only new_key in the new timestamp
    format is ever read again -- no dual-format branches anywhere else in
    the codebase.
    """
    legacy_key = _TIMESTAMP_FIELD_LEGACY_KEYS[new_key]
    state_file = _STATE_DIR / f"{tank_id}.json"
    data = _read_state_file(tank_id)
    if not data:
        return None

    raw = data.get(new_key)
    used_legacy_key = False
    if raw is None:
        raw = data.get(legacy_key)
        used_legacy_key = raw is not None

    if raw is None:
        return None

    normalized = _normalize_timestamp_str(raw)
    if normalized is None:
        logger.warning(f"Tank {tank_id}: unrecognized stored value for {new_key}: {raw!r}")
        return None

    if used_legacy_key or normalized != raw or legacy_key in data:
        data[new_key] = normalized
        data.pop(legacy_key, None)
        try:
            state_file.write_text(json.dumps(data))
        except Exception as e:
            logger.error(f"Failed to save state for tank={tank_id}: {e}")

    return normalized


def publish_identity(publisher: Publisher, tank: TankConfig, tank_state: TankState) -> None:
    """Publish identity fields at startup (ticket 01, 06; hub schema v0.5 alarm config fields).

    Identity fields (retained, published once at startup):
    - fluid_type, capacity_l (always)
    - alarm_direction, alarm_threshold_pct, alarm_restore_pct (only if alarm is configured --
      same condition as alarm_state's live publication)
    - last_inspected_at, last_full_at, last_empty_at (only if loaded from state file)
    """
    publisher.publish(_topic(tank.id, "fluid_type"), json.dumps(tank.fluid_type))
    publisher.publish(_topic(tank.id, "capacity_l"), json.dumps(tank.capacity_l))

    if tank.alarm_direction is not None:
        publisher.publish(_topic(tank.id, "alarm_direction"), json.dumps(tank.alarm_direction))
        publisher.publish(_topic(tank.id, "alarm_threshold_pct"), json.dumps(tank.alarm_threshold))
        publisher.publish(_topic(tank.id, "alarm_restore_pct"), json.dumps(tank.alarm_restore))

    # Republish persisted identity timestamps if they were loaded from state (ticket 06;
    # last_full_at/last_empty_at added by flow-rate-full-empty-telemetry ticket 02,
    # renamed from *_date by volume-timestamp-telemetry ticket 01)
    if tank_state.last_inspected_at is not None:
        publisher.publish(
            _topic(tank.id, "last_inspected_at"), json.dumps(tank_state.last_inspected_at)
        )
    if tank_state.last_full_at is not None:
        publisher.publish(_topic(tank.id, "last_full_at"), json.dumps(tank_state.last_full_at))
    if tank_state.last_empty_at is not None:
        publisher.publish(_topic(tank.id, "last_empty_at"), json.dumps(tank_state.last_empty_at))


def _crossed(direction: str, level_pct: float, boundary: float, entering_alarm: bool) -> bool:
    """True if level_pct has crossed past boundary for this transition.

    Entering alarm moves *toward* the configured direction (low: <=,
    high: >=); restoring to ok moves back the *opposite* way (low: >=,
    high: <=) -- entry and restore boundaries are approached from
    opposite sides, so the comparison flips between them.
    """
    if direction == "low":
        return level_pct <= boundary if entering_alarm else level_pct >= boundary
    return level_pct >= boundary if entering_alarm else level_pct <= boundary


def _compute_alarm_state(
    tank: TankConfig,
    level_pct: float,
    status: str,
    alarm_state_obj: AlarmState,
    now: float,
) -> tuple[str, AlarmState]:
    """Compute alarm state with hysteresis + optional delay (ticket 02).

    Sensor faults force alarm immediately. Otherwise a pending transition
    (ok->alarm on crossing alarm_threshold, or alarm->ok on crossing
    alarm_restore) is deferred by alarm_delay_s if set, and cancelled if
    the level moves back off the crossed boundary before the delay
    elapses. Delay applies symmetrically to both transition directions.

    Returns:
        (new_alarm_state_str, updated_AlarmState)
    """
    if status in ("open_circuit", "short_circuit"):
        return "alarm", AlarmState(previous_state="alarm")

    if tank.alarm_direction is None:
        return alarm_state_obj.previous_state, alarm_state_obj

    previous = alarm_state_obj.previous_state
    entering_alarm = previous == "ok"
    boundary = tank.alarm_threshold if entering_alarm else tank.alarm_restore
    target_state = "alarm" if entering_alarm else "ok"
    pending = _crossed(tank.alarm_direction, level_pct, boundary, entering_alarm)

    if not pending:
        # Not past the relevant boundary: no pending transition, clear any timer.
        return previous, AlarmState(previous_state=previous)

    delay_start = (
        now if alarm_state_obj.delay_timer_start is None else alarm_state_obj.delay_timer_start
    )

    if tank.alarm_delay_s > 0 and (now - delay_start) < tank.alarm_delay_s:
        # Still within delay window: hold current state, keep timer running.
        return previous, AlarmState(previous_state=previous, delay_timer_start=delay_start)

    # Delay elapsed (or no delay configured): commit the transition.
    return target_state, AlarmState(previous_state=target_state)


def _compute_one_shot_latch(
    in_band: bool,
    latched: bool,
    delay_start: float | None,
    now: float,
    delay_s: float,
) -> tuple[str | None, bool, float | None]:
    """One-shot latch: commits the current timestamp once per sustained
    excursion into a band (flow-rate-full-empty-telemetry ticket 02;
    timestamp -- not bare date -- as of volume-timestamp-telemetry ticket
    01). Shared building block for both the full-direction and
    empty-direction latches in _compute_full_empty_state -- mirrors how
    _crossed() is shared by both alarm directions in _compute_alarm_state.

    Out of band: clears latch/timer (re-arms). In band, not latched: starts
    the timer on first entry, commits once delay_s elapses (or immediately
    if delay_s <= 0). In band, already latched: no-op. Leaving before the
    delay elapses (i.e. in_band=False while a timer was pending) cancels
    the pending commit -- same cancel-on-retreat behavior as _compute_alarm_state.

    The committed value is real wall-clock time (datetime.now(), with the
    system's local UTC offset attached), not derived from the `now: float`
    parameter -- `now` only drives the delay-timer arithmetic above, same
    split the prior date.today()-based implementation used.

    Returns:
        (committed_timestamp_str | None, new_latched, new_delay_start)
    """
    if not in_band:
        return None, False, None
    if latched:
        return None, True, delay_start
    delay_start = now if delay_start is None else delay_start
    if delay_s <= 0 or (now - delay_start) >= delay_s:
        return datetime.now().astimezone().isoformat(timespec="seconds"), True, None
    return None, False, delay_start


def _compute_full_empty_state(
    tank: TankConfig,
    level_pct: float,
    now: float,
    full_empty_state: FullEmptyState,
) -> tuple[str | None, str | None, FullEmptyState]:
    """Compute last_full_at/last_empty_at one-shot latches
    (flow-rate-full-empty-telemetry ticket 02).

    Two independent latches, one per direction, both delegating to
    _compute_one_shot_latch.

    Returns:
        (last_full_at | None, last_empty_at | None, updated_FullEmptyState)
        -- the timestamp fields are only non-None on the read where a commit happens.
    """
    committed_full, full_latched, full_delay_start = _compute_one_shot_latch(
        in_band=level_pct >= tank.full_threshold_pct,
        latched=full_empty_state.full_latched,
        delay_start=full_empty_state.full_delay_start,
        now=now,
        delay_s=tank.alarm_delay_s,
    )
    committed_empty, empty_latched, empty_delay_start = _compute_one_shot_latch(
        in_band=level_pct <= tank.empty_threshold_pct,
        latched=full_empty_state.empty_latched,
        delay_start=full_empty_state.empty_delay_start,
        now=now,
        delay_s=tank.alarm_delay_s,
    )
    return (
        committed_full,
        committed_empty,
        FullEmptyState(
            full_latched=full_latched,
            full_delay_start=full_delay_start,
            empty_latched=empty_latched,
            empty_delay_start=empty_delay_start,
        ),
    )


def _compute_volume_since_latch(
    tank: TankConfig,
    level_pct: float,
    level_pct_at_full_commit: float | None,
    level_pct_at_empty_commit: float | None,
) -> tuple[float, float]:
    """Compute volume_since_full_l/volume_since_empty_l -- net liters moved
    since each direction's own last full/empty latch commit
    (volume-timestamp-telemetry ticket 02).

    Distinct from an edge-to-edge instantaneous rate: this is a running
    total against a fixed anchor -- the level_pct at
    the moment the tank was last genuinely full/empty, captured by the
    caller (read_and_publish) the instant _compute_full_empty_state
    reports a commit -- not an edge-to-edge rate. No internal state of
    its own: a pure function of current level and the two persisted
    anchors. A partial top-off mid-drain correctly shrinks
    volume_since_full_l (level_pct moves back toward the anchor) rather
    than needing separate reset-on-refill logic.

    0 when no commit of that direction has ever happened (anchor is
    None) -- covers both "never yet full/empty" and the upgrade case (a
    state file with last_full_at but no level_pct_at_full_commit, from
    before this feature existed).

    Returns:
        (volume_since_full_l, volume_since_empty_l)
    """

    def _volume(anchor: float | None) -> float:
        if anchor is None:
            return 0.0
        return abs(level_pct - anchor) / 100.0 * tank.capacity_l

    return _volume(level_pct_at_full_commit), _volume(level_pct_at_empty_commit)


def read_and_publish(
    publisher: Publisher,
    adc: ADS1115,
    tank: TankConfig,
    tank_state: TankState,
    temperature_sensors: dict[str, DS18B20],
    now: float,
) -> None:
    """Read sensors and publish live fields (ticket 01, 02, 05;
    volume-timestamp-telemetry ticket 02).

    Live fields (retained, republished on every read):
    - level_pct, status (always)
    - volume_since_full_l, volume_since_empty_l (always)
    - alarm_state (if alarm is configured)
    - temperature_c (if DS18B20 is configured)
    Identity fields republished only when they change:
    - last_full_at, last_empty_at (auto-detected on a sustained full/empty crossing)
    """
    # Read level from ADS1115
    voltage = adc.read_voltage(tank.channel, _PGA_DEFAULT)
    level_pct, status = tank.calibration.read(voltage)
    publisher.publish(_topic(tank.id, "level_pct"), json.dumps(round(level_pct, 1)))
    publisher.publish(_topic(tank.id, "status"), json.dumps(status.value))

    # Compute and publish alarm state (ticket 02)
    new_alarm_state, updated_alarm_state = _compute_alarm_state(
        tank, level_pct, status.value, tank_state.alarm, now
    )
    tank_state.alarm = updated_alarm_state
    if tank.alarm_direction is not None:
        publisher.publish(_topic(tank.id, "alarm_state"), json.dumps(new_alarm_state))

    # Compute last_full_at/last_empty_at auto-detection (flow-rate-full-empty-telemetry ticket 02)
    committed_full, committed_empty, updated_full_empty_state = _compute_full_empty_state(
        tank, level_pct, now, tank_state.full_empty
    )
    tank_state.full_empty = updated_full_empty_state
    if committed_full is not None:
        _save_state_field(tank.id, "last_full_at", committed_full)
        tank_state.last_full_at = committed_full
        publisher.publish(_topic(tank.id, "last_full_at"), json.dumps(committed_full))
        logger.info(f"Tank {tank.id}: last_full_at auto-detected as {committed_full}")
        # Anchor for volume_since_full_l is the level_pct at this exact commit
        # (volume-timestamp-telemetry ticket 02) -- persisted so it survives a restart.
        tank_state.level_pct_at_full_commit = level_pct
        _save_state_field(tank.id, "level_pct_at_full_commit", level_pct)
    if committed_empty is not None:
        _save_state_field(tank.id, "last_empty_at", committed_empty)
        tank_state.last_empty_at = committed_empty
        publisher.publish(_topic(tank.id, "last_empty_at"), json.dumps(committed_empty))
        logger.info(f"Tank {tank.id}: last_empty_at auto-detected as {committed_empty}")
        tank_state.level_pct_at_empty_commit = level_pct
        _save_state_field(tank.id, "level_pct_at_empty_commit", level_pct)

    # Compute and publish volume-since-latch -- unconditional live fields
    # (volume-timestamp-telemetry ticket 02)
    volume_since_full_l, volume_since_empty_l = _compute_volume_since_latch(
        tank, level_pct, tank_state.level_pct_at_full_commit, tank_state.level_pct_at_empty_commit
    )
    publisher.publish(
        _topic(tank.id, "volume_since_full_l"), json.dumps(round(volume_since_full_l, 2))
    )
    publisher.publish(
        _topic(tank.id, "volume_since_empty_l"), json.dumps(round(volume_since_empty_l, 2))
    )

    # Read and publish temperature if configured (ticket 05)
    if tank.temp_sensor_id is not None:
        if tank.temp_sensor_id not in temperature_sensors:
            try:
                temperature_sensors[tank.temp_sensor_id] = DS18B20(tank.temp_sensor_id)
            except FileNotFoundError as e:
                logger.warning(f"Tank {tank.id}: {e}")
                return

        try:
            temp_c = temperature_sensors[tank.temp_sensor_id].read_temperature_c()
            publisher.publish(_topic(tank.id, "temperature_c"), json.dumps(round(temp_c, 2)))
        except ValueError as e:
            logger.warning(f"Tank {tank.id}: Failed to read temperature: {e}")

    logger.info(
        "tank=%s voltage=%.4fV level_pct=%.1f status=%s alarm=%s",
        tank.id,
        voltage,
        level_pct,
        status.value,
        new_alarm_state if tank.alarm_direction else "n/a",
    )


def _handle_timestamp_set_command(
    publisher: Publisher,
    tank: TankConfig,
    tank_state: TankState,
    field_name: str,
    payload: str,
) -> None:
    """Handle last_inspected_at/last_full_at/last_empty_at /set command topics.

    Shared handler for all three timestamp-valued command topics (ticket 06
    established the pattern for last_inspected_date; flow-rate-full-empty-telemetry
    ticket 02 reused it for last_full_date/last_empty_date; volume-timestamp-telemetry
    ticket 01 renamed all three and widened accepted input to either a full
    ISO-8601 timestamp or a bare YYYY-MM-DD date, normalized to local midnight
    -- manual corrections/backfills often only know the date, not the minute).
    Rejects malformed or future values, logs-only on error, no error topic.
    """
    try:
        # Payload should be a JSON string: "2026-09-29" or "2026-09-29T14:32:05-04:00"
        raw = json.loads(payload)
    except json.JSONDecodeError:
        logger.error(f"Tank {tank.id}: Invalid JSON in {field_name}/set: {payload}")
        return

    # Validate + normalize to a full ISO-8601 timestamp, reject future values
    normalized = _normalize_timestamp_str(raw) if isinstance(raw, str) else None
    if normalized is None:
        logger.error(
            f"Tank {tank.id}: Invalid {field_name}: {raw} "
            "(must be an ISO-8601 timestamp or YYYY-MM-DD, and not in the future)"
        )
        return

    # Persist and publish -- manual writes go through unconditionally (last write
    # wins); auto-detection keeps running independently and may overwrite later.
    _save_state_field(tank.id, field_name, normalized)
    setattr(tank_state, field_name, normalized)
    publisher.publish(_topic(tank.id, field_name), json.dumps(normalized))
    logger.info(f"Tank {tank.id}: {field_name} set to {normalized}")


def run(config: AppConfig) -> None:
    """Main publish loop (ticket 01, 02, 05, 06; flow-rate-full-empty-telemetry ticket 01, 02)."""
    _ensure_state_dir()

    publisher = Publisher(config.mqtt)
    publisher.connect()
    adc = ADS1115(bus_number=config.i2c_bus, address=config.i2c_address)

    # Initialize tank state objects
    tank_states: dict[str, TankState] = {}
    for tank in config.tanks:
        tank_state = TankState()
        # Load persisted identity timestamps (ticket 06; last_full_at/last_empty_at
        # added by flow-rate-full-empty-telemetry ticket 02, renamed + upgraded from
        # bare dates to full timestamps by volume-timestamp-telemetry ticket 01 --
        # _migrate_timestamp_field transparently upgrades any pre-rename state file)
        tank_state.last_inspected_at = _migrate_timestamp_field(tank.id, "last_inspected_at")
        tank_state.last_full_at = _migrate_timestamp_field(tank.id, "last_full_at")
        tank_state.last_empty_at = _migrate_timestamp_field(tank.id, "last_empty_at")
        # Volume-since-latch anchors (volume-timestamp-telemetry ticket 02): survive
        # a restart even though the derived volume_since_*_l field is live-cadence.
        # None (no key present -- pre-upgrade state file) means "no anchor yet";
        # _compute_volume_since_latch publishes 0 until the next latch commit.
        tank_state.level_pct_at_full_commit = _load_state_field(tank.id, "level_pct_at_full_commit")
        tank_state.level_pct_at_empty_commit = _load_state_field(
            tank.id, "level_pct_at_empty_commit"
        )
        tank_states[tank.id] = tank_state

    # Temperature sensor cache (ticket 05)
    temperature_sensors: dict[str, DS18B20] = {}

    # Publish identity fields at startup
    for tank in config.tanks:
        publish_identity(publisher, tank, tank_states[tank.id])

    # Subscribe to last_inspected_at/last_full_at/last_empty_at /set command
    # topics (ticket 06; last_full_at/last_empty_at added by ticket 02)
    for tank in config.tanks:
        for field_name in ("last_inspected_at", "last_full_at", "last_empty_at"):
            topic = _topic(tank.id, f"{field_name}/set")

            def make_callback(t: TankConfig, f: str) -> callable:
                def callback(payload: str) -> None:
                    _handle_timestamp_set_command(publisher, t, tank_states[t.id], f, payload)

                return callback

            publisher.subscribe(topic, make_callback(tank, field_name))

    try:
        while True:
            now = time.time()
            for tank in config.tanks:
                try:
                    read_and_publish(
                        publisher, adc, tank, tank_states[tank.id], temperature_sensors, now
                    )
                except Exception:
                    logger.exception("Read failed for tank=%s", tank.id)
            time.sleep(min(t.update_interval_ms for t in config.tanks) / 1000.0)
    finally:
        adc.close()
        publisher.disconnect()
