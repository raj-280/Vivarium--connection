#!/usr/bin/env python3
"""
pi/tests/fake_arduino.py — Throwaway echo script for Stage 7 socat testing.

This script sits on one end of a socat virtual serial port pair and mimics
the Arduino (RackMonitor_Mega_IS_S.ino) well enough to exercise bridge.py's
full ACK → forward → response flow end-to-end, without any real hardware.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SETUP (Linux / macOS / WSL on Windows — all three terminals in the SAME shell)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Terminal 1 — create the virtual serial port pair (leave this running):

    socat -d -d pty,raw,echo=0 pty,raw,echo=0

  socat prints two lines like:
    2024/... N pty  opened as /dev/pts/3
    2024/... N pty  opened as /dev/pts/4

  /dev/pts/3  → the bridge's end  (write into device.conf as serial_port)
  /dev/pts/4  → this script's end  (pass as argv[1] below)

  The two numbers are arbitrary; always use what socat actually prints.

Terminal 2 — start this script on the script's end:

    python pi/tests/fake_arduino.py /dev/pts/4

Terminal 3 — start bridge.py, pointing device.conf at the bridge's end:

    export GANTRY_DEVICE_CONF=/tmp/device.conf.test
    cp pi/tests/device.conf.test /tmp/device.conf.test
    # Edit /tmp/device.conf.test: set serial_port = /dev/pts/3
    python pi/bridge.py
    # OR: python -m pi.bridge  (from repo root)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT TO OBSERVE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. On startup, bridge.py runs the reconnect-cleanup sequence:
     → sends M114 (health check + homed-flag verify)
     → this script replies with the fake M114 line (homed: X=Y Y=Y C=Y)
     → bridge.py logs the parsed homed flags and publishes BRIDGE_RECONNECTED

2. From the MQTT side (mosquitto_pub in another terminal, or via the server):
     mosquitto_pub -t vivarium/rack/rack-test/command -m "G28"
     → bridge.py publishes COMMAND_ACK:G28  (before touching serial)
     → bridge.py forwards "G28" to serial
     → this script replies "Yo! On my way!"
     → bridge.py publishes "Yo! On my way!" on the response topic

3. Send CAPTURE to verify interception (never reaches serial):
     mosquitto_pub -t vivarium/rack/rack-test/command -m "CAPTURE"
     → bridge.py publishes COMMAND_ACK:CAPTURE  ← still happens
     → bridge.py logs "not yet implemented (Stage 10)"
     → NO serial traffic — fake_arduino.py sees nothing

4. Test SERIAL_TIMEOUT by failing a specific command:
     FAIL_COMMANDS=G28 python pi/tests/fake_arduino.py /dev/pts/4
     → fake_arduino.py silently drops G28 both times
     → bridge.py publishes SERIAL_TIMEOUT:G28 after the retry window

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE TABLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Command     Response
─────────── ──────────────────────────────────────────────────────────
M114        X:0.00 Y:0.00 C:0.00 homed:X=Y Y=Y C=Y
G28         Yo! On my way!
M700 ...    Yo! On my way!
M701 ...    Yo! On my way!
M710        Yo! On my way!
M711        Yo! On my way!
!           Yo! On my way!
(anything)  Yo! On my way!
$FAIL_CMD   (silence — triggers bridge's serial_timeout / retry path)
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

try:
    import serial
except ImportError:
    print(
        "ERROR: pyserial is not installed. Run:  pip install pyserial",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Fake responses ────────────────────────────────────────────────────────────

FAKE_M114 = "X:0.00 Y:0.00 C:0.00 homed:X=Y Y=Y C=Y"
FAKE_ACK  = "Yo! On my way!"

# Commands to silently drop (no response) — simulates a non-responsive Arduino.
# Set via the FAIL_COMMANDS environment variable, e.g.:
#   FAIL_COMMANDS=G28,M700  python pi/tests/fake_arduino.py /dev/pts/4
FAIL_COMMANDS: set[str] = {
    c.strip().upper()
    for c in os.environ.get("FAIL_COMMANDS", "").split(",")
    if c.strip()
}

# Realistic Arduino response delay (ms).  Keeps the test feeling natural.
RESPONSE_DELAY_S = 0.05


# ── Main loop ─────────────────────────────────────────────────────────────────

def choose_response(command: str) -> Optional[str]:
    """Return the fake reply for `command`, or None to simulate a timeout."""
    base = command.split()[0].upper() if command.strip() else ""

    if base in FAIL_COMMANDS:
        return None  # deliberate silence → triggers bridge's retry / SERIAL_TIMEOUT

    if base == "M114":
        return FAKE_M114

    return FAKE_ACK


def main() -> None:
    if len(sys.argv) < 2:
        print(
            f"Usage: python {sys.argv[0]} <serial_port>\n"
            f"Example: python {sys.argv[0]} /dev/pts/4",
            file=sys.stderr,
        )
        sys.exit(1)

    port = sys.argv[1]

    if FAIL_COMMANDS:
        print(f"[fake_arduino] Will NOT respond to: {', '.join(sorted(FAIL_COMMANDS))}")

    print(f"[fake_arduino] Opening {port} @ 115200 baud …")
    try:
        ser = serial.Serial(port, baudrate=115200, timeout=1)
    except serial.SerialException as exc:
        print(f"[fake_arduino] ERROR: {exc}", file=sys.stderr)
        print(
            "  Make sure socat is running and you're pointing at the script's end "
            "(NOT the bridge's end).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[fake_arduino] Listening on {port}. Ctrl-C to quit.\n")

    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue  # timeout — nothing sent yet

            command = raw.decode("utf-8", errors="replace").strip()
            if not command:
                continue

            print(f"[fake_arduino] ← RX: {command!r}")

            reply = choose_response(command)
            if reply is None:
                print(
                    f"[fake_arduino]   (silently dropping {command.split()[0]!r} "
                    "— simulating timeout)"
                )
                continue

            time.sleep(RESPONSE_DELAY_S)
            ser.write((reply + "\n").encode("utf-8"))
            ser.flush()
            print(f"[fake_arduino] → TX: {reply!r}\n")

    except KeyboardInterrupt:
        print("\n[fake_arduino] Interrupted — closing port.")
    finally:
        ser.close()
        print("[fake_arduino] Done.")


if __name__ == "__main__":
    main()
