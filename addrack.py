"""
add_test_rack.py — TEMPORARY debug helper.

Inserts (or updates) a fake rack row directly into vivarium.db so you can
check whether the frontend renders a rack row at all, independent of
whether a real Pi has provisioned yet.

Run from your server/ folder (same place vivarium.db lives), with your venv
active:

    cd server
    python add_test_rack.py

To remove it again later:

    python add_test_rack.py --delete
"""

import sys
from datetime import datetime

sys.path.insert(0, ".")  # so `from db.models import ...` resolves like main.py does

from server.db.database import SessionLocal, create_tables  # noqa: E402
from server.db.models import Rack  # noqa: E402

RACK_ID = "rack-test-001"


def add_rack():
    create_tables()  # no-op if tables already exist
    db = SessionLocal()
    try:
        existing = db.query(Rack).filter_by(id=RACK_ID).first()
        if existing:
            print(f"{RACK_ID} already exists — updating it instead of inserting.")
            existing.mqtt_status = "online"
            existing.camera_status = "online"
            existing.updated_at = datetime.utcnow()
            db.commit()
            print("Updated.")
            return

        rack = Rack(
            id=RACK_ID,
            display_name="Test Rack (temporary)",
            location="debug",
            pi_ip=None,
            mqtt_username=RACK_ID,
            mqtt_password_ref="fake-not-real",
            rtsp_password_ref="fake-not-real",
            presign_api_key_ref="fake-not-real",
            cpu_serial="FAKE0000TEST",
            grid_rows=12,
            grid_cols=7,
            x0_offset_mm=0.0,
            pitch_x_mm=50.0,
            y0_offset_mm=0.0,
            pitch_y_mm=50.0,
            position_tolerance_x_mm=3.0,
            position_tolerance_y_mm=2.0,
            mqtt_status="online",      # fake it as online so it doesn't look greyed-out
            camera_status="online",
            last_position_x=0.0,
            last_position_y=0.0,
            last_position_c=0.0,
            homed_x=True,
            homed_y=True,
            homed_c=True,
            last_homed_at=datetime.utcnow(),
            lock_holder_user_id=None,
            lock_type=None,
            lock_acquired_at=None,
            lock_expires_at=None,
            scan_state="idle",
            maintenance_required=False,
            limit_x_mm=300.0,
            limit_y_mm=200.0,
            limit_c_mm=180.0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(rack)
        db.commit()
        print(f"Inserted fake rack: {RACK_ID}")
    finally:
        db.close()


def delete_rack():
    db = SessionLocal()
    try:
        existing = db.query(Rack).filter_by(id=RACK_ID).first()
        if not existing:
            print(f"{RACK_ID} not found — nothing to delete.")
            return
        db.delete(existing)
        db.commit()
        print(f"Deleted {RACK_ID}.")
    finally:
        db.close()


if __name__ == "__main__":
    if "--delete" in sys.argv:
        delete_rack()
    else:
        add_rack()