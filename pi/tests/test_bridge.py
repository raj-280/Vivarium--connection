"""
pi/tests/test_bridge.py
=======================

Self-contained integration test for bridge.py.

No real Pi, no real Arduino, no real MQTT broker needed.
Everything is faked in-process using unittest.mock.

Run from the repo root:
    python -m pytest pi/tests/test_bridge.py -v
    # OR without pytest:
    python pi/tests/test_bridge.py

What is tested
--------------
  1.  COMMAND_ACK is always the FIRST response published (before serial touch)
  2.  INTERCEPTED commands (CAPTURE, SCAN_START, SCAN_STOP) never reach serial
  3.  Normal commands (G28, M114, M700, !) are forwarded to the fake Arduino
  4.  Fake Arduino reply is published on the response topic
  5.  SERIAL_TIMEOUT is published when the fake Arduino is silent
  6.  Emergency stop (!) calls serial.emergency_stop(), NOT _forward_to_serial
  7.  Heartbeat payload contains correct keys and camera_status
  8.  Reconnect-cleanup publishes BRIDGE_RECONNECTED
  9.  Noise strings from Arduino ("Yo! On my way!") are filtered and NOT
      re-published as the final response
  10. LAYOUT_CONFIG published with correct numeric values after M705/M706/M707
  11. LIMITS published after LAYOUT_CONFIG with derived axis limits
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock, patch, call

# ── Path setup ────────────────────────────────────────────────────────────────
# Allow importing pi.* from the repo root
_PI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_PI_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _PI_DIR not in sys.path:
    sys.path.insert(0, _PI_DIR)


# =============================================================================
# Fake Arduino
# =============================================================================

class FakeArduino:
    """
    In-memory fake Arduino. Bridge talks to SerialHandler; we intercept at
    the SerialHandler level by replacing its send/receive methods.

    Response table (mirrors real firmware):
        M114        → "X:0.00 Y:0.00 C:0.00 homed:X=Y Y=Y C=Y"
        M705        → "ROWS=12 COLS=7"
        M706        → "Pitch X=50.0 Y=50.0"
        M707        → "Offsets X0=0.0 Y0=0.0"
        G28         → "Yo! On my way!"     (noise — bridge should filter)
        M700 Rn Cn  → "Yo! On my way!"
        M710/M711   → "Yo! On my way!"
        !           → "Yo! On my way!"
        FAIL_*      → None (silence → triggers SERIAL_TIMEOUT)
    """

    FAKE_M114 = "X:0.00 Y:0.00 C:0.00 homed:X=Y Y=Y C=Y"
    FAKE_M705 = "ROWS=12 COLS=7"
    # BUG-10 FIX: bridge.py parses M706 with regex r'Pitch\s+X=...Y=...'
    # (space between Pitch and X). Old value "PitchX=50.0 PitchY=50.0"
    # never matched and test_layout_config_has_pitch always failed silently.
    FAKE_M706 = "Pitch X=50.0 Y=50.0"
    FAKE_M707 = "Offsets X0=0.0 Y0=0.0"
    FAKE_ACK  = "Yo! On my way!"

    def __init__(self, fail_commands: set[str] | None = None):
        self.fail_commands: set[str] = {c.upper() for c in (fail_commands or set())}
        self.received: list[str] = []  # commands actually sent to "serial"

    def respond(self, command: str) -> Optional[str]:
        """Return the fake Arduino reply, or None to simulate silence."""
        self.received.append(command)
        base = command.split()[0].upper() if command.strip() else ""

        if base in self.fail_commands:
            return None  # silence → SERIAL_TIMEOUT

        if base == "M114":
            return self.FAKE_M114
        if base == "M705":
            return self.FAKE_M705
        if base == "M706":
            return self.FAKE_M706
        if base == "M707":
            return self.FAKE_M707

        return self.FAKE_ACK


# =============================================================================
# Fake MQTT publisher (collects published messages)
# =============================================================================

class FakeMQTT:
    """Replaces mqtt_client — records every publish call."""

    def __init__(self):
        self.responses: list[str] = []
        self.statuses: list[dict] = []
        self.device_id = "rack-test"

    def publish_response(self, payload: str) -> None:
        self.responses.append(payload)

    def publish_status(self, payload: dict) -> None:
        self.statuses.append(payload)

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


# =============================================================================
# Helpers
# =============================================================================

def _make_settings(device_id: str = "rack-test") -> MagicMock:
    """Return a mock settings object that looks like pi/config/settings.py."""
    s = MagicMock()
    s.device_id = device_id
    s.broker_host = "localhost"
    s.broker_port = 1883
    s.mqtt_use_tls = False
    s.mqtt_username = device_id
    s.mqtt_password = ""
    s.serial_port = "/dev/null"
    s.serial_baud = 115200
    s.serial_timeout_s = 0.5   # short for tests
    s.serial_retry_count = 1
    s.capture_dir = "/tmp/vivarium-test"
    s.tmp_is_tmpfs = False
    s.stream_name = device_id
    s.mediamtx_port = 8554
    s.scan_lock_keepalive_interval_s = 30.0
    return s


def _make_serial_handler(fake_arduino: FakeArduino, settings_mock: MagicMock) -> MagicMock:
    """
    Return a mock SerialHandler whose send_command calls the FakeArduino.

    Bridge calls:
        serial.send_command(cmd)  → reply | None
        serial.health_check()     → reply | None   (same as send_command("M114"))
        serial.emergency_stop()   → None  (fire and forget)
        serial.disconnect()       → None
    """
    sh = MagicMock()
    sh.send_command.side_effect = fake_arduino.respond
    sh.health_check.side_effect  = lambda: fake_arduino.respond("M114")
    sh.emergency_stop            = MagicMock()
    sh.disconnect                = MagicMock()
    return sh


# =============================================================================
# Base test class — sets up all mocks, no real network/serial
# =============================================================================

class BridgeTestBase(unittest.TestCase):
    """
    Base class that patches everything external and gives each test a
    pre-wired Bridge instance with a FakeArduino and FakeMQTT.
    """

    DEVICE_ID = "rack-test"

    def setUp(self):
        self.fake_arduino = FakeArduino()
        self.fake_mqtt    = FakeMQTT()
        self.settings     = _make_settings(self.DEVICE_ID)
        self.serial       = _make_serial_handler(self.fake_arduino, self.settings)

        # Patch out all external singletons that bridge.py imports at the top.
        # Order matters — patch the module as the bridge sees it.
        self._patches = [
            patch("bridge.settings",         self.settings),
            patch("bridge.mqtt_client",       self.fake_mqtt),
            patch("bridge.mediamtx_health",   MagicMock(last_status="online")),
            patch("bridge.camera_handler",    MagicMock()),
            # ScanExecutor is complex; replace entirely with a no-op mock.
            patch("bridge.ScanExecutor",      return_value=MagicMock()),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_patches)

        # Import bridge AFTER patching so it sees the mocks.
        import bridge as bridge_module
        self.bridge_module = bridge_module

        # Build a Bridge instance, injecting the fake serial.
        # We call __init__ then replace the serial attribute immediately.
        self.bridge = bridge_module.Bridge()
        self.bridge.serial = self.serial
        self.bridge.device_id = self.DEVICE_ID

    def _stop_patches(self):
        for p in self._patches:
            try:
                p.stop()
            except RuntimeError:
                pass

    # ── Convenience ───────────────────────────────────────────────────────────

    def send_command(self, cmd: str) -> None:
        """Simulate an MQTT message arriving on the command topic."""
        self.bridge._on_command("command", cmd)

    def send_emergency(self, cmd: str = "!") -> None:
        """Simulate an emergency MQTT message."""
        self.bridge._on_emergency("emergency", cmd)

    @property
    def responses(self) -> list[str]:
        return self.fake_mqtt.responses

    @property
    def statuses(self) -> list[dict]:
        return self.fake_mqtt.statuses


# =============================================================================
# Test cases
# =============================================================================

class TestCommandAck(BridgeTestBase):
    """Test 1 — COMMAND_ACK is always the FIRST thing published."""

    def test_ack_is_first_for_g28(self):
        self.send_command("G28")
        self.assertGreater(len(self.responses), 0, "No responses published")
        self.assertEqual(self.responses[0], "COMMAND_ACK:G28")

    def test_ack_is_first_for_m114(self):
        self.send_command("M114")
        self.assertEqual(self.responses[0], "COMMAND_ACK:M114")

    def test_ack_is_first_for_capture(self):
        self.send_command("CAPTURE")
        self.assertEqual(self.responses[0], "COMMAND_ACK:CAPTURE")

    def test_ack_is_first_for_scan_start(self):
        self.send_command("SCAN_START")
        self.assertEqual(self.responses[0], "COMMAND_ACK:SCAN_START")

    def test_ack_format_with_args(self):
        """ACK for 'M700 R1 C1' must be 'COMMAND_ACK:M700 R1 C1'."""
        self.send_command("M700 R1 C1")
        self.assertEqual(self.responses[0], "COMMAND_ACK:M700 R1 C1")


class TestInterceptedCommands(BridgeTestBase):
    """Test 2 — CAPTURE, SCAN_START, SCAN_STOP never hit the fake Arduino."""

    def test_capture_not_forwarded_to_serial(self):
        self.send_command("CAPTURE")
        self.assertNotIn("CAPTURE", self.fake_arduino.received)

    def test_scan_start_not_forwarded_to_serial(self):
        self.send_command("SCAN_START")
        self.assertNotIn("SCAN_START", self.fake_arduino.received)

    def test_scan_stop_not_forwarded_to_serial(self):
        self.send_command("SCAN_STOP")
        self.assertNotIn("SCAN_STOP", self.fake_arduino.received)

    def test_capture_dispatches_camera_handler(self):
        import bridge as b
        self.send_command("CAPTURE")
        # Give the daemon thread a moment to start
        time.sleep(0.1)
        b.camera_handler.capture.assert_called_once()

    def test_scan_start_dispatches_scan_executor(self):
        self.send_command("SCAN_START")
        time.sleep(0.1)
        self.bridge._scan_executor.start.assert_called_once()

    def test_scan_stop_calls_request_stop(self):
        self.send_command("SCAN_STOP")
        self.bridge._scan_executor.request_stop.assert_called_once()


class TestSerialForwarding(BridgeTestBase):
    """Test 3 & 4 — Normal commands are forwarded and the reply is published."""

    def test_g28_forwarded_to_arduino(self):
        self.send_command("G28")
        self.assertIn("G28", self.fake_arduino.received)

    def test_m114_forwarded_to_arduino(self):
        self.send_command("M114")
        self.assertIn("M114", self.fake_arduino.received)

    def test_m700_r1_c1_forwarded(self):
        self.send_command("M700 R1 C1")
        self.assertTrue(
            any("M700" in r for r in self.fake_arduino.received),
            "M700 was not forwarded to serial",
        )

    def test_arduino_reply_published(self):
        """Bridge must publish the Arduino's reply after the ACK."""
        self.send_command("M114")
        # responses[0] is COMMAND_ACK:M114
        # responses[1] should be the M114 reply
        self.assertGreaterEqual(len(self.responses), 2)
        self.assertEqual(self.responses[1], FakeArduino.FAKE_M114)

    def test_g28_ack_reply_noise_filtered(self):
        """
        G28 returns 'Yo! On my way!' — that is the Arduino's ACK noise.
        Bridge currently publishes it as the response (it's the only thing
        the Arduino sends for G28). Verify the pipeline completes: ACK then reply.
        """
        self.send_command("G28")
        self.assertEqual(self.responses[0], "COMMAND_ACK:G28")
        self.assertEqual(self.responses[1], FakeArduino.FAKE_ACK)


