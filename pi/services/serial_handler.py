"""
pi/services/serial_handler.py

pyserial wrapper for the Arduino link (Section 2.2 / 5.2).

Responsibilities
────────────────
• detect_port()      — glob /dev/ttyACM* and /dev/ttyUSB* to find the Arduino.
• connect()          — open the serial port (using detect_port if port == 'auto').
• reconnect_loop()   — keep retrying connect() until it succeeds; called by
                       bridge.py when a SerialException is caught.
• _reader_loop()     — background thread that continuously reads lines from the
                       serial port and routes them: either to the pending
                       send_command() response Event, or to the unsolicited
                       callback (for spontaneous Arduino messages).
• send_command()     — write a command line, signal the reader thread, wait on
                       a threading.Event for the response. This decouples I/O
                       so the MQTT message loop is NEVER blocked during long
                       Arduino operations (M700 rack moves can take minutes).
• emergency_stop()   — fire-and-forget: flush buffers, write !\n, set stop
                       flag, return immediately without calling readline().
• health_check()     — send M114, return True if a response arrives within
                       the normal timeout window.
• disconnect()       — stop the reader thread, close the port.

Thread safety: a threading.Lock (self._write_lock) guards every serial write.
The reader thread holds the lock only during the actual read, not while
sleeping on the Event — this means send_command() and emergency_stop() can
always write immediately without contending with a blocking readline().

Lines are newline-terminated, matching the Arduino firmware's serial protocol.
"""

from __future__ import annotations

import glob
import logging
import threading
import time
from typing import Callable, Optional

import serial  # pyserial

try:
    from config.settings import settings          # python pi/bridge.py  (pi/ on sys.path)
except ImportError:
    from pi.config.settings import settings       # python -m pi.bridge  (repo root on sys.path)

logger = logging.getLogger(__name__)

# Ports to scan when port == 'auto'
_AUTO_PORT_GLOBS = ["/dev/ttyACM*", "/dev/ttyUSB*"]

# Interval between reconnect attempts (seconds)
_RECONNECT_RETRY_INTERVAL_S = 3.0


