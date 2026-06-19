"""
pi/bridge.py

Main loop for the Pi bridge — Section 5.2.

Responsibilities (Stages 7 + 9 scope — Section 13.7 / 13.9):
─────────────────────────────────────────────────────
• On any message received on vivarium/rack/{id}/command, immediately publish
  COMMAND_ACK:{command} on vivarium/rack/{id}/response — BEFORE forwarding to
  the Arduino. This is the signal the server uses to distinguish "Pi never
  got it" from "Pi got it but Arduino didn't respond" (Section 4.5).
• CAPTURE is intercepted here and dispatched to camera_handler.capture() on
  a daemon thread (Section 5.2 / 5.4 / Stage 9). SCAN_START and SCAN_STOP
  are still logged as "not yet implemented" — scan_executor.py lands in Stage 12.
• Everything else (G28, M700, M701-704, M710, M711, M114, emergency !) is
  forwarded to the Arduino via serial_handler, and the Arduino's response
  (or None on SERIAL_TIMEOUT) is published back on the response topic.
• Heartbeat published to vivarium/rack/{id}/status every 30 seconds.
• Last Will is registered by mqtt_client at connect time (Section 5.2 / 9).
• Reconnect cleanup sequence, run once at startup and again on every MQTT
  reconnect:
    1. Arduino health check (serial_handler.health_check() — sends M114)
    2. Homed-flag verify (parse the M114 response for homed flags; log only
       in Stage 7 — position_monitor equivalent on the Pi side is informational)
    3. /tmp sweep — delete any leftover rack-*-*.jpg files
    4. Publish BRIDGE_RECONNECTED on the response topic
    5. Send M705/M706/M707/M799, parse all four responses, publish LAYOUT_CONFIG
       JSON — the server updates gantry_state + DB from this one clean payload.

Run:
    python -m pi.bridge
or:
    python pi/bridge.py     (with pi/ on PYTHONPATH, see systemd units)
"""
 
from __future__ import annotations

import glob
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
 
# Allow running as either `python pi/bridge.py` (cwd = repo root, pi/ added
# to sys.path below) or `python -m pi.bridge` (proper package import).
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.settings import settings
    from services.mqtt_client import mqtt_client
    from services.serial_handler import SerialHandler
    from services.camera_handler import camera_handler       # Stage 9
    from services.go2rtc_health import go2rtc_health         # Stage 10
    from services.scan_executor import ScanExecutor          # Stage 11
else:
    from .config.settings import settings
    from .services.mqtt_client import mqtt_client
    from .services.serial_handler import SerialHandler
    from .services.camera_handler import camera_handler      # Stage 9
    from .services.go2rtc_health import go2rtc_health        # Stage 10
    from .services.scan_executor import ScanExecutor         # Stage 11
 
 
