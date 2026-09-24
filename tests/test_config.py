"""Layered config-loading tests: config.default.ini overridden by config.ini."""
from pathlib import Path

import pytest

from renewvan_tank.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_defaults_only_loads_two_tanks():
    config = load_config(default_path=FIXTURES / "config.default.ini", local_path=None)
    assert config.mqtt.host == "localhost"
    ids = {t.id for t in config.tanks}
    assert ids == {"fresh", "grey"}


def test_local_override_replaces_mqtt_host_and_calibration():
    config = load_config(
        default_path=FIXTURES / "config.default.ini", local_path=FIXTURES / "config.ini"
    )
    assert config.mqtt.host == "192.168.1.10"
    fresh = next(t for t in config.tanks if t.id == "fresh")
    assert fresh.calibration.sensor_max == 200.0


def test_disabled_tank_section_is_excluded():
    config = load_config(default_path=FIXTURES / "config.default.ini", local_path=None)
    assert all(t.id != "disabled_extra" for t in config.tanks)


def test_missing_default_config_raises():
    with pytest.raises(FileNotFoundError):
        load_config(default_path=FIXTURES / "does-not-exist.ini", local_path=None)
