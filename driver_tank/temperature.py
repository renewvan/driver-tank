"""NTC thermistor temperature reading via the ADS1115.

Replaces the DS18B20 1-Wire reader (ntc-temperature-sensor effort):
water temperature now comes from a 10k NTC thermistor wired as a
voltage divider on a spare ADS1115 channel, read through the same
adc.read_voltage() call as the level senders.

Pure math (voltage -> resistance -> Celsius), no I/O -- matches
calibration.py's testability pattern: fixture voltages in, degrees out.

Divider orientation is config-driven because installs differ:

- low-side thermistor (fixed R to 3.3V): tap voltage falls as it warms
      R_ntc = R_fixed * V / (Vcc - V)
- high-side thermistor (fixed R to GND): tap voltage rises as it warms
      R_ntc = R_fixed * (Vcc - V) / V

Failure classification mirrors calibration.py's resistance -> status
pattern: R > 10x nominal -> open circuit, R < nominal/20 -> short
circuit. Both raise ValueError; the caller warns and skips the publish.

Research: hub repo, .scratch/ntc-temperature-sensor/issues/01-ntc-reading-math.md
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_T_REF_K = 298.15  # 25 °C, the reference point for nominal_ohm/beta
_OPEN_CIRCUIT_FACTOR = 10.0
_SHORT_CIRCUIT_FACTOR = 1.0 / 20.0


@dataclass(frozen=True)
class NTCConversion:
    """Voltage -> Celsius for one thermistor divider.

    Built by config.py when a tank has temp_channel set; the driver feeds
    it raw ADS1115 tap voltages.
    """

    nominal_ohm: float  # thermistor resistance at 25 C
    beta: float  # B-value (e.g. 3950 for generic 10k NTCs)
    fixed_resistor_ohm: float  # the other divider leg
    thermistor_low_side: bool  # True: fixed R to 3.3V; False: fixed R to GND
    reference_voltage: float  # divider supply (3.3V on the Pi -- never 5V)

    def resistance_ohm(self, voltage: float) -> float:
        """Thermistor resistance implied by the divider tap voltage.

        Passed through unclamped (like calibration.py) so pegged-rail
        readings classify as open/short rather than silently clamping.
        """
        if self.thermistor_low_side:
            # V = Vcc * R_ntc / (R_fixed + R_ntc); pegs at Vcc when open
            if voltage >= self.reference_voltage:
                return math.inf
            return self.fixed_resistor_ohm * voltage / (self.reference_voltage - voltage)
        # V = Vcc * R_fixed / (R_ntc + R_fixed); pegs at 0 when open
        if voltage <= 0:
            return math.inf
        return self.fixed_resistor_ohm * (self.reference_voltage - voltage) / voltage

    def temperature_c(self, resistance_ohm: float) -> float:
        """Beta equation: 1/T = 1/T0 + (1/B) * ln(R / R0)."""
        inverse_t = 1.0 / _T_REF_K + math.log(resistance_ohm / self.nominal_ohm) / self.beta
        return 1.0 / inverse_t - 273.15

    def read_temperature_c(self, voltage: float) -> float:
        """Tap voltage -> degrees Celsius.

        Raises:
            ValueError: open circuit (R > 10x nominal) or short circuit
                (R < nominal/20), matching the DS18B20 reader's contract.
        """
        resistance = self.resistance_ohm(voltage)
        if resistance > self.nominal_ohm * _OPEN_CIRCUIT_FACTOR:
            raise ValueError(
                f"NTC open circuit: {resistance:.0f} ohm > "
                f"{self.nominal_ohm * _OPEN_CIRCUIT_FACTOR:.0f} ohm (V={voltage:.4f})"
            )
        if resistance < self.nominal_ohm * _SHORT_CIRCUIT_FACTOR:
            raise ValueError(
                f"NTC short circuit: {resistance:.0f} ohm < "
                f"{self.nominal_ohm * _SHORT_CIRCUIT_FACTOR:.0f} ohm (V={voltage:.4f})"
            )
        return self.temperature_c(resistance)
