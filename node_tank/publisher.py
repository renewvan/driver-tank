"""Persistent MQTT connection with LWT and a retained-topic publish helper.

Per docs/porting-dbus-to-mqtt-node.md: connect with retry/backoff, an
LWT on a status topic so downstream consumers can detect a dead node,
and a long-lived connection (this node is the source of truth, not a
re-publisher).

Ticket 06: Added subscription support for command topics (last_inspected_date/set).
"""
from __future__ import annotations

import logging
from typing import Callable

import paho.mqtt.client as mqtt

from node_tank.config import MqttConfig

logger = logging.getLogger(__name__)

HEALTH_TOPIC = "renewvan/tank/health"


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
        self._client.on_message = self._on_message
        self._subscriptions: dict[str, Callable[[str], None]] = {}

    def _on_connect(self, client, userdata, flags, rc):  # noqa: ANN001
        if rc == 0:
            logger.info("Connected to MQTT broker %s:%s", self._config.host, self._config.port)
            client.publish(HEALTH_TOPIC, payload="online", qos=1, retain=True)
            # Re-subscribe to all topics after reconnect
            for topic in self._subscriptions:
                client.subscribe(topic, qos=1)
        else:
            logger.error("MQTT connect failed with rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):  # noqa: ANN001
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%s); paho will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg):  # noqa: ANN001
        """Handle incoming MQTT messages for subscribed topics."""
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        if topic in self._subscriptions:
            try:
                self._subscriptions[topic](payload)
            except Exception:
                logger.exception(f"Error handling message on topic {topic}")

    def connect(self) -> None:
        self._client.connect(self._config.host, self._config.port)
        self._client.loop_start()

    def publish(self, topic: str, payload: str) -> None:
        self._client.publish(topic, payload=payload, qos=1, retain=True)

    def subscribe(self, topic: str, callback: Callable[[str], None]) -> None:
        """Subscribe to a topic and call callback with the payload (ticket 06).
        
        Args:
            topic: MQTT topic to subscribe to
            callback: Function to call with payload string when message arrives
        """
        self._subscriptions[topic] = callback
        self._client.subscribe(topic, qos=1)
        logger.info(f"Subscribed to {topic}")

    def disconnect(self) -> None:
        self._client.publish(HEALTH_TOPIC, payload="offline", qos=1, retain=True)
        self._client.loop_stop()
        self._client.disconnect()
