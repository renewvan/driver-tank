"""Layered INI config: config.default.ini (shipped) + config.ini (local override).

Same pattern as dbus-ads1115 (and dbus-serialbattery before it): the
default file ships every key with a safe value and is never edited in
place; config.ini is gitignored, holds only the keys a given install
needs to override, and survives upgrades.
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from driver_tank.calibration import Calibration, parse_shape

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.default.ini"


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


@dataclass(frozen=True)
class AppConfig:
    i2c_bus: int
    i2c_address: int
    mqtt: MqttConfig
    tanks: list[TankConfig]


def _get_float(section: configparser.SectionProxy, key: str, default: float | None = None) -> float:
    return section.getfloat(key, fallback=default)


def load_config(default_path: Path = DEFAULT_CONFIG_PATH, local_path: Path | None = None) -> AppConfig:
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
        client_id=mqtt_section.get("client_id", "driver-tank"),
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
            reference_voltage=_get_float(section, "reference_voltage", i2c.getfloat("reference_voltage", 3.3)),
            sensor_min=_get_float(section, "sensor_min"),
            sensor_max=_get_float(section, "sensor_max"),
            shape=parse_shape(section.get("shape", fallback="")),
        )
        tanks.append(
            TankConfig(
                id=tank_id,
                name=section.get("name", tank_id),
                channel=section.getint("channel"),
                fluid_type=section.get("fluid_type"),
                capacity_l=_get_float(section, "capacity_l"),
                update_interval_ms=section.getint("update_interval_ms", 3000),
                calibration=calibration,
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
