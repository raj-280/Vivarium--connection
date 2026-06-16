"""
server/services/mqtt_client.py

paho-mqtt wrapper for the server's MQTT connection (Section 4.2 / Section 11).

Responsibilities
────────────────
• Connect to Mosquitto with credentials + optional TLS from settings.
• Subscribe to vivarium/rack/+/# (all rack traffic).
• Publish commands with the correct QoS per Section 11:
      vivarium/rack/{id}/command    QoS 1   (motion, G28, M114, …)
      vivarium/rack/{id}/emergency  QoS 2   (emergency stop !)
      vivarium/rack/{id}/status     QoS 0   (heartbeat)
      vivarium/rack/{id}/image      QoS 1
      vivarium/rack/{id}/scan_progress QoS 0
      vivarium/rack/{id}/scan_status   QoS 1
• Dispatch received messages to registered subtopic handlers.
• [PROD ONLY] TLS when MQTT_USE_TLS=true + MQTT_TLS_CA_PATH set.

Module-level singleton:
    from services.mqtt_client import mqtt_client
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

from config.settings import settings

logger = logging.getLogger(__name__)


# ── Topic templates (Section 11) ──────────────────────────────────────────────

TOPIC_COMMAND        = "vivarium/rack/{rack_id}/command"
TOPIC_RESPONSE       = "vivarium/rack/{rack_id}/response"
TOPIC_STATUS         = "vivarium/rack/{rack_id}/status"
TOPIC_EMERGENCY      = "vivarium/rack/{rack_id}/emergency"
TOPIC_IMAGE          = "vivarium/rack/{rack_id}/image"
TOPIC_SCAN_PROGRESS  = "vivarium/rack/{rack_id}/scan_progress"
TOPIC_SCAN_STATUS    = "vivarium/rack/{rack_id}/scan_status"
TOPIC_BROADCAST      = "vivarium/all/command"
SUBSCRIBE_PATTERN    = "vivarium/rack/+/#"   # Server subscribes to all rack traffic

# QoS constants (Section 11)
QOS_COMMAND          = 1
QOS_EMERGENCY        = 2
QOS_STATUS           = 0
QOS_IMAGE            = 1
QOS_SCAN_PROGRESS    = 0
QOS_SCAN_STATUS      = 1


# ── Topic parsing helpers ─────────────────────────────────────────────────────

def extract_rack_id(topic: str) -> Optional[str]:
    """'vivarium/rack/rack-001/response' → 'rack-001'"""
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "vivarium" and parts[1] == "rack":
        return parts[2]
    return None


def extract_subtopic(topic: str) -> Optional[str]:
    """'vivarium/rack/rack-001/response' → 'response'"""
    parts = topic.split("/")
    return parts[3] if len(parts) >= 4 else None


# ── Handler type ──────────────────────────────────────────────────────────────

# (rack_id: str | None, subtopic: str, payload: str | dict | bytes) -> None
MessageHandler = Callable[[Optional[str], str, Any], None]


# ── VivMQTTClient ─────────────────────────────────────────────────────────────

class VivMQTTClient:
    """
    Server-side paho-mqtt wrapper.

    connect()          — connect to broker, start network loop, wait for on_connect.
    disconnect()       — stop loop, disconnect gracefully.
    publish_command()  — publish to the command topic (QoS 1).
    publish_emergency()— publish ! to the emergency topic (QoS 2).
    register_handler() — subscribe a callback to a specific subtopic pattern.
    is_connected       — bool property.
    """

    def __init__(self) -> None:
        self._client = mqtt.Client(
            client_id="vivarium-server",
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self._connected   = threading.Event()
        self._handlers: list[tuple[str, MessageHandler]] = []
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self, timeout_s: float = 5.0) -> None:
        """
        Connect to the Mosquitto broker and start paho's background network loop.
        Blocks until connected or timeout_s elapses.
        Raises RuntimeError on timeout.
        """
        if settings.MQTT_USERNAME:
            self._client.username_pw_set(settings.MQTT_USERNAME, settings.MQTT_PASSWORD)

        # [PROD ONLY] TLS — config-flag controlled; inactive locally
        if settings.MQTT_USE_TLS and settings.MQTT_TLS_CA_PATH:
            self._client.tls_set(ca_certs=settings.MQTT_TLS_CA_PATH)
            logger.info("MQTT TLS enabled (CA: %s)", settings.MQTT_TLS_CA_PATH)

        logger.info(
            "Connecting to MQTT broker %s:%d …",
            settings.MQTT_BROKER,
            settings.MQTT_PORT,
        )
        self._client.connect(
            settings.MQTT_BROKER,
            settings.MQTT_PORT,
            keepalive=60,
        )
        self._client.loop_start()

        if not self._connected.wait(timeout=timeout_s):
            self._client.loop_stop()
            raise RuntimeError(
                f"MQTT connection to {settings.MQTT_BROKER}:{settings.MQTT_PORT} "
                f"timed out after {timeout_s:.0f}s."
            )
        logger.info("MQTT connected and subscribed to %s", SUBSCRIBE_PATTERN)

    def disconnect(self) -> None:
        """Graceful shutdown: stop network loop, then disconnect."""
        self._client.loop_stop()
        self._client.disconnect()
        self._connected.clear()
        logger.info("MQTT disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── paho callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected.set()
            client.subscribe(SUBSCRIBE_PATTERN, qos=1)
        else:
            logger.error(
                "MQTT connection refused (rc=%d). Check broker address and credentials.", rc
            )

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected.clear()
        if rc != 0:
            logger.warning(
                "MQTT unexpected disconnect (rc=%d). paho will attempt reconnection.", rc
            )
        else:
            logger.info("MQTT disconnected cleanly.")

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        topic    = msg.topic
        rack_id  = extract_rack_id(topic)
        subtopic = extract_subtopic(topic) or ""

        # Decode payload: try JSON first, fall back to raw string
        try:
            payload_str = msg.payload.decode("utf-8")
            try:
                payload: Any = json.loads(payload_str)
            except json.JSONDecodeError:
                payload = payload_str
        except Exception:
            payload = msg.payload  # keep as bytes

        logger.debug("MQTT RX: topic=%s payload=%r", topic, payload)

        for pattern, handler in self._handlers:
            if pattern == "*" or pattern == subtopic:
                try:
                    handler(rack_id, subtopic, payload)
                except Exception:
                    logger.exception(
                        "MQTT handler error: pattern=%r rack=%s subtopic=%s",
                        pattern, rack_id, subtopic,
                    )

    # ── Publish API ───────────────────────────────────────────────────────────

    def publish_command(
        self,
        rack_id: str,
        command: str,
        qos: int = QOS_COMMAND,
    ) -> None:
        """
        Publish a motion/control command to vivarium/rack/{id}/command.
        Emergency stop (!) must use publish_emergency() — it goes to a different
        topic at QoS 2.
        """
        if command.strip() == "!":
            self.publish_emergency(rack_id)
            return
        topic = TOPIC_COMMAND.format(rack_id=rack_id)
        self._publish(topic, command, qos=qos)

    def publish_emergency(self, rack_id: str) -> None:
        """
        Publish ! to vivarium/rack/{id}/emergency at QoS 2 (exactly-once).
        Never queued, never lock-checked — always immediate (Section 4.3).
        """
        topic = TOPIC_EMERGENCY.format(rack_id=rack_id)
        self._publish(topic, "!", qos=QOS_EMERGENCY)
        logger.warning("EMERGENCY STOP published: rack=%s", rack_id)

    def _publish(self, topic: str, payload: Any, qos: int = 1) -> None:
        """Internal publish: JSON-encodes dicts/lists, passes strings as-is."""
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        info = self._client.publish(topic, str(payload), qos=qos)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish error: topic=%s rc=%d", topic, info.rc)
        else:
            logger.debug("MQTT TX: topic=%s payload=%r qos=%d", topic, payload, qos)

    # ── Handler registration ──────────────────────────────────────────────────

    def register_handler(self, subtopic: str, handler: MessageHandler) -> None:
        """
        Register a message handler for a specific MQTT subtopic.

        subtopic: the last path segment, e.g. "response", "image", "status".
                  Use "*" to receive all messages regardless of subtopic.

        handler signature:
            (rack_id: str | None, subtopic: str, payload: str | dict) → None

        Example:
            mqtt_client.register_handler("response", on_response_message)
            mqtt_client.register_handler("image",    on_image_message)
        """
        self._handlers.append((subtopic, handler))
        logger.debug("MQTT handler registered for subtopic=%r", subtopic)


# ── Module-level singleton ────────────────────────────────────────────────────

mqtt_client = VivMQTTClient()