logging.basicConfig(
    level=os.environ.get("GANTRY_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bridge")
 
 
# ── Constants ─────────────────────────────────────────────────────────────
 
HEARTBEAT_INTERVAL_S = 30.0
 
# Commands intercepted by the bridge — never forwarded to the Arduino.
# Section 5.2 / 5.4 / 5.5 / Section 6 ("CAPTURE never reaches the Arduino").
INTERCEPTED_COMMANDS = {"CAPTURE", "SCAN_START", "SCAN_STOP"}
 
 
# ── Bridge ────────────────────────────────────────────────────────────────
 
class Bridge:
    """
    Coordinates the MQTT <-> serial link for one rack.
 
    Stage 7 scope: command ACK/forward/response flow, heartbeat, and the
    reconnect cleanup sequence. CAPTURE / SCAN_START / SCAN_STOP are
    intercepted and logged only — their real implementations land in
    Stages 10 and 12.
    """
 
    def __init__(self) -> None:
        self.device_id = settings.device_id
        self.serial = SerialHandler()
        self._stop_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
 
        # Tracks whether _on_connect has fired before, so the reconnect
        # cleanup sequence runs on every (re)connect, including the first.
        self._first_connect_done = False
 
        # Start go2rtc health polling in the background so heartbeats can
        # read camera_status cheaply from the cache (Section 5.6 / Stage 10).
        go2rtc_health.start_background_polling()
 
        # Scan executor — shares this bridge's serial port (Section 5.5).
        # One instance per Bridge; scan lock enforces a single active scan.
        self._scan_executor = ScanExecutor(self.serial)
 
    # ── Lifecycle ─────────────────────────────────────────────────────────
 
    def start(self) -> None:
        logger.info("Starting bridge for device_id=%s", self.device_id)
        logger.info("Settings: %r", settings)
 
        # Open the serial link to the Arduino (or, for Stage 7 testing, to
        # the socat virtual-port echo script).
        self.serial.connect()
 
        # Register MQTT handlers before connecting so nothing is missed.
        mqtt_client.register_handler("command", self._on_command)
        mqtt_client.register_handler("emergency", self._on_emergency)
        mqtt_client.register_handler("broadcast", self._on_broadcast)
 
        # Hook into paho's on_connect via a wrapper so we can run the
        # reconnect-cleanup sequence on every (re)connect, not just the first.
        self._wrap_on_connect()
 
        mqtt_client.connect()
 
        # Start heartbeat loop
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
 
        logger.info("Bridge started. Waiting for commands…")
 
    def stop(self) -> None:
        logger.info("Stopping bridge…")
        self._stop_event.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2.0)
        mqtt_client.disconnect()
        self.serial.disconnect()
        logger.info("Bridge stopped.")
 
    def run_forever(self) -> None:
        """Block until SIGINT/SIGTERM (Ctrl+C or systemd stop)."""
 
        def _handle_signal(signum, frame):
            logger.info("Received signal %s — shutting down.", signum)
            self._stop_event.set()
 
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
 
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        finally:
            self.stop()
 
    # ── Reconnect cleanup sequence (Section 5.2) ─────────────────────────
 
    def _wrap_on_connect(self) -> None:
        """
        Wrap mqtt_client's internal _on_connect so the reconnect-cleanup
        sequence runs every time the connection (re)establishes — including
        the very first connect.
        """
        original_on_connect = mqtt_client._on_connect
 
        def wrapped(client, userdata, flags, rc):
            original_on_connect(client, userdata, flags, rc)
            if rc == 0:
                # Run cleanup on a separate thread so we don't block paho's
                # network loop while doing serial I/O.
                threading.Thread(
                    target=self._reconnect_cleanup, name="reconnect-cleanup", daemon=True
                ).start()
 
        mqtt_client._client.on_connect = wrapped
 
    def _reconnect_cleanup(self) -> None:
        """
        Section 5.2 reconnect cleanup, run on every connect/reconnect:
          1. Arduino health check
          2. Homed-flag verify
          3. /tmp sweep (delete leftover rack-*-*.jpg)
          4. Publish BRIDGE_RECONNECTED
          5. Query M705/M706/M707/M799, publish LAYOUT_CONFIG JSON
        """
        which = "initial connect" if not self._first_connect_done else "reconnect"
        logger.info("Running reconnect-cleanup sequence (%s)…", which)
        self._first_connect_done = True
 
        # 1. Arduino health check — health_check() now returns the raw M114
        # response string (or None on timeout) so we reuse it for the
        # homed-flag verify step below WITHOUT a second serial round-trip.
        # Sending M114 twice in rapid succession caused the second “Yo! On my
        # way!” echo to contaminate the response window of the first layout
        # query (M705), producing spurious parse-warnings in the logs.
        m114_response: Optional[str] = None
        healthy = False
        try:
            m114_response = self.serial.health_check()   # raw M114 str or None
            healthy = m114_response is not None
        except Exception:
            logger.exception("Arduino health check failed during reconnect cleanup.")
 
        if healthy:
            logger.info("Arduino health check: OK")
        else:
            logger.warning("Arduino health check: NO RESPONSE")
 
        # 2. Homed-flag verify
        homed = self._parse_homed_flags(m114_response)
        if homed is not None:
            logger.info(
                "Homed-flag verify: X=%s Y=%s C=%s",
                homed.get("homed_x"), homed.get("homed_y"), homed.get("homed_c"),
            )
        else:
            logger.info("Homed-flag verify: no M114 response to parse (skipped).")
 
        # 3. /tmp sweep — delete leftover rack-*-*.jpg
        swept = self._sweep_tmp()
        if swept:
            logger.info("/tmp sweep: removed %d leftover capture file(s): %s", len(swept), swept)
        else:
            logger.info("/tmp sweep: nothing to clean up.")
 
        # 4. Publish BRIDGE_RECONNECTED
        mqtt_client.publish_response(f"BRIDGE_RECONNECTED:{which}")
        logger.info("Reconnect-cleanup sequence complete — published BRIDGE_RECONNECTED.")
 
        # 5. Query layout + limits, publish LAYOUT_CONFIG.
        # A brief pause gives the Arduino’s serial TX buffer time to flush any
        # remaining bytes from the M114 round-trip above before we open the
        # M705/M706/M707 response windows.  This prevents the M114 real-response
        # line (arriving slightly late) from being swallowed by M705’s waiter
        # before _waiting_for_response is set, and prevents the M705 “Yo!” echo
        # from arriving AFTER the window opens and being routed as unsolicited.
        time.sleep(0.3)
        self._publish_layout_config()
 
    def _publish_layout_config(self) -> None:
       try:
           rows_line   = self.serial.send_command("M705")
           pitch_line  = self.serial.send_command("M706")
           offset_line = self.serial.send_command("M707")
           # M799 removed — not implemented in firmware
       except Exception:
           logger.exception("_publish_layout_config: serial error — skipping LAYOUT_CONFIG.")
           return
   
       payload: dict = {"type": "LAYOUT_CONFIG"}
       parsed_any = False
   
       # Check for E-stop state — Arduino replies "E-stop" to all commands when stopped
       ESTOP_STR = "e-stop"
       for name, val in [("M705", rows_line), ("M706", pitch_line), ("M707", offset_line)]:
           if val and val.strip().lower() == ESTOP_STR:
               logger.warning(
                   "_publish_layout_config: Arduino is in E-stop state "
                   "— LAYOUT_CONFIG not published. Send M17 to re-enable."
               )
               return
   
       # Parse M705: ROWS=12 COLS=7
       if rows_line:
           m = re.search(r'ROWS=(\d+)\s+COLS=(\d+)', rows_line, re.IGNORECASE)
           if m:
               payload["grid_rows"] = int(m.group(1))
               payload["grid_cols"] = int(m.group(2))
               parsed_any = True
           else:
               logger.warning("_publish_layout_config: could not parse M705 response: %r", rows_line)
   
       # Parse M706: Pitch X=50.0 Y=50.0
       if pitch_line:
           m = re.search(r'Pitch\s+X=([-\d.]+)\s+Y=([-\d.]+)', pitch_line, re.IGNORECASE)
           if m:
               payload["pitch_x_mm"] = float(m.group(1))
               payload["pitch_y_mm"] = float(m.group(2))
               parsed_any = True
           else:
               logger.warning("_publish_layout_config: could not parse M706 response: %r", pitch_line)
   
       # Parse M707: Offsets X0=0.0 Y0=0.0
       if offset_line:
           m = re.search(r'Offsets\s+X0=([-\d.]+)\s+Y0=([-\d.]+)', offset_line, re.IGNORECASE)
           if m:
               payload["x0_offset_mm"] = float(m.group(1))
               payload["y0_offset_mm"] = float(m.group(2))
               parsed_any = True
           else:
               logger.warning("_publish_layout_config: could not parse M707 response: %r", offset_line)
   
       if parsed_any:
           mqtt_client.publish_response(json.dumps(payload))
           logger.info(
               "LAYOUT_CONFIG published: rows=%s cols=%s pitch=(%s,%s) offset=(%s,%s)",
               payload.get("grid_rows"), payload.get("grid_cols"),
               payload.get("pitch_x_mm"), payload.get("pitch_y_mm"),
               payload.get("x0_offset_mm"), payload.get("y0_offset_mm"),
           )
       else:
           logger.warning("_publish_layout_config: no valid responses from Arduino — LAYOUT_CONFIG not published.")
    @staticmethod
    def _parse_homed_flags(m114_response: Optional[str]) -> Optional[dict[str, bool]]:
        """
        Parse a raw M114 response line for homed flags, e.g.:
            "X:0.00 Y:0.00 C:0.00 homed:X=Y Y=Y C=N"
        Returns {"homed_x": bool, "homed_y": bool, "homed_c": bool} or None
        if the response doesn't contain a recognisable "homed:" section.
 
        This is a best-effort informational parse for Stage 7 logging only —
        the authoritative homed-flag check lives in
        server/services/position_monitor.py (Section 4.4).
        """
        if not m114_response:
            return None
 
        marker = "homed:"
        idx = m114_response.lower().find(marker)
        if idx == -1:
            return None
 
        segment = m114_response[idx + len(marker):].strip()
        flags = {"homed_x": False, "homed_y": False, "homed_c": False}
        for token in segment.split():
            if "=" not in token:
                continue
            axis, value = token.split("=", 1)
            axis = axis.strip().upper()
            is_homed = value.strip().upper() == "Y"
            if axis == "X":
                flags["homed_x"] = is_homed
            elif axis == "Y":
                flags["homed_y"] = is_homed
            elif axis == "C":
                flags["homed_c"] = is_homed
 
        return flags
 
    @staticmethod
    def _sweep_tmp() -> list[str]:
        """
        Delete any leftover rack-*-*.jpg files in /tmp (Section 5.2 / 5.4).
 
        On the real Pi, /tmp is tmpfs (RAM only, tmp_is_tmpfs=true in
        device.conf) and capture files are named rack-{id}-{timestamp}.jpg
        per Section 5.4. This sweep catches files left behind by a crash
        mid-capture.
        """
        pattern = os.path.join("/tmp", "rack-*-*.jpg")
        matches = glob.glob(pattern)
        removed: list[str] = []
        for path in matches:
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                logger.exception("Failed to remove leftover capture file: %s", path)
        return removed
 
    # ── Heartbeat (Section 5.2 / 11) ─────────────────────────────────────
 
    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._publish_heartbeat()
            except Exception:
                logger.exception("Heartbeat publish failed.")
            # Sleep in small increments so stop() is responsive.
            for _ in range(int(HEARTBEAT_INTERVAL_S * 2)):
                if self._stop_event.is_set():
                    return
                time.sleep(0.5)
 
    def _publish_heartbeat(self) -> None:
        # Read camera_status from the cached result of the go2rtc health check
        # (Section 5.6). The health thread updates this every 30s in the
        # background — calling last_status here is non-blocking.
        camera_status = go2rtc_health.last_status
 
        payload = {
            "status": "online",
            "ts": datetime.now(timezone.utc).isoformat(),
            "device_id": self.device_id,
            "camera_status": camera_status,
        }
        mqtt_client.publish_status(payload)
        logger.debug("Heartbeat published: %s", payload)
 
    # ── Command handling (Section 5.2) ───────────────────────────────────
 
    def _on_command(self, subtopic: str, payload: Any) -> None:
        """
        Handle vivarium/rack/{id}/command messages.
 
        Order of operations is critical (Section 5.2 / 4.5):
          1. Publish COMMAND_ACK:{command} immediately — BEFORE touching
             serial. This is the "Pi got it" signal.
          2. If the command is CAPTURE / SCAN_START / SCAN_STOP — intercept,
             do NOT forward to serial (Stage 7: log "not yet implemented").
          3. Otherwise — forward to the Arduino via serial_handler and
             publish whatever comes back (or SERIAL_TIMEOUT if nothing does).
        """
        command = self._extract_command(payload)
        if command is None:
            logger.warning("Received command message with no usable command: %r", payload)
            return
 
        logger.info("Command received: %r", command)
 
        # Step 1 — ACK immediately, before forwarding to serial (Section 5.2).
        mqtt_client.publish_response(f"COMMAND_ACK:{command}")
 
        # Step 2 — intercepted commands never reach the Arduino.
        base_command = command.split()[0].upper() if command.strip() else ""
        if base_command in INTERCEPTED_COMMANDS:
            if base_command == "CAPTURE":
                # Dispatch to camera_handler on a separate thread so the
                # MQTT message loop is not blocked during the photo + file
                # copy sequence (Section 5.2 / 5.4).
                threading.Thread(
                    target=camera_handler.capture,
                    name="capture",
                    daemon=True,
                ).start()
                logger.info("CAPTURE dispatched to camera_handler thread.")
 
            elif base_command == "SCAN_START":
                # Parse optional payload fields for resume/session tracking.
                payload_dict: dict = {}
                if isinstance(payload, dict):
                    payload_dict = payload
                elif isinstance(payload, str):
                    import json as _json
                    try:
                        payload_dict = _json.loads(payload)
                    except Exception:
                        pass
                payload_dict["rack_id"] = self.device_id
 
                # Dispatch scan in a daemon thread — must share self.serial.
                threading.Thread(
                    target=self._scan_executor.start,
                    args=(payload_dict,),
                    name="scan-executor",
                    daemon=True,
                ).start()
                logger.info("SCAN_START dispatched to scan_executor thread.")
 
            elif base_command == "SCAN_STOP":
                # Graceful stop between cells (Section 5.5 / 4.8).
                self._scan_executor.request_stop()
                logger.info("SCAN_STOP forwarded to scan_executor.")
 
            return
 
        # Step 3 — forward everything else to the Arduino.
        self._forward_to_serial(command)
 
    def _on_emergency(self, subtopic: str, payload: Any) -> None:
        """
        Handle vivarium/rack/{id}/emergency (QoS 2, "!" only).
 
        Emergency stop is fire-and-forget (Item 6) — we call
        serial.emergency_stop() which flushes buffers and writes !\n without
        blocking on a readline(). This ensures E-stop is never delayed by an
        in-progress long-running command (e.g. M700 rack move).
        """
        command = self._extract_command(payload) or "!"
        logger.warning("EMERGENCY command received: %r", command)
        mqtt_client.publish_response(f"COMMAND_ACK:{command}")
        # Fire-and-forget — do NOT call _forward_to_serial which blocks on readline
        self.serial.emergency_stop()
 
    def _on_broadcast(self, subtopic: str, payload: Any) -> None:
        """Handle vivarium/all/command — same flow as a normal command."""
        logger.info("Broadcast command received: %r", payload)
        self._on_command(subtopic, payload)
 
    def _forward_to_serial(self, command: str) -> None:
        """
        Forward `command` to the Arduino and publish its response (or
        SERIAL_TIMEOUT:{command} if both attempts in serial_handler time out).
 
        On SerialException (USB disconnect), triggers the reconnect loop in a
        daemon thread (Item 2) and publishes SERIAL_TIMEOUT so the server can
        escalate appropriately.
        """
        import serial as _serial
        try:
            response = self.serial.send_command(command)
        except _serial.SerialException as exc:
            logger.error(
                "Serial port disconnected while forwarding %r: %s — starting reconnect loop.",
                command, exc,
            )
            mqtt_client.publish_response(f"SERIAL_TIMEOUT:{command}")
            threading.Thread(
                target=self.serial.reconnect_loop,
                name="serial-reconnect",
                daemon=True,
            ).start()
            return
        except Exception:
            logger.exception("Serial error while forwarding command %r", command)
            mqtt_client.publish_response(f"SERIAL_TIMEOUT:{command}")
            return
 
        if response is None:
            mqtt_client.publish_response(f"SERIAL_TIMEOUT:{command}")
        else:
            mqtt_client.publish_response(response)
 
    @staticmethod
    def _extract_command(payload: Any) -> Optional[str]:
        """
        Accepts either a plain string command (e.g. "G28") or a JSON object
        with a "command" field (e.g. {"command": "M700 R2 C3"}), matching
        how server/services/mqtt_client.py publishes commands.
        """
        if isinstance(payload, str):
            return payload.strip() or None
        if isinstance(payload, dict):
            cmd = payload.get("command")
            if isinstance(cmd, str):
                return cmd.strip() or None
        return None
 
 
# ── Entry point ───────────────────────────────────────────────────────────
 
def main() -> None:
    bridge = Bridge()
    bridge.start()
    bridge.run_forever()
 
 
if __name__ == "__main__":
    main()