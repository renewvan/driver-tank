"""Driver info: manifest.json + stamped version + Board serial, serialised for the bus.

Copied (not imported) into every renewvan Python driver repo; see hub
docs/adr/0011-node-manifest-and-retained-info-topic.md for why there is no
shared package. Contract: hub `schema/driver-info.schema.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

CPUINFO_PATH = Path("/proc/cpuinfo")


def read_board_serial(cpuinfo_path: Path = CPUINFO_PATH) -> str | None:
    """The `Serial` line of the board's /proc/cpuinfo; None when unreadable, empty or all zeros.

    Never a placeholder: an all-zero serial would make every Pi with a failing
    kernel look identical.
    """
    try:
        lines = cpuinfo_path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition(":")
        if key.strip() == "Serial":
            serial = value.strip()
            return serial if serial.strip("0") else None
    return None


_HERE = Path(__file__).resolve().parent
# Image build copies manifest.json beside this module; a checkout run falls
# back to the repo-root file.
MANIFEST_PATHS = (_HERE / "manifest.json", _HERE.parent / "manifest.json")


def _find_manifest() -> Path:
    return next((p for p in MANIFEST_PATHS if p.is_file()), MANIFEST_PATHS[-1])


def build_info(
    version: str | None, board_serial: str | None, manifest_path: Path | None = None
) -> str:
    """Driver info JSON: manifest fields, `version` (or `dev`), `board_serial` when known."""
    info = json.loads((manifest_path or _find_manifest()).read_text())
    info["version"] = version or "dev"
    if board_serial:
        info["board_serial"] = board_serial
    return json.dumps(info)
