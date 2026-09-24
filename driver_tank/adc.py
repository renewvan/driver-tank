"""Minimal ADS1115 single-ended voltage reader over I2C.

No Venus OS IIO-sysfs dependency (dbus-ads1115 read via /sys/bus/i2c
kernel IIO driver, which only exists on Venus OS images) — this talks to
the chip directly over smbus2, since it runs on plain Raspberry Pi OS.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# Register map (ADS1115 datasheet)
_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

_OS_SINGLE = 0x8000
_MUX_SINGLE_ENDED = {0: 0x4000, 1: 0x5000, 2: 0x6000, 3: 0x7000}
_PGA_MAP = {  # full-scale volts -> config bits
    6.144: 0x0000,
    4.096: 0x0200,
    2.048: 0x0400,
    1.024: 0x0600,
    0.512: 0x0800,
    0.256: 0x0A00,
}
_MODE_SINGLE_SHOT = 0x0100
_DR_128SPS = 0x0080
_COMP_DISABLE = 0x0003


def _closest_pga(pga: float) -> float:
    return min(_PGA_MAP, key=lambda v: abs(v - pga))


@dataclass
class ADS1115:
    bus_number: int
    address: int

    def __post_init__(self) -> None:
        import smbus2  # imported lazily so tests never need real hardware/I2C

        self._bus = smbus2.SMBus(self.bus_number)

    def read_voltage(self, channel: int, pga: float) -> float:
        """Trigger a single-shot conversion on `channel` and return volts."""
        pga_key = _closest_pga(pga)
        config = (
            _OS_SINGLE
            | _MUX_SINGLE_ENDED[channel]
            | _PGA_MAP[pga_key]
            | _MODE_SINGLE_SHOT
            | _DR_128SPS
            | _COMP_DISABLE
        )
        self._bus.write_i2c_block_data(self.address, _REG_CONFIG, [config >> 8, config & 0xFF])
        time.sleep(0.01)  # 128SPS conversion time + margin
        data = self._bus.read_i2c_block_data(self.address, _REG_CONVERSION, 2)
        raw = (data[0] << 8) | data[1]
        if raw > 32767:
            raw -= 65536
        return raw * pga_key / 32767.0

    def close(self) -> None:
        self._bus.close()
