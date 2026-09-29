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


@dataclass
class AlarmState:
    """Per-tank alarm state tracking (ticket 02)."""
    previous_state: str = "ok"  # "ok" or "alarm"
    delay_timer_start: float | None = None  # Time when threshold was crossed


@dataclass
class TankState:
    """Per-tank runtime state."""
    alarm: AlarmState = field(default_factory=AlarmState)
    last_inspected_date: str | None = None  # Most recent persisted date (YYYY-MM-DD)


def _topic(tank_id: str, prop: str) -> str:
    return f"renewvan/tank/{tank_id}/{prop}"


def _ensure_state_dir() -> None:
    """Ensure state directory exists for persistence (ticket 06)."""
    _STATE_DIR.mkdir(exist_ok=True, parents=True)


def _load_last_inspected_date(tank_id: str) -> str | None:
    """Load persisted last_inspected_date from state file (ticket 06)."""
    state_file = _STATE_DIR / f"{tank_id}.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
        return data.get("last_inspected_date")
    except Exception as e:
        logger.warning(f"Failed to load state for tank={tank_id}: {e}")
        return None


def _save_last_inspected_date(tank_id: str, date_str: str) -> None:
    """Persist last_inspected_date to state file (ticket 06)."""
    state_file = _STATE_DIR / f"{tank_id}.json"
    try:
        state_file.write_text(json.dumps({"last_inspected_date": date_str}))
    except Exception as e:
        logger.error(f"Failed to save state for tank={tank_id}: {e}")


def _validate_last_inspected_date(date_str: str) -> bool:
    """Validate YYYY-MM-DD format and reject future dates (ticket 06)."""
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
    - last_inspected_date (only if loaded from state file)
    """
    publisher.publish(_topic(tank.id, "fluid_type"), json.dumps(tank.fluid_type))
    publisher.publish(_topic(tank.id, "capacity_l"), json.dumps(tank.capacity_l))
    
    # Republish last_inspected_date if it was persisted (ticket 06)
    if tank_state.last_inspected_date is not None:
        publisher.publish(
            _topic(tank.id, "last_inspected_date"),
            json.dumps(tank_state.last_inspected_date)
        )


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

    delay_start = now if alarm_state_obj.delay_timer_start is None else alarm_state_obj.delay_timer_start

    if tank.alarm_delay_s > 0 and (now - delay_start) < tank.alarm_delay_s:
        # Still within delay window: hold current state, keep timer running.
        return previous, AlarmState(previous_state=previous, delay_timer_start=delay_start)

    # Delay elapsed (or no delay configured): commit the transition.
    return target_state, AlarmState(previous_state=target_state)


def read_and_publish(
    publisher: Publisher,
    adc: ADS1115,
    tank: TankConfig,
    tank_state: TankState,
    temperature_sensors: dict[str, DS18B20],
    now: float,
) -> None:
    """Read sensors and publish live fields (ticket 01, 02, 05).
    
    Live fields (retained, republished on every read):
    - level_pct, status (always)
    - alarm_state (if alarm is configured)
    - temperature_c (if DS18B20 is configured)
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


def _handle_last_inspected_date_command(
    publisher: Publisher,
    tank: TankConfig,
    tank_state: TankState,
    payload: str,
) -> None:
    """Handle last_inspected_date/set command topic (ticket 06)."""
    try:
        # Payload should be a JSON string: "2026-09-29"
        date_str = json.loads(payload)
    except json.JSONDecodeError:
        logger.error(f"Tank {tank.id}: Invalid JSON in last_inspected_date/set: {payload}")
        return
    
    # Validate format and reject future dates
    if not _validate_last_inspected_date(date_str):
        logger.error(
            f"Tank {tank.id}: Invalid last_inspected_date: {date_str} "
            "(must be YYYY-MM-DD and not a future date)"
        )
        return
    
    # Persist and publish
    _save_last_inspected_date(tank.id, date_str)
    tank_state.last_inspected_date = date_str
    publisher.publish(_topic(tank.id, "last_inspected_date"), json.dumps(date_str))
    logger.info(f"Tank {tank.id}: last_inspected_date set to {date_str}")


def run(config: AppConfig) -> None:
    """Main publish loop (ticket 01, 02, 05, 06)."""
    _ensure_state_dir()
    
    publisher = Publisher(config.mqtt)
    publisher.connect()
    adc = ADS1115(bus_number=config.i2c_bus, address=config.i2c_address)
    
    # Initialize tank state objects
    tank_states: dict[str, TankState] = {}
    for tank in config.tanks:
        tank_state = TankState()
        # Load persisted last_inspected_date (ticket 06)
        tank_state.last_inspected_date = _load_last_inspected_date(tank.id)
        tank_states[tank.id] = tank_state
    
    # Temperature sensor cache (ticket 05)
    temperature_sensors: dict[str, DS18B20] = {}
    
    # Publish identity fields at startup
    for tank in config.tanks:
        publish_identity(publisher, tank, tank_states[tank.id])
    
    # Subscribe to last_inspected_date/set command topics (ticket 06)
    for tank in config.tanks:
        topic = _topic(tank.id, "last_inspected_date/set")
        def make_callback(t: TankConfig) -> callable:
            def callback(payload: str) -> None:
                _handle_last_inspected_date_command(publisher, t, tank_states[t.id], payload)
            return callback
        publisher.subscribe(topic, make_callback(tank))
    
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
