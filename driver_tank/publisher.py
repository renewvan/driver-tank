"""Persistent MQTT connection with LWT and a retained-topic publish helper.

Per docs/porting-dbus-driver-to-mqtt.md: connect with retry/backoff, an
LWT on a status topic so downstream consumers can detect a dead driver,
and a long-lived connection (this driver is the source of truth, not a
re-publisher).
"""
from __future__ import annotations

import logging

import paho.mqtt.client as mqtt

from driver_tank.config import MqttConfig

logger = logging.getLogger(__name__)

HEALTH_TOPIC = "van/tank/driver/status"


class Publisher:
    def __init__(self, config: MqttConfig) -> None:
        self._config = config
        self._client = mqtt.Client(client_id=config.client_id, clean_session=False)
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.will_set(HEALTH_TOPIC, payload="offline", qos=1, retain=True)
        self._client.reconnect_delay_set(
            min_delay=config.reconnect_min_delay, max_delay=config.reconnect_max_delay
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):  # noqa: ANN001
        if rc == 0:
            logger.info("Connected to MQTT broker %s:%s", self._config.host, self._config.port)
            client.publish(HEALTH_TOPIC, payload="online", qos=1, retain=True)
        else:
            logger.error("MQTT connect failed with rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):  # noqa: ANN001
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%s); paho will auto-reconnect", rc)

    def connect(self) -> None:
        self._client.connect(self._config.host, self._config.port)
        self._client.loop_start()

    def publish(self, topic: str, payload: str) -> None:
        self._client.publish(topic, payload=payload, qos=1, retain=True)

    def disconnect(self) -> None:
        self._client.publish(HEALTH_TOPIC, payload="offline", qos=1, retain=True)
        self._client.loop_stop()
        self._client.disconnect()
