"""Layered config-loading tests: config.default.ini overridden by config.ini."""

from pathlib import Path

import pytest

from driver_tank.config import load_config

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


# --- sensor_type / Level probes ----------------------------------------------


def _load_with_local(tmp_path, local: str):
    path = tmp_path / "config.ini"
    path.write_text(local)
    return load_config(default_path=FIXTURES / "config.default.ini", local_path=path)


def test_sensor_type_defaults_to_sender():
    config = load_config(default_path=FIXTURES / "config.default.ini", local_path=None)
    fresh = next(t for t in config.tanks if t.id == "fresh")
    assert fresh.level_probes is None
    assert fresh.calibration is not None


def test_probes_tank_overrides_a_default_sender_section(tmp_path):
    # Defaults still carry channel/sensor_min/sensor_max for grey: they are ignored,
    # only keys set in the local config.ini count as a conflict.
    config = _load_with_local(
        tmp_path,
        "[tank.grey]\nsensor_type = probes\nprobes = 1:100\nfixed_resistor = 47000\n",
    )
    grey = next(t for t in config.tanks if t.id == "grey")
    assert grey.calibration is None
    assert grey.channel is None
    assert grey.level_probes.marks_pct == [100.0]
    assert grey.level_probes.fixed_resistor == 47000.0
    assert grey.level_probes.excitation_gpio is None


def test_probes_tank_reads_wet_thresholds_and_excitation(tmp_path):
    config = _load_with_local(
        tmp_path,
        "[tank.grey]\nsensor_type = probes\nprobes = 1:100\nfixed_resistor = 47000\n"
        "probe_wet_max_ohm = 50000\nprobe_dry_min_ohm = 80000\nprobe_excitation_gpio = 17\n",
    )
    probes = next(t for t in config.tanks if t.id == "grey").level_probes
    assert (probes.wet_max_ohm, probes.dry_min_ohm) == (50000.0, 80000.0)
    assert probes.excitation_gpio == 17


def test_probe_marks_are_shape_corrected(tmp_path):
    config = _load_with_local(
        tmp_path,
        "[tank.grey]\nsensor_type = probes\nprobes = 1:50,2:100\nfixed_resistor = 47000\n"
        "shape = 50:40\n",
    )
    grey = next(t for t in config.tanks if t.id == "grey")
    assert grey.level_probes.marks_pct == [40.0, 100.0]


@pytest.mark.parametrize("key", ["channel = 1", "sensor_min = 0", "sensor_max = 190"])
def test_probes_tank_rejects_sender_only_keys_set_locally(tmp_path, key):
    with pytest.raises(ValueError, match="sender"):
        _load_with_local(
            tmp_path,
            f"[tank.grey]\nsensor_type = probes\nprobes = 1:100\nfixed_resistor = 47000\n{key}\n",
        )


def test_new_probes_tank_section_needs_no_sender_keys(tmp_path):
    config = _load_with_local(
        tmp_path,
        "[tank.black]\nsensor_type = probes\nprobes = 2:100\nfixed_resistor = 47000\n"
        "tank_capacity = 20\nvolume_unit = liters\nfluid_type = black_water\n",
    )
    black = next(t for t in config.tanks if t.id == "black")
    assert black.level_probes.marks_pct == [100.0]


def test_probe_channel_colliding_with_temp_channel_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="channel"):
        _load_with_local(
            tmp_path,
            "[tank.grey]\nsensor_type = probes\nprobes = 3:100\nfixed_resistor = 47000\n"
            "temp_channel = 3\n",
        )


def test_probes_tank_requires_probes_key(tmp_path):
    with pytest.raises(ValueError, match="probes"):
        _load_with_local(tmp_path, "[tank.grey]\nsensor_type = probes\nfixed_resistor = 47000\n")


def test_voltage_sensor_type_is_not_supported_yet(tmp_path):
    with pytest.raises(ValueError, match="voltage"):
        _load_with_local(tmp_path, "[tank.grey]\nsensor_type = voltage\n")


def test_unknown_sensor_type_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="sensor_type"):
        _load_with_local(tmp_path, "[tank.grey]\nsensor_type = float\n")
