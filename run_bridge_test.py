#!/usr/bin/env python3
"""
run_bridge_test.py — Runs the REAL pi/bridge.py with a fake serial backend.

Nothing in pi/bridge.py, pi/services/serial_handler.py, pi/services/
camera_handler.py, or pi/services/scan_executor.py is modified or
duplicated. This script only patches serial.Serial -> FakeSerial BEFORE
those modules get imported, so SerialHandler.connect() transparently opens
a fake port instead of failing on a missing /dev/ttyACM0.

Usage (run from the repo root, e.g.
  C:\\Users\\rajes\\Downloads\\VivariumConnection_nee> ):

    python vivarium-test\\run_bridge_test.py

Requires device.conf already provisioned (see step-by-step below) and the
serial_port value in it can be left as the placeholder /dev/ttyACM0 — it is
never actually opened, FakeSerial ignores the value.
"""

import os
import sys
from pathlib import Path

# 1. Make the fake_serial.py module importable, then patch serial.Serial
#    BEFORE anything under pi/ gets imported.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_serial import install_fake_serial  # noqa: E402
install_fake_serial()

# 2. Put pi/ on sys.path exactly like bridge.py expects when run directly.
repo_root = Path(__file__).resolve().parent.parent
pi_dir = repo_root / "pi"
sys.path.insert(0, str(pi_dir))

# 3. Run the REAL bridge — same entrypoint as `python pi/bridge.py`.
if __name__ == "__main__":
    import bridge  # pi/bridge.py, unmodified
    b = bridge.Bridge()
    b.start()
    b.run_forever()
