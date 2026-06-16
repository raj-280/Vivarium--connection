"""
pi/services/mqtt_client.py

paho-mqtt wrapper for the Pi's MQTT connection (Section 2.2 / Section 11).

Topic / QoS mapping mirrors server/services/mqtt_client.py exactly — see
Section 11 of the implementation plan:

    vivarium/rack/{id}/command        Server → Pi   QoS 1
    vivarium/rack/{id}/response       Pi → Server   QoS 1
    vivarium/rack/{id}/status         Pi → Server   QoS 0  (heartbeat + Last Will)
    vivarium/rack/{id}/emergency      Server → Pi   QoS 2
    vivarium/rack/{id}/image          Pi → Server   QoS 1
    vivarium/rack/{id}/scan_progress  Pi → Server   QoS 0
    vivarium/rack/{id}/scan_status    Pi → Server   QoS 1
    vivarium/all/command              Server → All  QoS 1

Last Will (Section 5.2 / 9 Layer 2B):
    Registered at connect time on vivarium/rack/{id}/status, QoS 1, retained.
    Payload: {"status": "offline", "reason": "unexpected_disconnect"}
    The broker auto-publishes this within ~30s of an unexpected disconnect.

This Pi connects with cleansession=false (persistent session) so commands
published while this Pi is offline are queued by the broker, per Section 9
Layer 2B ("persistent sessions queue commands while a Pi is offline").

[PROD ONLY] TLS is config-driven via mqtt_use_tls / ca_cert_path in
device.conf and is inert (unused) locally — see Section 2.2 / 9.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

try:
    from config.settings import settings          # python pi/bridge.py  (pi/ on sys.path)
except ImportError:
    from pi.config.settings import settings       # python -m pi.bridge  (repo root on sys.path)

logger = logging.getLogger(__name__)


# ── Topic templates (Section 11) ──────────────────────────────────────────────

TOPIC_COMMAND       = "vivarium/rack/{rack_id}/command"
TOPIC_RESPONSE      = "vivarium/rack/{rack_id}/response"
TOPIC_STATUS        = "vivarium/rack/{rack_id}/status"
TOPIC_EMERGENCY     = "vivarium/rack/{rack_id}/emergency"
TOPIC_IMAGE         = "vivarium/rack/{rack_id}/image"
TOPIC_SCAN_PROGRESS = "vivarium/rack/{rack_id}/scan_progress"
TOPIC_SCAN_STATUS   = "vivarium/rack/{rack_id}/scan_status"
TOPIC_BROADCAST     = "vivarium/all/command"

# QoS constants (Section 11) — must match server/services/mqtt_client.py
QOS_COMMAND       = 1
QOS_RESPONSE      = 1
QOS_STATUS        = 0
QOS_EMERGENCY     = 2
QOS_IMAGE         = 1
QOS_SCAN_PROGRESS = 0
QOS_SCAN_STATUS   = 1

# Topics this Pi subscribes to: its own command/emergency topics + the
# all-racks broadcast topic.
SUBSCRIBE_TOPICS: list[tuple[str, int]] = []  # populated in __init__ (needs device_id)


# ── Last Will payload (Section 5.2 / 9 Layer 2B) ──────────────────────────────

LAST_WILL_PAYLOAD = json.dumps({"status": "offline", "reason": "unexpected_disconnect"})


# ── Handler type ──────────────────────────────────────────────────────────────

# (subtopic: str, payload: str | dict | bytes) -> None
MessageHandler = Callable[[str, Any], None]


class PiMQTTClient:
    """
    Pi-side paho-mqtt wrapper.

    connect()       — connect to broker, register Last Will, subscribe to
                       this rack's command/emergency topics + broadcast.
    disconnect()    — graceful shutdown.
    publish_*()     — publish to the correct topic at the correct QoS.
    register_handler() — subscribe a callback to a specific subtopic.
    is_connected    — bool property.
    """

    def __init__(self, device_id: Optional[str] = None) -> None:
        self.device_id = device_id or settings.device_id

        self._client = mqtt.Client(
            client_id=f"vivarium-pi-{self.device_id}",
            clean_session=False,  # persistent session — Section 9 Layer 2B
            protocol=mqtt.MQTTv311,
        )
        self._connected = threading.Event()
        self._handlers: list[tuple[str, MessageHandler]] = []

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        # Last Will — registered before connect() so it's active for the
        # entire connection lifetime (Section 5.2 / 9 Layer 2B).
        status_topic = TOPIC_STATUS.format(rack_id=self.device_id)
        self._client.will_set(
            status_topic,
            payload=LAST_WILL_PAYLOAD,
            qos=QOS_RESPONSE,  # QoS 1, retained, per Section 9 Layer 2B
            retain=True,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def connect(self, timeout_s: float = 5.0) -> None:
        """
        Connect to the Mosquitto broker using broker_host/broker_port and
        mqtt_password from device.conf, start paho's background network
        loop, and block until connected (or timeout_s elapses).
        """
        if settings.mqtt_password:
            # device_id doubles as the MQTT username (per-Pi identity,
            # Section 9 Layer 2B ACL pattern: vivarium/rack/{id}/*)
            self._client.username_pw_set(self.device_id, settings.mqtt_password)

        # [PROD ONLY] TLS — config-driven, inert locally.
        if settings.mqtt_use_tls and settings.ca_cert_path:
            self._client.tls_set(ca_certs=settings.ca_cert_path)
            logger.info("MQTT TLS enabled (CA: %s)", settings.ca_cert_path)

        logger.info(
            "Connecting to MQTT broker %s:%d as %r …",
            settings.broker_host, settings.broker_port, self._client._client_id,
        )
        self._client.connect(
            settings.broker_host,
            settings.broker_port,
            keepalive=60,
        )
        self._client.loop_start()

        if not self._connected.wait(timeout=timeout_s):
            self._client.loop_stop()
            raise RuntimeError(
                f"MQTT connection to {settings.broker_host}:{settings.broker_port} "
                f"timed out after {timeout_s:.0f}s."
            )
        logger.info("MQTT connected (device_id=%s).", self.device_id)

    def disconnect(self) -> None:
        """
        Graceful shutdown. Note: a clean disconnect does NOT trigger the
        Last Will (that's the point of Last Will — it only fires on
        *unexpected* disconnects).
        """
        self._client.loop_stop()
        self._client.disconnect()
        self._connected.clear()
        logger.info("MQTT disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── paho callbacks ────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected.set()
            command_topic   = TOPIC_COMMAND.format(rack_id=self.device_id)
            emergency_topic = TOPIC_EMERGENCY.format(rack_id=self.device_id)
            client.subscribe(command_topic, qos=QOS_COMMAND)
            client.subscribe(emergency_topic, qos=QOS_EMERGENCY)
            client.subscribe(TOPIC_BROADCAST, qos=QOS_COMMAND)
            logger.info(
                "Subscribed to %s (qos=%d), %s (qos=%d), %s (qos=%d)",
                command_topic, QOS_COMMAND,
                emergency_topic, QOS_EMERGENCY,
                TOPIC_BROADCAST, QOS_COMMAND,
            )
        else:
            logger.error(
                "MQTT connection refused (rc=%d). Check broker address and credentials.", rc
            )

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected.clear()
        if rc != 0:
            logger.warning(
                "MQTT unexpected disconnect (rc=%d) — broker will publish Last Will "
                "on %s within ~30s.",
                rc, TOPIC_STATUS.format(rack_id=self.device_id),
            )
        else:
            logger.info("MQTT disconnected cleanly.")

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic

        # Determine subtopic: "command", "emergency", or "broadcast"
        if topic == TOPIC_BROADCAST:
            subtopic = "broadcast"
        else:
            parts = topic.split("/")
            subtopic = parts[3] if len(parts) >= 4 else topic

        try:
            payload_str = msg.payload.decode("utf-8")
            try:
                payload: Any = json.loads(payload_str)
            except json.JSONDecodeError:
                payload = payload_str
        except Exception:
            payload = msg.payload

        logger.debug("MQTT RX: topic=%s payload=%r", topic, payload)

        for pattern, handler in self._handlers:
            if pattern == "*" or pattern == subtopic:
                try:
                    handler(subtopic, payload)
                except Exception:
                    logger.exception(
                        "MQTT handler error: pattern=%r subtopic=%s", pattern, subtopic
                    )

    # ── Publish API (Section 11 QoS mapping) ─────────────────────────────────

    def publish_response(self, payload: Any) -> None:
        """vivarium/rack/{id}/response — QoS 1. ACKs, M114, SERIAL_TIMEOUT, etc."""
        topic = TOPIC_RESPONSE.format(rack_id=self.device_id)
        self._publish(topic, payload, qos=QOS_RESPONSE)

    def publish_status(self, payload: Any, retain: bool = False) -> None:
        """vivarium/rack/{id}/status — QoS 0. Heartbeat every 30s (Section 5.2)."""
        topic = TOPIC_STATUS.format(rack_id=self.device_id)
        self._publish(topic, payload, qos=QOS_STATUS, retain=retain)

    def publish_image(self, payload: Any) -> None:
        """vivarium/rack/{id}/image — QoS 1."""
        topic = TOPIC_IMAGE.format(rack_id=self.device_id)
        self._publish(topic, payload, qos=QOS_IMAGE)

    def publish_scan_progress(self, payload: Any) -> None:
        """vivarium/rack/{id}/scan_progress — QoS 0."""
        topic = TOPIC_SCAN_PROGRESS.format(rack_id=self.device_id)
        self._publish(topic, payload, qos=QOS_SCAN_PROGRESS)

    def publish_scan_status(self, payload: Any) -> None:
        """vivarium/rack/{id}/scan_status — QoS 1."""
        topic = TOPIC_SCAN_STATUS.format(rack_id=self.device_id)
        self._publish(topic, payload, qos=QOS_SCAN_STATUS)

    def _publish(self, topic: str, payload: Any, qos: int = 1, retain: bool = False) -> None:
        """Internal publish: JSON-encodes dicts/lists, passes strings as-is."""
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        info = self._client.publish(topic, str(payload), qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish error: topic=%s rc=%d", topic, info.rc)
        else:
            logger.debug("MQTT TX: topic=%s payload=%r qos=%d retain=%s", topic, payload, qos, retain)

    # ── Handler registration ──────────────────────────────────────────────────

    def register_handler(self, subtopic: str, handler: MessageHandler) -> None:
        """
        Register a message handler for a specific subtopic.

        subtopic: "command", "emergency", "broadcast", or "*" for all.

        handler signature:
            (subtopic: str, payload: str | dict) → None
        """
        self._handlers.append((subtopic, handler))
        logger.debug("MQTT handler registered for subtopic=%r", subtopic)


# ── Module-level singleton ────────────────────────────────────────────────────
# Other modules: `from services.mqtt_client import mqtt_client`
mqtt_client = PiMQTTClient()
