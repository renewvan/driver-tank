"""Level probes: wet/dry with hysteresis and the Stepped level -- pure, no I/O."""

import pytest

from driver_tank.probes import LevelProbes, Probe, parse_probes


def make_probes(*marks: float, wet_max_ohm=100_000.0, dry_min_ohm=150_000.0) -> LevelProbes:
    return LevelProbes(
        probes=tuple(Probe(channel=i, mark_pct=m) for i, m in enumerate(marks)),
        fixed_resistor=47_000.0,
        reference_voltage=3.3,
        wet_max_ohm=wet_max_ohm,
        dry_min_ohm=dry_min_ohm,
    )


def voltage_for(ohm: float, fixed: float = 47_000.0, vref: float = 3.3) -> float:
    return vref * ohm / (ohm + fixed)


# --- wet/dry --------------------------------------------------------------


def test_open_probe_reads_dry():
    assert make_probes(100).is_wet(3.3, was_wet=False) is False


def test_water_bridged_probe_reads_wet():
    assert make_probes(100).is_wet(voltage_for(10_000), was_wet=False) is True


def test_between_thresholds_keeps_previous_state():
    probes = make_probes(100)
    v = voltage_for(120_000)  # above wet_max, below dry_min
    assert probes.is_wet(v, was_wet=True) is True
    assert probes.is_wet(v, was_wet=False) is False


def test_wet_probe_goes_dry_above_dry_min():
    assert make_probes(100).is_wet(voltage_for(200_000), was_wet=True) is False


# --- stepped level ----------------------------------------------------------


def test_all_dry_is_zero():
    assert make_probes(33, 66, 100).stepped_level([False, False, False]) == (0.0, True)


def test_level_is_mark_of_highest_wet_probe():
    assert make_probes(33, 66, 100).stepped_level([True, True, False]) == (66.0, True)


def test_single_full_probe_wet_is_its_mark():
    assert make_probes(100).stepped_level([True]) == (100.0, True)


def test_wet_above_dry_is_inconsistent_but_still_highest_wet():
    assert make_probes(33, 66, 100).stepped_level([False, True, False]) == (66.0, False)


def test_marks_are_ascending():
    assert make_probes(33, 66, 100).marks_pct == [33.0, 66.0, 100.0]


# --- parse_probes -----------------------------------------------------------


def test_parse_probes_sorts_by_mark_and_applies_shape():
    from driver_tank.calibration import ShapePoint

    probes = parse_probes("2:100, 1:50", shape=[ShapePoint(50.0, 40.0)])
    assert probes == (Probe(channel=1, mark_pct=40.0), Probe(channel=2, mark_pct=100.0))


@pytest.mark.parametrize(
    "value",
    ["", "1", "1:abc", "4:50", "-1:50", "1:0", "1:101", "1:50,1:80", "1:50,2:50"],
)
def test_parse_probes_rejects_bad_values(value):
    with pytest.raises(ValueError):
        parse_probes(value, shape=[])