class SerialHandler:
    """
    Thread-safe pyserial wrapper with a background reader loop.

    Usage:
        handler = SerialHandler()
        handler.connect()
        response = handler.send_command("M114")
        # response is None if the timeout/retry window expired.
        handler.emergency_stop()   # fire-and-forget
        handler.disconnect()
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baud: Optional[int] = None,
        timeout_s: Optional[float] = None,
        retry_count: Optional[int] = None,
        on_unsolicited: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.port = port or settings.serial_port
        self.baud = baud or settings.serial_baud
        self.timeout_s = timeout_s if timeout_s is not None else settings.serial_timeout_s
        self.retry_count = retry_count if retry_count is not None else settings.serial_retry_count

        # Callback for lines that arrive when no send_command() is waiting
        self.on_unsolicited: Optional[Callable[[str], None]] = on_unsolicited

        self._serial: Optional[serial.Serial] = None

        # Background reader thread
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_reader = threading.Event()

        # Synchronisation for send_command() ↔ _reader_loop()
        self._write_lock = threading.Lock()
        self._response_event = threading.Event()
        self._pending_response: Optional[str] = None
        self._waiting_for_response = False

        # Emergency-stop flag — set by emergency_stop(), cleared on next connect
        self._estop = threading.Event()

    # ── Port detection ─────────────────────────────────────────────────────

    def detect_port(self) -> Optional[str]:
        """
        Scan /dev/ttyACM* and /dev/ttyUSB* and return the first one found.
        Returns None if no port is found (Arduino not plugged in).
        """
        for pattern in _AUTO_PORT_GLOBS:
            matches = sorted(glob.glob(pattern))
            if matches:
                logger.info("detect_port: found %s → using %s", matches, matches[0])
                return matches[0]
        logger.debug("detect_port: no serial port found")
        return None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def connect(self) -> None:
        """
        Open the serial port.

        If self.port == 'auto', calls detect_port() first.
        Raises serial.SerialException if the port cannot be opened.
        After a successful open, starts the background _reader_loop thread.
        """
        port = self.port
        if port == "auto":
            port = self.detect_port()
            if port is None:
                raise serial.SerialException(
                    "No Arduino found on /dev/ttyACM* or /dev/ttyUSB*"
                )

        logger.info(
            "Opening serial port %s @ %d baud (timeout=%.1fs, retries=%d)",
            port, self.baud, self.timeout_s, self.retry_count,
        )
        # Open with no read timeout — the reader thread uses its own loop
        self._serial = serial.Serial(
            port=port,
            baudrate=self.baud,
            timeout=0.1,   # short poll interval for the reader thread
        )
        # Give the Arduino a moment to settle after the port opens
        time.sleep(0.2)
        self._estop.clear()

        # Start the background reader
        self._stop_reader.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="serial-reader",
            daemon=True,
        )
        self._reader_thread.start()
        logger.info("Serial port %s opened; reader thread started.", port)

    def reconnect_loop(self) -> None:
        """
        Keep retrying connect() until it succeeds.

        Called by bridge.py when _forward_to_serial() catches a SerialException.
        This blocks until the Arduino is reconnected — the caller should invoke
        this in a daemon thread so the MQTT loop is not stalled.
        """
        # Stop the existing reader thread first
        self._stop_reader.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None

        attempt = 0
        while True:
            attempt += 1
            try:
                logger.info("reconnect_loop: attempt %d — scanning for Arduino…", attempt)
                # Force auto-detection on reconnect regardless of configured port
                saved_port = self.port
                self.port = "auto"
                self.connect()
                self.port = saved_port
                logger.info("reconnect_loop: reconnected successfully on attempt %d.", attempt)
                return
            except serial.SerialException as exc:
                logger.warning(
                    "reconnect_loop: attempt %d failed (%s) — retrying in %.0fs",
                    attempt, exc, _RECONNECT_RETRY_INTERVAL_S,
                )
                time.sleep(_RECONNECT_RETRY_INTERVAL_S)
            except Exception:
                logger.exception(
                    "reconnect_loop: unexpected error on attempt %d — retrying in %.0fs",
                    attempt, _RECONNECT_RETRY_INTERVAL_S,
                )
                time.sleep(_RECONNECT_RETRY_INTERVAL_S)

    def disconnect(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._stop_reader.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("Serial port closed.")
        self._serial = None

    @property
    def is_connected(self) -> bool:
        return self._serial is not None and self._serial.is_open

    # ── Background reader loop ─────────────────────────────────────────────

    def _reader_loop(self) -> None:
        """
        Run in a daemon thread.  Continuously reads lines from the serial port
        and either:
          • Routes them to the pending send_command() waiter (by setting the
            threading.Event), or
          • Calls self.on_unsolicited() for spontaneous Arduino messages.

        This loop never blocks the MQTT thread.
        """
        logger.debug("_reader_loop: started")
        buffer = b""
        while not self._stop_reader.is_set():
            try:
                if not self.is_connected:
                    time.sleep(0.05)
                    continue

                chunk = self._serial.read(256)
                if not chunk:
                    continue

                buffer += chunk
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    logger.debug("Serial RX: %r", line)
                    self._route_response(line)

            except serial.SerialException:
                logger.warning("_reader_loop: SerialException — port disconnected.")
                self._stop_reader.set()
                # Unblock any waiting send_command()
                with self._write_lock:
                    self._pending_response = None
                    self._waiting_for_response = False
                    self._response_event.set()
                break
            except Exception:
                logger.exception("_reader_loop: unexpected error")
                time.sleep(0.1)

        logger.debug("_reader_loop: stopped")

    def _route_response(self, line: str) -> None:
        """
        Called from _reader_loop with each complete line.

        If send_command() is waiting, deliver the line as its response.
        Otherwise call on_unsolicited() if registered.
        """
        with self._write_lock:
            if self._waiting_for_response:
                self._pending_response = line
                self._waiting_for_response = False
                self._response_event.set()
                return

        # Not waiting — unsolicited message
        if self.on_unsolicited:
            try:
                self.on_unsolicited(line)
            except Exception:
                logger.exception("on_unsolicited callback raised")
        else:
            logger.debug("Serial unsolicited: %r", line)

    # ── Core send/receive ────────────────────────────────────────────────

    def send_command(self, command: str) -> Optional[str]:
        """
        Write `command` to the serial port and wait for a response line.

        Uses a threading.Event so the MQTT loop is never blocked — the
        background _reader_loop signals this event when a line arrives.

        Retry-once-after-1s logic (Section 5.2):
          1. Write command; wait up to serial_timeout_s for a response.
          2. If nothing received and retry_count >= 1: wait 1s, retry.
          3. If still nothing: return None.
        """
        if not self.is_connected:
            raise RuntimeError("SerialHandler.send_command() called before connect()")

        response = self._write_and_wait(command)
        if response is not None:
            return response

        # Retry-once-after-1s (Section 5.2)
        for attempt in range(1, self.retry_count + 1):
            logger.warning(
                "No serial response to %r — retrying (attempt %d/%d) after 1s",
                command, attempt, self.retry_count,
            )
            time.sleep(1.0)
            response = self._write_and_wait(command)
            if response is not None:
                return response

        logger.error("No serial response to %r after retries — SERIAL_TIMEOUT", command)
        return None

    def _write_and_wait(self, command: str) -> Optional[str]:
        """Write one line, signal the reader thread to capture the response, wait."""
        assert self._serial is not None

        with self._write_lock:
            # Arm the response event before writing so we can't miss a fast reply
            self._pending_response = None
            self._response_event.clear()
            self._waiting_for_response = True

            line = command.strip() + "\n"
            self._serial.write(line.encode("utf-8"))
            self._serial.flush()
            logger.debug("Serial TX: %r", command.strip())

        # Wait outside the lock so the reader thread can acquire it to deliver
        signalled = self._response_event.wait(timeout=self.timeout_s)
        if not signalled:
            # Timeout — disarm the waiter flag
            with self._write_lock:
                self._waiting_for_response = False
            return None

        with self._write_lock:
            result = self._pending_response
            self._pending_response = None

        return result

    # ── Emergency stop ────────────────────────────────────────────────────

    def emergency_stop(self) -> None:
        """
        Fire-and-forget emergency stop (Item 6).

        Flushes both input and output buffers, writes !\n, sets the estop
        flag, and returns immediately — does NOT call readline() or wait
        for any acknowledgement from the Arduino.

        Called by bridge._on_emergency() instead of _forward_to_serial().
        """
        self._estop.set()
        # Unblock any waiting send_command()
        with self._write_lock:
            self._waiting_for_response = False
            self._pending_response = None
            self._response_event.set()

        if self._serial and self._serial.is_open:
            try:
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
                self._serial.write(b"!\n")
                self._serial.flush()
                logger.warning("Emergency stop sent to Arduino.")
            except serial.SerialException:
                logger.exception("emergency_stop: SerialException while writing !")
        else:
            logger.warning("emergency_stop: serial port not connected — ! not sent.")

    # ── Health check ──────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """
        Send M114 and return True if any response is received within the
        normal timeout/retry window. Used by the reconnect-cleanup sequence.
        """
        response = self.send_command("M114")
        return response is not None
