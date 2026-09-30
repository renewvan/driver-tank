"""Publish loop: ADC read -> calibration math -> renewvan/tank/<id>/<property>.

Field/topic shape per hub's schema/tank.schema.json and
docs/porting-dbus-to-mqtt-node.md: fluid_type/capacity_l published
retained at startup (identity fields, rarely change), level_pct/status
republished retained on every read (live fields).

Additional publishers per spec (tickets 02, 05, 06):
- alarm_state: live field if alarm is configured
- temperature_c: live field if DS18B20 is configured
- last_inspected_date: identity field, command via /set topic (unretained)

Persistence boundary (ticket 06): config.py's config.ini is install-time
and load-only, never machine-written (see config.py's docstring). This
module introduces a *separate*, machine-written state/<tank_id>.json
directory for the one piece of runtime state that must survive a
restart (last_inspected_date) -- distinct from config, never read by
config.py, gitignored like config.ini.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime
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


@dataclass
class AlarmState:
    """Per-tank alarm state tracking (ticket 02)."""
    previous_state: str = "ok"  # "ok" or "alarm"
    delay_timer_start: float | None = None  # Time when threshold was crossed


@dataclass
class FlowState:
    """Per-tank fill/drain flow-rate tracking (flow-rate-full-empty-telemetry ticket 01)."""
    edge_level_pct: float | None = None  # level_pct at the last qualifying edge, None until first read
    edge_time: float | None = None  # timestamp of the last qualifying edge
    last_fill_rate: float = 0.0  # last-published fill_rate_lpm
    last_drain_rate: float = 0.0  # last-published drain_rate_lpm


@dataclass
class FullEmptyState:
    """Per-tank last-full/last-empty one-shot latch tracking (flow-rate-full-empty-telemetry ticket 02)."""
    full_latched: bool = False
    full_delay_start: float | None = None
    empty_latched: bool = False
    empty_delay_start: float | None = None


@dataclass
class TankState:
    """Per-tank runtime state."""
    alarm: AlarmState = field(default_factory=AlarmState)
    last_inspected_date: str | None = None  # Most recent persisted date (YYYY-MM-DD)
    flow: FlowState = field(default_factory=FlowState)
    full_empty: FullEmptyState = field(default_factory=FullEmptyState)
    last_full_date: str | None = None  # Most recent persisted date (YYYY-MM-DD)
    last_empty_date: str | None = None  # Most recent persisted date (YYYY-MM-DD)


def _topic(tank_id: str, prop: str) -> str:
    return f"renewvan/tank/{tank_id}/{prop}"


def _ensure_state_dir() -> None:
    """Ensure state directory exists for persistence (ticket 06)."""
    _STATE_DIR.mkdir(exist_ok=True, parents=True)


def _load_state_field(tank_id: str, field_name: str) -> str | None:
    """Load a single persisted date field from state/<tank_id>.json (ticket 06).

    Generic across last_inspected_date, last_full_date, last_empty_date --
    all three share one file, one read/write helper (flow-rate-full-empty-telemetry ticket 02).
    """
    state_file = _STATE_DIR / f"{tank_id}.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
        return data.get(field_name)
    except Exception as e:
        logger.warning(f"Failed to load state for tank={tank_id}: {e}")
        return None


def _save_state_field(tank_id: str, field_name: str, date_str: str) -> None:
    """Persist a single date field to state/<tank_id>.json, merging with existing keys.

    Read-modify-write so last_inspected_date/last_full_date/last_empty_date
    (written independently, at different times) don't clobber each other
    in the shared file (flow-rate-full-empty-telemetry ticket 02).
    """
    state_file = _STATE_DIR / f"{tank_id}.json"
    try:
        data = {}
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
            except Exception:
                data = {}
        data[field_name] = date_str
        state_file.write_text(json.dumps(data))
    except Exception as e:
        logger.error(f"Failed to save state for tank={tank_id}: {e}")


def _validate_date_str(date_str: str) -> bool:
    """Validate YYYY-MM-DD format and reject future dates (ticket 06).

    Shared by last_inspected_date, last_full_date, last_empty_date
    /set command handlers (flow-rate-full-empty-telemetry ticket 02).
    """
    # Format validation
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return False
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        if date_obj > today:
            return False
        return True
    except ValueError:
        return False


def publish_identity(publisher: Publisher, tank: TankConfig, tank_state: TankState) -> None:
    """Publish identity fields at startup (ticket 01, 06).
    
    Identity fields (retained, published once at startup):
    - fluid_type, capacity_l (always)
    - last_inspected_date, last_full_date, last_empty_date (only if loaded from state file)
    """
    publisher.publish(_topic(tank.id, "fluid_type"), json.dumps(tank.fluid_type))
    publisher.publish(_topic(tank.id, "capacity_l"), json.dumps(tank.capacity_l))
    
    # Republish persisted identity dates if they were loaded from state (ticket 06;
    # last_full_date/last_empty_date added by flow-rate-full-empty-telemetry ticket 02)
    if tank_state.last_inspected_date is not None:
        publisher.publish(
            _topic(tank.id, "last_inspected_date"),
            json.dumps(tank_state.last_inspected_date)
        )
    if tank_state.last_full_date is not None:
        publisher.publish(
            _topic(tank.id, "last_full_date"),
            json.dumps(tank_state.last_full_date)
        )
    if tank_state.last_empty_date is not None:
        publisher.publish(
            _topic(tank.id, "last_empty_date"),
            json.dumps(tank_state.last_empty_date)
        )


def _crossed(direction: str, level_pct: float, boundary: float, entering_alarm: bool) -> bool:
    """True if level_pct has crossed past boundary for this transition.

    Entering alarm moves *toward* the configured direction (empty: <=,
    full: >=); restoring to ok moves back the *opposite* way (empty: >=,
    full: <=) -- entry and restore boundaries are approached from
    opposite sides, so the comparison flips between them.
    """
    if direction == "empty":
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

    delay_start = now if alarm_state_obj.delay_timer_start is None else alarm_state_obj.delay_timer_start

    if tank.alarm_delay_s > 0 and (now - delay_start) < tank.alarm_delay_s:
        # Still within delay window: hold current state, keep timer running.
        return previous, AlarmState(previous_state=previous, delay_timer_start=delay_start)

    # Delay elapsed (or no delay configured): commit the transition.
    return target_state, AlarmState(previous_state=target_state)


def _compute_flow_rates(
    tank: TankConfig,
    level_pct: float,
    now: float,
    flow_state: FlowState,
) -> tuple[float, float, FlowState]:
    """Compute fill_rate_lpm/drain_rate_lpm from edge-to-edge level_pct changes
    (flow-rate-full-empty-telemetry ticket 01).

    Tracks the last level_pct reading that differed from the stored "edge"
    sample by at least flow_min_delta_pct. A qualifying delta moves the edge
    and computes a fresh rate; sub-threshold deltas are noise and are
    ignored (edge and published rate both hold). If flow_idle_timeout_s
    elapses with no qualifying edge, both rates drop to 0 -- but the edge
    itself is NOT moved, so a later resumed fill/drain is still measured
    from the last real level change, not from the idle-timeout instant.

    Returns:
        (fill_rate_lpm, drain_rate_lpm, updated_FlowState)
    """
    if flow_state.edge_level_pct is None:
        # First read of the process: no edge yet, nothing to compare against.
        return 0.0, 0.0, FlowState(edge_level_pct=level_pct, edge_time=now)

    delta = level_pct - flow_state.edge_level_pct

    if abs(delta) >= tank.flow_min_delta_pct:
        elapsed_s = now - flow_state.edge_time
        elapsed_min = elapsed_s / 60.0 if elapsed_s > 0 else 0.0
        rate = (abs(delta) / 100.0 * tank.capacity_l / elapsed_min) if elapsed_min > 0 else 0.0
        if delta > 0:
            fill_rate, drain_rate = rate, 0.0
        else:
            fill_rate, drain_rate = 0.0, rate
        return fill_rate, drain_rate, FlowState(
            edge_level_pct=level_pct, edge_time=now,
            last_fill_rate=fill_rate, last_drain_rate=drain_rate,
        )

    if (now - flow_state.edge_time) >= tank.flow_idle_timeout_s:
        # No qualifying edge for a while: flow has stopped. Edge stays put.
        return 0.0, 0.0, FlowState(
            edge_level_pct=flow_state.edge_level_pct, edge_time=flow_state.edge_time,
            last_fill_rate=0.0, last_drain_rate=0.0,
        )

    # In band, no timeout yet: hold the last-published rate.
    return flow_state.last_fill_rate, flow_state.last_drain_rate, flow_state


def _compute_full_empty_state(
    tank: TankConfig,
    level_pct: float,
    now: float,
    full_empty_state: FullEmptyState,
) -> tuple[str | None, str | None, FullEmptyState]:
    """Compute last_full_date/last_empty_date one-shot latches
    (flow-rate-full-empty-telemetry ticket 02).

    Two independent latches, structurally identical, one per direction.
    Per direction: leaving the in-band range clears the latch and timer
    (re-arming for the next excursion); entering the band starts the
    delay timer (reusing tank.alarm_delay_s) if not already latched, and
    commits date.today() once the delay elapses (immediately if
    alarm_delay_s <= 0); staying in-band while already latched is a
    no-op; leaving before the delay elapses cancels the pending commit --
    same cancel-on-retreat behavior as _compute_alarm_state.

    Returns:
        (last_full_date | None, last_empty_date | None, updated_FullEmptyState)
        -- the date fields are only non-None on the read where a commit happens.
    """
    full_latched = full_empty_state.full_latched
    full_delay_start = full_empty_state.full_delay_start
    committed_full: str | None = None

    if level_pct < tank.full_threshold_pct:
        full_latched, full_delay_start = False, None
    elif not full_latched:
        full_delay_start = now if full_delay_start is None else full_delay_start
        if tank.alarm_delay_s <= 0 or (now - full_delay_start) >= tank.alarm_delay_s:
            committed_full = date.today().isoformat()
            full_latched, full_delay_start = True, None

    empty_latched = full_empty_state.empty_latched
    empty_delay_start = full_empty_state.empty_delay_start
    committed_empty: str | None = None

    if level_pct > tank.empty_threshold_pct:
        empty_latched, empty_delay_start = False, None
    elif not empty_latched:
        empty_delay_start = now if empty_delay_start is None else empty_delay_start
        if tank.alarm_delay_s <= 0 or (now - empty_delay_start) >= tank.alarm_delay_s:
            committed_empty = date.today().isoformat()
            empty_latched, empty_delay_start = True, None

    return committed_full, committed_empty, FullEmptyState(
        full_latched=full_latched, full_delay_start=full_delay_start,
        empty_latched=empty_latched, empty_delay_start=empty_delay_start,
    )


def read_and_publish(
    publisher: Publisher,
    adc: ADS1115,
    tank: TankConfig,
    tank_state: TankState,
    temperature_sensors: dict[str, DS18B20],
    now: float,
) -> None:
    """Read sensors and publish live fields (ticket 01, 02, 05; flow-rate-full-empty-telemetry ticket 01/02).
    
    Live fields (retained, republished on every read):
    - level_pct, status, fill_rate_lpm, drain_rate_lpm (always)
    - alarm_state (if alarm is configured)
    - temperature_c (if DS18B20 is configured)
    Identity fields republished only when they change:
    - last_full_date, last_empty_date (auto-detected on a sustained full/empty crossing)
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
    
    # Compute and publish fill/drain rate -- unconditional live fields (flow-rate-full-empty-telemetry ticket 01)
    fill_rate_lpm, drain_rate_lpm, updated_flow_state = _compute_flow_rates(
        tank, level_pct, now, tank_state.flow
    )
    tank_state.flow = updated_flow_state
    publisher.publish(_topic(tank.id, "fill_rate_lpm"), json.dumps(round(fill_rate_lpm, 2)))
    publisher.publish(_topic(tank.id, "drain_rate_lpm"), json.dumps(round(drain_rate_lpm, 2)))
    
    # Compute last_full_date/last_empty_date auto-detection (flow-rate-full-empty-telemetry ticket 02)
    committed_full, committed_empty, updated_full_empty_state = _compute_full_empty_state(
        tank, level_pct, now, tank_state.full_empty
    )
    tank_state.full_empty = updated_full_empty_state
    if committed_full is not None:
        _save_state_field(tank.id, "last_full_date", committed_full)
        tank_state.last_full_date = committed_full
        publisher.publish(_topic(tank.id, "last_full_date"), json.dumps(committed_full))
        logger.info(f"Tank {tank.id}: last_full_date auto-detected as {committed_full}")
    if committed_empty is not None:
        _save_state_field(tank.id, "last_empty_date", committed_empty)
        tank_state.last_empty_date = committed_empty
        publisher.publish(_topic(tank.id, "last_empty_date"), json.dumps(committed_empty))
        logger.info(f"Tank {tank.id}: last_empty_date auto-detected as {committed_empty}")
    
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


