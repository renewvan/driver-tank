"""Fixture-voltage tests for the NTC divider -> beta-equation conversion.

Prior art: tests/test_calibration.py (fixture input, pure function, no I/O).
Fixture points computed from the beta equation for a generic 10k B=3950 NTC
with a 10k fixed resistor on a 3.3V rail:

- 25 C  -> R = 10 000 ohm  (tap at mid-rail in either orientation)
- 0 C   -> R = 33 618 ohm  (low-side tap 2.5440 V, high-side 0.7566 V)
- 60 C  -> R =  2 486 ohm  (low-side tap 0.6570 V, high-side 2.6430 V)
"""

import pytest

from node_tank.temperature import NTCConversion


def make_conversion(*, thermistor_low_side: bool) -> NTCConversion:
    return NTCConversion(
        nominal_ohm=10000.0,
        beta=3950.0,
        fixed_resistor_ohm=10000.0,
        thermistor_low_side=thermistor_low_side,
        reference_voltage=3.3,
    )


@pytest.mark.parametrize(
    ("thermistor_low_side", "voltage", "expected_c"),
    [
        # 25 C: R = R0 -> mid-rail tap in both orientations
        (True, 1.65, 25.0),
        (False, 1.65, 25.0),
        # 0 C: R = 33 618 ohm
        (True, 2.5440, 0.0),
        (False, 0.7566, 0.0),
        # 60 C: R = 2 486 ohm
        (True, 0.6570, 60.0),
        (False, 2.6430, 60.0),
    ],
)
def test_beta_conversion_both_orientations(thermistor_low_side, voltage, expected_c):
    conversion = make_conversion(thermistor_low_side=thermistor_low_side)
    assert conversion.read_temperature_c(voltage) == pytest.approx(expected_c, abs=0.05)


def test_low_side_open_circuit_at_rail_raises():
    # Open thermistor pulls the low-side tap all the way to the 3.3V rail.
    with pytest.raises(ValueError, match="open circuit"):
        make_conversion(thermistor_low_side=True).read_temperature_c(3.3)


def test_low_side_open_circuit_above_threshold_raises():
    # Tap near the rail: R = 10000 * 3.29 / 0.01 = 3.29 Mohm >> 100k threshold.
    with pytest.raises(ValueError, match="open circuit"):
        make_conversion(thermistor_low_side=True).read_temperature_c(3.29)


def test_low_side_short_circuit_at_ground_raises():
    # Shorted thermistor pulls the low-side tap to ground.
    with pytest.raises(ValueError, match="short circuit"):
        make_conversion(thermistor_low_side=True).read_temperature_c(0.0)


def test_high_side_open_circuit_at_ground_raises():
    # Open thermistor pulls the high-side tap to ground.
    with pytest.raises(ValueError, match="open circuit"):
        make_conversion(thermistor_low_side=False).read_temperature_c(0.0)


def test_high_side_short_circuit_at_rail_raises():
    # Shorted thermistor pulls the high-side tap to the 3.3V rail.
    with pytest.raises(ValueError, match="short circuit"):
        make_conversion(thermistor_low_side=False).read_temperature_c(3.3)


def test_reference_voltage_override_is_honored():
    # The reference_voltage key feeds the divider math, not the chip: a
    # differently-supplied divider still reads R=R0 as 25 C at mid-rail.
    conversion = NTCConversion(
        nominal_ohm=10000.0,
        beta=3950.0,
        fixed_resistor_ohm=10000.0,
        thermistor_low_side=True,
        reference_voltage=5.0,
    )
    assert conversion.read_temperature_c(2.5) == pytest.approx(25.0, abs=0.05)