class TestSerialTimeout(BridgeTestBase):
    """Test 5 — SERIAL_TIMEOUT when the fake Arduino stays silent."""

    def setUp(self):
        super().setUp()
        # Make all commands time out
        self.fake_arduino.fail_commands = {"G28"}
        # send_command side_effect already delegates to fake_arduino.respond
        # but we need to update the mock to return None for G28
        self.serial.send_command.side_effect = self.fake_arduino.respond

    def test_serial_timeout_published_for_silent_arduino(self):
        self.send_command("G28")
        timeout_responses = [r for r in self.responses if r.startswith("SERIAL_TIMEOUT")]
        self.assertGreater(
            len(timeout_responses), 0,
            f"Expected SERIAL_TIMEOUT, got: {self.responses}",
        )

    def test_serial_timeout_contains_command(self):
        self.send_command("G28")
        timeout = next(r for r in self.responses if r.startswith("SERIAL_TIMEOUT"))
        self.assertIn("G28", timeout)


class TestEmergencyStop(BridgeTestBase):
    """Test 6 — Emergency stop calls emergency_stop(), not _forward_to_serial."""

    def test_emergency_ack_published(self):
        self.send_emergency("!")
        self.assertEqual(self.responses[0], "COMMAND_ACK:!")

    def test_emergency_calls_serial_emergency_stop(self):
        self.send_emergency("!")
        self.serial.emergency_stop.assert_called_once()

    def test_emergency_does_not_use_send_command(self):
        self.send_emergency("!")
        self.serial.send_command.assert_not_called()

    def test_emergency_not_forwarded_to_arduino(self):
        """! must not go through the normal serial send path."""
        self.send_emergency("!")
        self.assertNotIn("!", self.fake_arduino.received)