def _handle_date_set_command(
    publisher: Publisher,
    tank: TankConfig,
    tank_state: TankState,
    field_name: str,
    payload: str,
) -> None:
    """Handle last_inspected_date/last_full_date/last_empty_date /set command topics.

    Shared handler for all three date-valued command topics (ticket 06
    established the pattern for last_inspected_date; flow-rate-full-empty-telemetry
    ticket 02 reuses it exactly for last_full_date/last_empty_date). Rejects
    malformed or future dates, logs-only on error, no error topic.
    """
    try:
        # Payload should be a JSON string: "2026-09-29"
        date_str = json.loads(payload)
    except json.JSONDecodeError:
        logger.error(f"Tank {tank.id}: Invalid JSON in {field_name}/set: {payload}")
        return
    
    # Validate format and reject future dates
    if not _validate_date_str(date_str):
        logger.error(
            f"Tank {tank.id}: Invalid {field_name}: {date_str} "
            "(must be YYYY-MM-DD and not a future date)"
        )
        return
    
    # Persist and publish -- manual writes go through unconditionally (last write
    # wins); auto-detection keeps running independently and may overwrite later.
    _save_state_field(tank.id, field_name, date_str)
    setattr(tank_state, field_name, date_str)
    publisher.publish(_topic(tank.id, field_name), json.dumps(date_str))
    logger.info(f"Tank {tank.id}: {field_name} set to {date_str}")


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
        # Load persisted identity dates (ticket 06; last_full_date/last_empty_date
        # added by flow-rate-full-empty-telemetry ticket 02)
        tank_state.last_inspected_date = _load_state_field(tank.id, "last_inspected_date")
        tank_state.last_full_date = _load_state_field(tank.id, "last_full_date")
        tank_state.last_empty_date = _load_state_field(tank.id, "last_empty_date")
        tank_states[tank.id] = tank_state
    
    # Temperature sensor cache (ticket 05)
    temperature_sensors: dict[str, DS18B20] = {}
    
    # Publish identity fields at startup
    for tank in config.tanks:
        publish_identity(publisher, tank, tank_states[tank.id])
    
    # Subscribe to last_inspected_date/last_full_date/last_empty_date /set command
    # topics (ticket 06; last_full_date/last_empty_date added by ticket 02)
    for tank in config.tanks:
        for field_name in ("last_inspected_date", "last_full_date", "last_empty_date"):
            topic = _topic(tank.id, f"{field_name}/set")
            def make_callback(t: TankConfig, f: str) -> callable:
                def callback(payload: str) -> None:
                    _handle_date_set_command(publisher, t, tank_states[t.id], f, payload)
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
