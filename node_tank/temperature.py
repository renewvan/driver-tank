"""Minimal DS18B20 1-Wire temperature reader.

Reads from /sys/bus/w1/devices/<rom_id>/w1_slave (raw sysfs interface).
No external dependencies beyond stdlib pathlib/open. Matches adc.py's I/O-boundary pattern.

Ticket 05: DS18B20 config/topic shape.
Research: research/04-ds18b20.md.
"""
from __future__ import annotations

from pathlib import Path


class DS18B20:
    """Single DS18B20 sensor reader via sysfs."""

    def __init__(self, rom_id: str):
        """Initialize with 1-Wire ROM ID (e.g., '28-0521a2e0cfff').

        Args:
            rom_id: The 64-bit 1-Wire ROM ID string.

        Raises:
            FileNotFoundError: If the sensor sysfs path doesn't exist.
        """
        self.rom_id = rom_id
        self._w1_path = Path(f"/sys/bus/w1/devices/{rom_id}/w1_slave")

        if not self._w1_path.exists():
            raise FileNotFoundError(
                f"DS18B20 sensor not found: {self._w1_path}. "
                f"Verify 1-Wire is enabled (dtoverlay=w1-gpio in /boot/config.txt) "
                f"and the ROM ID is correct."
            )

    def read_temperature_c(self) -> float:
        """Read temperature in Celsius.

        Reads /sys/bus/w1/devices/<rom_id>/w1_slave (kernel w1_therm driver):
        Line 1: ends YES/NO (CRC check)
        Line 2: t=<millidegrees C>

        Returns:
            Temperature in degrees Celsius.

        Raises:
            ValueError: If CRC check fails or temperature format is invalid.
        """
        try:
            content = self._w1_path.read_text()
        except OSError as e:
            raise ValueError(f"Failed to read {self._w1_path}: {e}") from e

        lines = content.strip().split("\n")
        if len(lines) < 2:
            raise ValueError(f"Invalid w1_slave format (expected 2 lines, got {len(lines)})")

        # Line 1: CRC check
        if not lines[0].endswith("YES"):
            raise ValueError(f"CRC check failed: {lines[0]}")

        # Line 2: extract t=<millidegrees>
        line2 = lines[1]
        if "t=" not in line2:
            raise ValueError(f"Temperature value not found in: {line2}")

        try:
            # Extract the millidegrees value
            t_str = line2.split("t=")[1].strip()
            t_millidegrees = float(t_str)
            return t_millidegrees / 1000.0
        except (IndexError, ValueError) as e:
            raise ValueError(f"Failed to parse temperature from '{line2}': {e}") from e
