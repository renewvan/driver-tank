"""Voltage -> resistance -> tank-level-percentage conversion math.

Ported near-verbatim from dbus-ads1115's TankSensor domain logic
(see /Users/alejandrosanchezbautista/Projects/VenusOS/dbus-ads1115/dbus_ads1115/sensors.py),
stripped of every D-Bus/Venus-OS/settings concern. Pure functions and a
small stateless Calibration value object: no MQTT, no I/O, no hardware
access — safe to unit test with fixture voltages only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    """Sensor fault status. Matches hub's tank.schema.json `status` enum."""

    OK = "ok"
    OPEN_CIRCUIT = "open_circuit"
    SHORT_CIRCUIT = "short_circuit"


@dataclass(frozen=True)
class ShapePoint:
    """A piecewise tank-shape correction point: raw sensor % -> true volume %."""

    sensor_pct: float
    volume_pct: float


def parse_shape(value: str) -> list[ShapePoint]:
    """Parse a "10:5,50:40,80:90" shape string into sorted ShapePoints.

    Same wire format dbus-ads1115 used for its /Shape D-Bus path. 0% and
    100% are implicit and never stored. Returns [] if value is falsy or
    malformed (falls back to a linear tank shape).
    """
    if not value:
        return []
    try:
        points: list[ShapePoint] = []
        for token in str(value).split(","):
            token = token.strip()
            if not token:
                continue
            sensor_s, vol_s = token.split(":")
            points.append(ShapePoint(float(sensor_s), float(vol_s)))
        points.sort(key=lambda p: p.sensor_pct)
        return points
    except Exception:
        return []


def apply_shape_correction(linear_pct: float, shape: list[ShapePoint]) -> float:
    """Map a linear resistance-derived percentage onto the tank's true shape.

    Piecewise-linear interpolation through the implicit (0, 0) and
    (100, 100) endpoints plus any configured intermediate points, e.g. for
    a cylindrical tank on its side where equal height changes don't give
    equal volume changes.
    """
    if not shape:
        return linear_pct

    points = [(0.0, 0.0)] + [(p.sensor_pct, p.volume_pct) for p in shape] + [(100.0, 100.0)]
    x = max(0.0, min(100.0, linear_pct))

    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    return x  # unreachable given clamping above


@dataclass(frozen=True)
class Calibration:
    """Per-sensor calibration: wiring + resistance-at-empty/full + tank shape.

    `sensor_min`/`sensor_max` are resistances in ohms at empty/full — not
    required to be ordered (e.g. US-standard senders run 240ohm empty ->
    30ohm full).
    """

    fixed_resistor: float
    reference_voltage: float
    sensor_min: float
    sensor_max: float
    shape: list[ShapePoint] = field(default_factory=list)
    # Resistance readings beyond sensor_max * this factor are treated as an
    # open circuit (sender wire disconnected) rather than "100%+".
    open_circuit_factor: float = 10.0

    def voltage_to_resistance(self, voltage: float) -> float:
        """Resistive divider: Vout = Vref * R_sensor / (R_sensor + R_fixed).

        Negative voltage (reversed polarity / shorted sender pulling the ADC
        input below ground) is passed through the formula rather than
        clamped, so it naturally yields a negative resistance that
        `status_for_resistance` classifies as SHORT_CIRCUIT. Exactly 0V is a
        legitimate empty-tank reading for a 0-ohm-at-empty sender.
        """
        if voltage >= self.reference_voltage:
            return float("inf")
        if voltage == 0:
            return 0.0
        return (voltage * self.fixed_resistor) / (self.reference_voltage - voltage)

    def resistance_to_percentage(self, resistance: float) -> float:
        """Linear resistance -> percentage mapping, then shape-corrected."""
        if self.sensor_max == self.sensor_min:
            return 0.0
        span = self.sensor_max - self.sensor_min
        linear_pct = (resistance - self.sensor_min) / span * 100
        linear_pct = max(0.0, min(100.0, linear_pct))
        return apply_shape_correction(linear_pct, self.shape)

    def status_for_resistance(self, resistance: float) -> Status:
        """Classify a resistance reading as ok / open_circuit / short_circuit."""
        sensor_span_max = max(self.sensor_min, self.sensor_max)
        if resistance == float("inf") or resistance > sensor_span_max * self.open_circuit_factor:
            return Status.OPEN_CIRCUIT
        if resistance < 0:
            return Status.SHORT_CIRCUIT
        return Status.OK

    def read(self, voltage: float) -> tuple[float, Status]:
        """Convert a measured sender voltage to (level_pct, status).

        level_pct is 0.0 whenever status != OK — a faulted sender has no
        meaningful level reading.
        """
        resistance = self.voltage_to_resistance(voltage)
        status = self.status_for_resistance(resistance)
        if status != Status.OK:
            return 0.0, status
        return self.resistance_to_percentage(resistance), status
