"""Probe excitation: power a Level probes divider only while it is read.

DC left on conductive probes corrodes them by electrolysis. When a tank sets
`probe_excitation_gpio`, the divider's top is fed from that GPIO instead of
3.3V, switched on just before the probes are read and off right after.
"""

from __future__ import annotations

from typing import Protocol


class Excitation(Protocol):
    def on(self) -> None: ...

    def off(self) -> None: ...


class NoExcitation:
    """Divider permanently fed from 3.3V: nothing to switch."""

    def on(self) -> None:
        pass

    def off(self) -> None:
        pass

    def close(self) -> None:
        pass


class GpioExcitation:
    """One output line on a gpiochip, held low between reads."""

    def __init__(self, chip: str, line: int) -> None:
        import gpiod  # imported lazily so tests never need real hardware
        from gpiod.line import Direction, Value

        self._line = line
        self._active, self._inactive = Value.ACTIVE, Value.INACTIVE
        self._request = gpiod.request_lines(
            chip,
            consumer="driver-tank",
            config={
                line: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
            },
        )

    def on(self) -> None:
        self._request.set_value(self._line, self._active)

    def off(self) -> None:
        self._request.set_value(self._line, self._inactive)

    def close(self) -> None:
        self.off()
        self._request.release()