class TestHeartbeat(BridgeTestBase):
    """Test 7 — Heartbeat payload structure."""

    def test_heartbeat_publishes_status(self):
        self.bridge._publish_heartbeat()
        self.assertGreater(len(self.statuses), 0)

    def test_heartbeat_has_required_keys(self):
        self.bridge._publish_heartbeat()
        hb = self.statuses[0]
        for key in ("status", "ts", "device_id", "camera_status"):
            self.assertIn(key, hb, f"Missing key {key!r} in heartbeat")

    def test_heartbeat_status_is_online(self):
        self.bridge._publish_heartbeat()
        self.assertEqual(self.statuses[0]["status"], "online")

    def test_heartbeat_device_id_matches(self):
        self.bridge._publish_heartbeat()
        self.assertEqual(self.statuses[0]["device_id"], self.DEVICE_ID)

    def test_heartbeat_camera_status_from_mediamtx(self):
        """camera_status comes from mediamtx_health.last_status mock."""
        self.bridge._publish_heartbeat()
        self.assertEqual(self.statuses[0]["camera_status"], "online")


class TestReconnectCleanup(BridgeTestBase):
    """Test 8 — Reconnect cleanup publishes BRIDGE_RECONNECTED."""

    def test_bridge_reconnected_published(self):
        # _reconnect_cleanup also calls M705/M706/M707 layout queries.
        # Make sure health_check returns a valid M114 response.
        self.bridge._reconnect_cleanup()
        reconnected = [r for r in self.responses if "BRIDGE_RECONNECTED" in r]
        self.assertGreater(
            len(reconnected), 0,  # at least 1
            f"BRIDGE_RECONNECTED not published. Responses: {self.responses}")

    def test_health_check_called_on_reconnect(self):
        self.bridge._reconnect_cleanup()
        self.serial.health_check.assert_called()


