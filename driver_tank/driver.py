"""Publish loop: ADC read -> calibration math -> van/tank/<id>/<property>.

Field/topic shape per hub's schema/tank.schema.json and
docs/porting-dbus-driver-to-mqtt.md: fluid_type/capacity_l published
retained at startup (identity fields, rarely change), level_pct/status
republished retained on every read (live fields).
"""
from __future__ import annotations

import json
import logging
import time

from driver_tank.adc import ADS1115
from driver_tank.config import AppConfig, TankConfig
from driver_tank.publisher import Publisher

logger = logging.getLogger(__name__)

_PGA_DEFAULT = 4.096


def _topic(tank_id: str, prop: str) -> str:
    return f"van/tank/{tank_id}/{prop}"


def publish_identity(publisher: Publisher, tank: TankConfig) -> None:
    publisher.publish(_topic(tank.id, "fluid_type"), json.dumps(tank.fluid_type))
    publisher.publish(_topic(tank.id, "capacity_l"), json.dumps(tank.capacity_l))


def read_and_publish(publisher: Publisher, adc: ADS1115, tank: TankConfig) -> None:
    voltage = adc.read_voltage(tank.channel, _PGA_DEFAULT)
    level_pct, status = tank.calibration.read(voltage)
    publisher.publish(_topic(tank.id, "level_pct"), json.dumps(round(level_pct, 1)))
    publisher.publish(_topic(tank.id, "status"), json.dumps(status.value))
    logger.info(
        "tank=%s voltage=%.4fV level_pct=%.1f status=%s", tank.id, voltage, level_pct, status.value
    )


def run(config: AppConfig) -> None:
    publisher = Publisher(config.mqtt)
    publisher.connect()
    adc = ADS1115(bus_number=config.i2c_bus, address=config.i2c_address)

    for tank in config.tanks:
        publish_identity(publisher, tank)

    try:
        while True:
            for tank in config.tanks:
                try:
                    read_and_publish(publisher, adc, tank)
                except Exception:
                    logger.exception("Read failed for tank=%s", tank.id)
            time.sleep(min(t.update_interval_ms for t in config.tanks) / 1000.0)
    finally:
        adc.close()
        publisher.disconnect()
