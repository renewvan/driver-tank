"""Level probes: conductive wet/dry points at fixed heights -> a Stepped level.

Each probe sits on its own ADS1115 channel through the same divider as a
sender (3.3V -- fixed_resistor -- channel -- probe -- water -- common/GND),
but with a large fixed_resistor (tens of kohm). A dry probe is an open
circuit (the normal reading, not a fault); water bridging it pulls the
channel down. Pure value objects: no MQTT, no I/O. See hub ADR 0033.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from driver_tank.calibration import ShapePoint, apply_shape_correction

ADS1115_CHANNELS = range(4)


@dataclass(frozen=True)
class Probe:
    channel: int
    mark_pct: float  # Level mark: volume %, after shape correction


@dataclass(frozen=True)
class LevelProbes:
    """A tank's Level probes, ascending by mark, plus their wet/dry thresholds.

    Hysteresis: a dry probe turns wet below `wet_max_ohm`; a wet probe turns
    dry only above `dry_min_ohm`, so a reading near the boundary doesn't flicker.
    """

    probes: tuple[Probe, ...]
    fixed_resistor: float
    reference_voltage: float
    wet_max_ohm: float
    dry_min_ohm: float
    excitation_gpio: int | None = None
    settle_ms: float = 5.0

    @property
    def marks_pct(self) -> list[float]:
        return [p.mark_pct for p in self.probes]

    def resistance(self, voltage: float) -> float:
        if voltage >= self.reference_voltage:
            return float("inf")
        if voltage <= 0:
            return 0.0
        return voltage * self.fixed_resistor / (self.reference_voltage - voltage)

    def is_wet(self, voltage: float, was_wet: bool) -> bool:
        ohm = self.resistance(voltage)
        if was_wet:
            return ohm <= self.dry_min_ohm
        return ohm < self.wet_max_ohm

    def stepped_level(self, wet: Sequence[bool]) -> tuple[float, bool]:
        """(level_pct, consistent): the mark of the highest wet probe, 0 if all dry.

        `consistent` is False when a wet probe sits above a dry one, which is
        physically impossible (a fouled or miswired probe); the level is still
        the highest wet mark, per the Stepped level definition.
        """
        level = 0.0
        seen_dry = False
        consistent = True
        for probe, is_wet in zip(self.probes, wet):
            if is_wet:
                level = probe.mark_pct
                if seen_dry:
                    consistent = False
            else:
                seen_dry = True
        return level, consistent


def parse_probes(value: str, shape: list[ShapePoint]) -> tuple[Probe, ...]:
    """Parse "channel:height_pct,..." into Probes sorted by mark.

    Heights are the probe tips as % of tank depth; the shape correction maps
    them to volume %, so marks and level_pct speak the same unit.
    """
    probes: list[Probe] = []
    for token in (t.strip() for t in str(value).split(",")):
        if not token:
            continue
        try:
            channel_s, height_s = token.split(":")
            channel, height = int(channel_s), float(height_s)
        except ValueError:
            raise ValueError(f"probe {token!r}: expected <channel>:<height_pct>") from None
        if channel not in ADS1115_CHANNELS:
            raise ValueError(f"probe {token!r}: channel must be 0-3")
        if not 0 < height <= 100:
            raise ValueError(f"probe {token!r}: height must be in (0, 100]")
        probes.append(Probe(channel=channel, mark_pct=apply_shape_correction(height, shape)))
    if not probes:
        raise ValueError("probes: at least one <channel>:<height_pct> is required")
    channels = [p.channel for p in probes]
    marks = [p.mark_pct for p in probes]
    if len(set(channels)) != len(channels):
        raise ValueError(f"probes {value!r}: duplicate channel")
    if len(set(marks)) != len(marks):
        raise ValueError(f"probes {value!r}: duplicate mark")
    return tuple(sorted(probes, key=lambda p: p.mark_pct))
