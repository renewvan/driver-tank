"""Fixture-voltage tests for calibration math — no MQTT broker or hardware."""
from renewvan_tank.calibration import Calibration, Status, ShapePoint, apply_shape_correction, parse_shape


def european_calibration() -> Calibration:
    # European standard sender: 0 ohm empty, 180 ohm full.
    return Calibration(fixed_resistor=220, reference_voltage=3.3, sensor_min=0.0, sensor_max=180.0)


def us_calibration() -> Calibration:
    # US standard sender: 240 ohm empty, 30 ohm full (inverted range).
    return Calibration(fixed_resistor=220, reference_voltage=3.3, sensor_min=240.0, sensor_max=30.0)


def test_empty_tank_reads_zero_percent():
    cal = european_calibration()
    # 0 ohm sender -> 0V across the divider.
    level_pct, status = cal.read(voltage=0.0)
    assert status == Status.OK
    assert level_pct == 0.0


def test_full_tank_reads_near_hundred_percent():
    cal = european_calibration()
    # 180 ohm sender: V = 3.3 * 180 / (180 + 220) = 1.485V
    level_pct, status = cal.read(voltage=1.485)
    assert status == Status.OK
    assert abs(level_pct - 100.0) < 0.5


def test_mid_tank_reads_roughly_half():
    cal = european_calibration()
    # Resistance that lands near the midpoint of the 0-180 ohm span.
    # 90 ohm -> V = 3.3 * 90 / (90 + 220) = 0.9581V
    level_pct, status = cal.read(voltage=0.9581)
    assert status == Status.OK
    assert 45.0 < level_pct < 55.0


def test_us_standard_inverted_range():
    cal = us_calibration()
    # 240 ohm (empty) -> V = 3.3 * 240 / (240 + 220) = 1.7217V
    level_pct, status = cal.read(voltage=1.7217)
    assert status == Status.OK
    assert level_pct < 1.0

    # 30 ohm (full) -> V = 3.3 * 30 / (30 + 220) = 0.396V
    level_pct, status = cal.read(voltage=0.396)
    assert status == Status.OK
    assert level_pct > 99.0


def test_open_circuit_at_reference_voltage():
    cal = european_calibration()
    level_pct, status = cal.read(voltage=3.3)
    assert status == Status.OPEN_CIRCUIT
    assert level_pct == 0.0


def test_open_circuit_above_reference_voltage_is_clamped():
    cal = european_calibration()
    level_pct, status = cal.read(voltage=3.4)
    assert status == Status.OPEN_CIRCUIT
    assert level_pct == 0.0


def test_short_circuit_negative_voltage():
    cal = european_calibration()
    level_pct, status = cal.read(voltage=-0.1)
    assert status == Status.SHORT_CIRCUIT
    assert level_pct == 0.0


def test_shape_correction_pulls_linear_reading_toward_curve():
    shape = parse_shape("50:20")  # sensor reads 50% when tank is actually 20% full
    assert shape == [ShapePoint(50.0, 20.0)]
    assert apply_shape_correction(50.0, shape) == 20.0
    assert apply_shape_correction(0.0, shape) == 0.0
    assert apply_shape_correction(100.0, shape) == 100.0
    # Halfway between the 0% anchor and the 50% shape point interpolates linearly.
    assert apply_shape_correction(25.0, shape) == 10.0


def test_shape_correction_noop_without_shape_points():
    assert apply_shape_correction(37.5, []) == 37.5


def test_parse_shape_ignores_malformed_string():
    assert parse_shape("not-a-valid-shape") == []
    assert parse_shape("") == []
    assert parse_shape(None) == []
