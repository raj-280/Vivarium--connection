"""
fake_serial.py — In-process virtual Arduino, no socat / com0com / WSL needed.

WHAT THIS DOES
──────────────
serial_handler.py calls `serial.Serial(port=..., baudrate=..., timeout=0.1)`
directly inside connect(). This file provides a class — FakeSerial — that
has the exact same methods pyserial's real Serial object has:
    write(), read(), flush(), close(), open(), is_open,
    reset_input_buffer(), reset_output_buffer()

We monkeypatch `serial.Serial` to point at FakeSerial BEFORE bridge.py /
serial_handler.py are imported. From that point on, every line of
serial_handler.py, bridge.py, camera_handler.py, scan_executor.py runs
EXACTLY as it would on the real Pi — unmodified, same code path. The only
thing that's fake is what's sitting "on the other end of the wire."

FakeSerial behaves like a real Arduino running RackMonitor_Mega_IS_S.ino:
  - M114        -> "X:0.00 Y:0.00 C:0.00 homed:X=Y Y=Y C=Y"
  - M705        -> "ROWS=12 COLS=7"
  - M706        -> "Pitch X=50.0 Y=50.0"
  - M707        -> "Offsets X0=0.0 Y0=0.0"
  - M799        -> "LIMITS X=300.00 Y=200.00 C=180.00"
  - everything else -> "ok"   (NOT "Yo! On my way!" — that string is
                               filtered as noise by serial_handler and
                               would cause every motion command to time out)
  - commands listed in FAIL_COMMANDS -> silence (to trigger SERIAL_TIMEOUT)
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Optional

# Commands to silently drop — trigger SERIAL_TIMEOUT path in serial_handler.
# Usage: FAIL_COMMANDS=G28 python vivarium-test\run_bridge_test.py
FAIL_COMMANDS: set[str] = {
    c.strip().upper()
    for c in os.environ.get("FAIL_COMMANDS", "").split(",")
    if c.strip()
}

FAKE_M114 = "X:0.00 Y:0.00 C:0.00 homed:X=Y Y=Y C=Y"
FAKE_M705 = "ROWS=12 COLS=7"
FAKE_M706 = "Pitch X=50.0 Y=50.0"
FAKE_M707 = "Offsets X0=0.0 Y0=0.0"
FAKE_M799 = "LIMITS X=300.00 Y=200.00 C=180.00"

# NOTE: "ok" not "Yo! On my way!" — serial_handler._write_and_wait() treats
# "yo! on my way!" as firmware noise, discards it, and waits for a second
# line that never comes → every motion command times out. "ok" passes through.
FAKE_ACK = "ok"

# Simulated Arduino reply latency (seconds).
RESPONSE_DELAY_S = 0.05

_FIXED_REPLIES = {
    "M114": FAKE_M114,
    "M705": FAKE_M705,
    "M706": FAKE_M706,
    "M707": FAKE_M707,
    "M799": FAKE_M799,
}


def _choose_response(command: str) -> Optional[str]:
    base = command.split()[0].upper() if command.strip() else ""
    if base in FAIL_COMMANDS:
        return None  # deliberate silence -> SERIAL_TIMEOUT path
    if base in _FIXED_REPLIES:
        return _FIXED_REPLIES[base]
    return FAKE_ACK


class FakeSerial:
    """
    Drop-in stand-in for serial.Serial.

    Mimics the subset of pyserial's API that serial_handler.py actually uses:
        Serial(port=, baudrate=, timeout=)
        .write(bytes) / .flush() / .read(n) / .close() / .open()
        .is_open
        .reset_input_buffer() / .reset_output_buffer()
    """

    def __init__(self, port: str = "", baudrate: int = 115200, timeout: float = 0.1, **kwargs):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True

        # Bytes the "Arduino" has written, waiting to be .read() by the bridge.
        self._rx_queue: "queue.Queue[bytes]" = queue.Queue()

        # Background "Arduino" worker thread.
        self._cmd_queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._arduino_loop, daemon=True)
        self._worker.start()

        if FAIL_COMMANDS:
            print(f"[fake_serial] Will NOT respond to: {', '.join(sorted(FAIL_COMMANDS))}")
        print(f"[fake_serial] Virtual Arduino ready (no socat/com0com/WSL).")

    # ── "Arduino" side ──────────────────────────────────────────────────
    def _arduino_loop(self) -> None:
        while not self._stop.is_set():
            try:
                command = self._cmd_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            print(f"[fake_serial] <- RX: {command!r}")
            reply = _choose_response(command)
            if reply is None:
                print(f"[fake_serial]    (silently dropping {command.split()[0]!r} — simulating timeout)")
                continue

            time.sleep(RESPONSE_DELAY_S)
            self._rx_queue.put((reply + "\n").encode("utf-8"))
            print(f"[fake_serial] -> TX: {reply!r}")

    # ── pyserial-compatible API ──────────────────────────────────────────

    def open(self) -> None:
        """
        Called by serial_handler.connect() after the ttyACM bootloader wait
        (close() → sleep(9) → open()). Re-opens the fake port and restarts
        the worker thread if close() killed it.
        """
        self.is_open = True
        if self._stop.is_set():
            self._stop.clear()
            self._worker = threading.Thread(target=self._arduino_loop, daemon=True)
            self._worker.start()
        print("[fake_serial] open() called — fake port re-opened.")

    def close(self) -> None:
        self.is_open = False
        self._stop.set()
        print("[fake_serial] close() called — fake port closed.")

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", errors="replace").strip()
        if text:
            self._cmd_queue.put(text)
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1) -> bytes:
        """
        Mimics pyserial: blocks up to self.timeout seconds, returns whatever
        bytes are available (possibly empty).
        """
        deadline = time.time() + self.timeout
        chunks = b""
        while time.time() < deadline and len(chunks) < size:
            remaining = deadline - time.time()
            try:
                chunk = self._rx_queue.get(timeout=max(0, remaining))
                chunks += chunk
            except queue.Empty:
                break
        return chunks[:size] if chunks else b""

    def reset_input_buffer(self) -> None:
        while not self._rx_queue.empty():
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break

    def reset_output_buffer(self) -> None:
        while not self._cmd_queue.empty():
            try:
                self._cmd_queue.get_nowait()
            except queue.Empty:
                break


def install_fake_serial() -> None:
    """
    Monkeypatch serial.Serial -> FakeSerial. Call this BEFORE importing
    bridge.py / serial_handler.py.
    """
    import serial
    serial.Serial = FakeSerial
    print("[fake_serial] serial.Serial monkeypatched -> FakeSerial")