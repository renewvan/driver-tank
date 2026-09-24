#!/usr/bin/env python3
"""renewvan-tank entrypoint."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from renewvan_tank.config import DEFAULT_CONFIG_PATH, load_config
from renewvan_tank.driver import run


def main() -> None:
    parser = argparse.ArgumentParser(description="renewvan-tank: MQTT tank driver")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "-c", "--config", default="config.ini", help="Path to local config.ini override"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    config = load_config(default_path=DEFAULT_CONFIG_PATH, local_path=Path(args.config))
    run(config)


if __name__ == "__main__":
    main()
