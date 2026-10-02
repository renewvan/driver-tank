"""Layered INI config: config.default.ini (shipped) + config.ini (local override).

Same pattern as dbus-ads1115 (and dbus-serialbattery before it): the
default file ships every key with a safe value and is never edited in
place; config.ini is gitignored, holds only the keys a given install
needs to override, and survives upgrades.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from pathlib import Path

from node_tank.calibration import Calibration, parse_shape

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.default.ini"

# Volume unit conversion factors (ticket 03)
# Convert from the configured unit to liters (canonical unit for publishing)
_VOLUME_TO_LITERS = {
    "liters": 1.0,
    "l": 1.0,
    "litres": 1.0,
    "cubic_meters": 1000.0,
    "m3": 1000.0,
    "m³": 1000.0,
    "gallons_us": 3.78541,
    "us_gallons": 3.78541,
    "gallons_imp": 4.54609,
    "gallons_imperial": 4.54609,
    "imp_gallons": 4.54609,
}


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    client_id: str
    reconnect_min_delay: float
    reconnect_max_delay: float


@dataclass(frozen=True)
class TankConfig:
    id: str  # topic path segment, e.g. "fresh" / "grey"
    name: str
    channel: int
    fluid_type: str
    capacity_l: float
    update_interval_ms: int
    calibration: Calibration
    # Alarm configuration (ticket 02)
    alarm_direction: str | None  # "low" or "high", None if alarm disabled
    alarm_threshold: float | None  # level % that triggers alarm
    alarm_restore: float | None  # level % that clears alarm
    alarm_delay_s: float  # seconds to wait after threshold crossed (default 0)
    # Temperature sensor (ticket 05)
    temp_sensor_id: str | None  # 1-Wire ROM ID for DS18B20, None if not configured
    # Full/empty timestamp tracking (flow-rate-full-empty-telemetry ticket 02;
    # renamed from *_date to *_at, volume-timestamp-telemetry ticket 01)
    full_threshold_pct: float  # in-band threshold for last_full_at (default 99)
    empty_threshold_pct: float  # in-band threshold for last_empty_at (default 1)


@dataclass(frozen=True)
class AppConfig:
    i2c_bus: int
    i2c_address: int
    mqtt: MqttConfig
    tanks: list[TankConfig]


def _get_float(section: configparser.SectionProxy, key: str, default: float | None = None) -> float:
    return section.getfloat(key, fallback=default)


def _convert_tank_capacity_to_liters(section: configparser.SectionProxy) -> float:
    """Convert tank_capacity + volume_unit to liters (ticket 03).

    Raises:
        ValueError: If both capacity_l (deprecated) and tank_capacity are present,
                   or if tank_capacity is present but volume_unit is missing.
    """
    has_capacity_l = "capacity_l" in section
    has_tank_capacity = "tank_capacity" in section
    has_volume_unit = "volume_unit" in section

    # Error if deprecated capacity_l key is present
    if has_capacity_l:
        raise ValueError(
            f"Section {section.name}: 'capacity_l' is deprecated. "
            "Replace with 'tank_capacity' + 'volume_unit' pair. "
            "Example: tank_capacity=70, volume_unit=liters"
        )

    # Both tank_capacity and volume_unit must be present together
    if has_tank_capacity != has_volume_unit:
        raise ValueError(
            f"Section {section.name}: 'tank_capacity' and 'volume_unit' must both be present. "
            "Example: tank_capacity=70, volume_unit=liters"
        )

    if not has_tank_capacity:
        raise ValueError(f"Section {section.name}: 'tank_capacity' key is required")

    # Read and convert
    raw_capacity = section.getfloat("tank_capacity")
    volume_unit = section.get("volume_unit").strip().lower()

    factor = _VOLUME_TO_LITERS.get(volume_unit)
    if factor is None:
        logger.warning(
            f"Tank '{section.name}': unknown volume_unit '{volume_unit}', assuming cubic_meters"
        )
        factor = 1000.0  # cubic_meters fallback

    return raw_capacity * factor


def load_config(
    default_path: Path = DEFAULT_CONFIG_PATH, local_path: Path | None = None
) -> AppConfig:
    """Load config.default.ini, then layer config.ini (if present) on top."""
    parser = configparser.ConfigParser()
    read_files = [str(default_path)]
    if local_path is not None and local_path.exists():
        read_files.append(str(local_path))
    parsed = parser.read(read_files)
    if not parsed:
        raise FileNotFoundError(f"No config file found (looked for {default_path})")

    i2c = parser["i2c"]
    mqtt_section = parser["mqtt"]
    mqtt = MqttConfig(
        host=mqtt_section.get("host", "localhost"),
        port=mqtt_section.getint("port", 1883),
        username=mqtt_section.get("username", fallback=None) or None,
        password=mqtt_section.get("password", fallback=None) or None,
        client_id=mqtt_section.get("client_id", "node-tank"),
        reconnect_min_delay=mqtt_section.getfloat("reconnect_min_delay", 1.0),
        reconnect_max_delay=mqtt_section.getfloat("reconnect_max_delay", 30.0),
    )

    tanks: list[TankConfig] = []
    for section_name in parser.sections():
        if not section_name.startswith("tank."):
            continue
        section = parser[section_name]
        if not section.getboolean("enabled", fallback=True):
            continue
        tank_id = section_name.split(".", 1)[1]
        calibration = Calibration(
            fixed_resistor=_get_float(section, "fixed_resistor"),
            reference_voltage=_get_float(
                section, "reference_voltage", i2c.getfloat("reference_voltage", 3.3)
            ),
            sensor_min=_get_float(section, "sensor_min"),
            sensor_max=_get_float(section, "sensor_max"),
            shape=parse_shape(section.get("shape", fallback="")),
        )

        # Volume-unit conversion (ticket 03)
        capacity_l = _convert_tank_capacity_to_liters(section)

        # Alarm configuration (ticket 02)
        alarm_direction = section.get("alarm_direction", fallback=None)
        alarm_direction = alarm_direction.strip().lower() if alarm_direction else None
        alarm_threshold = _get_float(section, "alarm_threshold", default=None)
        alarm_restore = _get_float(section, "alarm_restore", default=None)
        alarm_delay_s = _get_float(section, "alarm_delay_s", default=0.0)

        # Temperature sensor (ticket 05)
        temp_sensor_id = section.get("temp_sensor_id", fallback=None)
        temp_sensor_id = temp_sensor_id.strip() if temp_sensor_id else None

        # Full/empty date tracking (flow-rate-full-empty-telemetry ticket 02)
        full_threshold_pct = _get_float(section, "full_threshold_pct", default=99.0)
        empty_threshold_pct = _get_float(section, "empty_threshold_pct", default=1.0)

        tanks.append(
            TankConfig(
                id=tank_id,
                name=section.get("name", tank_id),
                channel=section.getint("channel"),
                fluid_type=section.get("fluid_type"),
                capacity_l=capacity_l,
                update_interval_ms=section.getint("update_interval_ms", 3000),
                calibration=calibration,
                alarm_direction=alarm_direction,
                alarm_threshold=alarm_threshold,
                alarm_restore=alarm_restore,
                alarm_delay_s=alarm_delay_s,
                temp_sensor_id=temp_sensor_id,
                full_threshold_pct=full_threshold_pct,
                empty_threshold_pct=empty_threshold_pct,
            )
        )

    if not tanks:
        raise ValueError("No enabled [tank.*] sections found in config")

    address = i2c.get("address", "0x48")
    i2c_address = int(address, 16) if address.lower().startswith("0x") else int(address)

    return AppConfig(
        i2c_bus=i2c.getint("bus", 1),
        i2c_address=i2c_address,
        mqtt=mqtt,
        tanks=tanks,
    )
