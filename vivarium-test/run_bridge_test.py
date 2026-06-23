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
#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# 1. Add vivarium-test/ to sys.path so fake_serial.py is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_serial import install_fake_serial   # no dotted import — just direct
install_fake_serial()

# 2. Add pi/ to sys.path so bridge.py can import its own modules
repo_root = Path(__file__).resolve().parent.parent
pi_dir = repo_root / "pi"
sys.path.insert(0, str(pi_dir))

# 3. Patch detect_port() BEFORE importing bridge.py so Windows dev machines
#    (which have no /dev/ttyACM* paths) don't raise SerialException.
#    serial_handler.connect() calls detect_port() only when port == "auto".
#    Returning "FAKE_COM0" means serial.Serial("FAKE_COM0", ...) is called —
#    which is already patched to FakeSerial, so any port name works fine.
from services import serial_handler as _sh
_sh.SerialHandler.detect_port = lambda self: "FAKE_COM0"
print("[run_bridge_test] detect_port() patched → 'FAKE_COM0'")

# 4. Run the real bridge
if __name__ == "__main__":
    import bridge
    b = bridge.Bridge()
    b.start()
    b.run_forever()