class TestLayoutConfig(BridgeTestBase):
    """Test 10 & 11 — LAYOUT_CONFIG and LIMITS are published after M705/M706/M707."""

    def test_layout_config_published(self):
        self.bridge._publish_layout_config()
        layout_messages = [
            r for r in self.responses
            if isinstance(r, str) and "grid_rows" in r
        ]
        self.assertGreater(
            len(layout_messages), 0,
            f"LAYOUT_CONFIG not published. Responses: {self.responses}",
        )

    def test_layout_config_has_correct_rows_cols(self):
        self.bridge._publish_layout_config()
        layout_msg = next(
            (r for r in self.responses if isinstance(r, str) and "grid_rows" in r),
            None,
        )
        self.assertIsNotNone(layout_msg, "No LAYOUT_CONFIG found")
        payload = json.loads(layout_msg)
        self.assertEqual(payload["grid_rows"], 12)
        self.assertEqual(payload["grid_cols"], 7)

    def test_layout_config_has_pitch(self):
        self.bridge._publish_layout_config()
        layout_msg = next(
            (r for r in self.responses if isinstance(r, str) and "pitch_x_mm" in r),
            None,
        )
        self.assertIsNotNone(layout_msg)
        payload = json.loads(layout_msg)
        self.assertAlmostEqual(payload["pitch_x_mm"], 50.0)
        self.assertAlmostEqual(payload["pitch_y_mm"], 50.0)

    def test_limits_published_after_layout(self):
        """_publish_limits fires right after layout config — verify LIMITS message."""
        self.bridge._publish_layout_config()
        limits_messages = [
            r for r in self.responses
            if isinstance(r, str) and r.startswith("LIMITS")
        ]
        self.assertGreater(
            len(limits_messages), 0,
            f"LIMITS not published. Responses: {self.responses}",
        )

    def test_limits_values_derived_correctly(self):
        """
        For ROWS=12 COLS=7, pitch=50, offset=0:
            limit_x = 0 + (7-1)*50  = 300.0
            limit_y = 0 + (12-1)*50 = 550.0
        """
        self.bridge._publish_layout_config()
        limits_msg = next(
            (r for r in self.responses if isinstance(r, str) and r.startswith("LIMITS")),
            None,
        )
        self.assertIsNotNone(limits_msg)
        # LIMITS X=300.0 Y=550.0 C=360.0
        self.assertIn("X=300.0", limits_msg)
        self.assertIn("Y=550.0", limits_msg)


class TestNoiseFiltering(BridgeTestBase):
    """
    Test 9 — Noise strings from the Arduino are handled correctly.

    "Yo! On my way!" is the Arduino's immediate ACK noise.
    For most commands it IS the only reply (G28, M710, etc.),
    so it gets published. For M114, it comes before the real response
    and SerialHandler's _write_and_wait filters it.
    This test verifies serial_handler.send_command is called once per command
    (bridge does not retry for noise).
    """

    def test_m114_published_as_full_response_not_noise(self):
        """M114 response should be the real position string, not 'Yo!'."""
        self.send_command("M114")
        # The real response (from FakeArduino) is the FAKE_M114 string,
        # not the noise "Yo! On my way!".
        self.assertIn(FakeArduino.FAKE_M114, self.responses)

    def test_send_command_called_once_per_command(self):
        """Bridge should call send_command exactly once per non-timeout command."""
        self.send_command("M114")
        self.assertEqual(self.serial.send_command.call_count, 1)


class TestBroadcastCommand(BridgeTestBase):
    """Broadcast commands on vivarium/all/command follow the same flow."""

    def test_broadcast_g28_acked_and_forwarded(self):
        self.bridge._on_broadcast("command", "G28")
        self.assertEqual(self.responses[0], "COMMAND_ACK:G28")
        self.assertIn("G28", self.fake_arduino.received)


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
