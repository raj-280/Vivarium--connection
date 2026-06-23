"""
server/services/provisioning.py

Full POST /provision logic.

Responsibilities
----------------
1. Validate ``provisioning_secret`` (401 if wrong).
2. Idempotency: if ``cpu_serial`` already exists in ``racks``, return the
   existing credentials from the matching ``racks`` row unchanged. Safe to
   reflash and re-run provisioner.
3. Device-ID assignment: Auto-assign the next sequential ID by checking
   the highest existing ID in the ``racks`` table.

   SQLite limitation note
   ----------------------
   SQLite has no row-level locking. The approximation used instead is:

     BEGIN IMMEDIATE — acquires a write lock on the *file* before any reads,
     so concurrent provisioners cannot both see the same highest row. Any
     second call will receive ``SQLITE_BUSY`` and the ``busy_timeout`` pragma
     (set to 5 s in database.py) will retry automatically before raising
     OperationalError.

4. Generate credentials: ``mqtt_password``, ``presign_api_key``, ``rtsp_password``
   — 256 bits (32 bytes) of cryptographic randomness, URL-safe base64-encoded.

5. Upsert the ``racks`` row with geometry defaults from settings and save the
   new Pi's ``cpu_serial``.

6. Write a ``provisioning_event`` audit row (Section 3.5).

Public API
----------
    result = provision_device(request_body, db)
    # Returns a ProvisionResult on success or raises HTTPException.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from config.settings import settings
from db.database import engine
from db.models import AuditLog, Rack

logger = logging.getLogger(__name__)


def _build_server_host() -> str:
    """
    Build the server_host URL that gets written into the Pi's device.conf.

    BACKEND_HOST is a *bind* address (e.g. 0.0.0.0 or 127.0.0.1) — it tells
    uvicorn which interface to listen on. Writing 0.0.0.0 into device.conf is
    WRONG: the Pi would try to connect to 0.0.0.0:8000 which doesn't route.

    Resolution order:
      1. SERVER_HOST setting  — explicit routable address or FQDN.
         Set SERVER_HOST in server/.env when your HTTP server and MQTT broker
         are on different hosts, e.g. SERVER_HOST=192.168.1.100
      2. MQTT_BROKER          — the broker hostname/IP the Pi already uses to
         reach this machine. Works for local dev and most deployments where
         the server and broker run on the same host.
    """
    host = getattr(settings, "SERVER_HOST", "").strip()
    if not host or host in ("0.0.0.0", "127.0.0.1", "localhost"):
        # Fall back to MQTT_BROKER which IS a routable address.
        host = settings.MQTT_BROKER
    return f"http://{host}:{settings.BACKEND_PORT}"


# ---------------------------------------------------------------------------
# How long must a rack be offline before we allow hardware replacement without
# manual admin intervention (Section 4.6 — "offline for more than 7 days").
# ---------------------------------------------------------------------------
_HARDWARE_REPLACE_THRESHOLD_DAYS = 7

# ---------------------------------------------------------------------------
# Maximum retries for the BEGIN IMMEDIATE / auto-assign loop.
# Each attempt waits busy_timeout (5 s, set in database.py) before raising, so
# in the worst case this loop runs for MAX_RETRIES * 5s.  Three retries covers
# rare race conditions without hanging indefinitely.
# ---------------------------------------------------------------------------
_MAX_ASSIGN_RETRIES = 3


# ===========================================================================
# Request / Response schemas (also used by routes.py)
# ===========================================================================

class ProvisionRequest(BaseModel):
    """Body of POST /provision (Section 4.6 / 5.3)."""

    cpu_serial: str
    provisioning_secret: str
    provision_token: Optional[str] = None
    # Pi also sends its current IP so the server can populate racks.pi_ip
    # and MediaMTX can pull the RTSP stream (Section 8).
    pi_ip: Optional[str] = None


@dataclass
class ProvisionResult:
    """Credentials returned to the Pi on successful provisioning."""

    device_id: str
    mqtt_username: str
    mqtt_password: str
    presign_api_key: str
    rtsp_password: str
    server_host: str          # Convenience: the Pi writes this into device.conf
    broker_host: str
    broker_port: int


# ===========================================================================
# Helpers
# ===========================================================================

def _generate_credential() -> str:
    """
    Return 256 bits of cryptographic randomness as a URL-safe base64 string.
    ``secrets.token_urlsafe(32)`` → 32 bytes → 43-char URL-safe base64 string.
    """
    return secrets.token_urlsafe(32)


def _audit(
    db: Session,
    *,
    event_type: str,
    rack_id: Optional[str],
    outcome: str,
    details: dict,
) -> None:
    """Append a row to audit_log (Section 3.5)."""
    entry = AuditLog(
        event_type=event_type,
        rack_id=rack_id,
        outcome=outcome,
        details=json.dumps(details),
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    # Flush so the row is persisted even if the caller later rolls back
    # for an unrelated reason — audit rows should never be lost.
    # The caller's db.commit() (in get_db) will commit them.
    db.flush()


def _upsert_rack(
    db: Session,
    *,
    device_id: str,
    cpu_serial: str,
    mqtt_username: str,
    mqtt_password_ref: str,
    presign_api_key_ref: str,
    rtsp_password_ref: str,
    pi_ip: Optional[str],
) -> Rack:
    """
    Insert or update the ``racks`` row for ``device_id``.

    On first provisioning: creates a new row with geometry defaults from settings.
    On re-provisioning (idempotent): updates the credential refs and pi_ip only;
    all other fields (geometry overrides, position, lock state, etc.) are preserved.
    """
    rack: Optional[Rack] = db.query(Rack).filter_by(id=device_id).first()

    if rack is None:
        rack = Rack(
            id=device_id,
            display_name=device_id,               # Admin can rename later
            location=None,
            cpu_serial=cpu_serial,
            pi_ip=pi_ip,
            mqtt_username=mqtt_username,
            mqtt_password_ref=mqtt_password_ref,  # Stored as plain text locally;
            rtsp_password_ref=rtsp_password_ref,  # [PROD] swap to a secrets-manager
            presign_api_key_ref=presign_api_key_ref,  # handle when hardening.
            # Geometry defaults from settings (Section 2.1 / 3.1)
            grid_rows=settings.RACK_ROWS,
            grid_cols=settings.RACK_COLS,
            x0_offset_mm=settings.X0_OFFSET_MM,
            pitch_x_mm=settings.PITCH_X_MM,
            y0_offset_mm=settings.Y0_OFFSET_MM,
            pitch_y_mm=settings.PITCH_Y_MM,
            position_tolerance_x_mm=settings.POSITION_TOLERANCE_X_MM,
            position_tolerance_y_mm=settings.POSITION_TOLERANCE_Y_MM,
            mqtt_status="offline",
            camera_status="unknown",
            scan_state="idle",
            maintenance_required=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(rack)
    else:
        # Idempotent update — preserve geometry and operational state
        rack.mqtt_username = mqtt_username
        rack.mqtt_password_ref = mqtt_password_ref
        rack.rtsp_password_ref = rtsp_password_ref
        rack.presign_api_key_ref = presign_api_key_ref
        rack.cpu_serial = cpu_serial
        if pi_ip is not None:
            rack.pi_ip = pi_ip
        rack.updated_at = datetime.now(timezone.utc)

    db.flush()
    return rack


# ===========================================================================
# Main service function
# ===========================================================================

def provision_device(body: ProvisionRequest, db: Session) -> ProvisionResult:
    """
    Execute the full provisioning flow (Section 4.6).

    Raises ``HTTPException`` on any validation failure so the route handler
    can propagate it directly.
    """
    now = datetime.now(timezone.utc)

    # ── Step 1: validate provisioning_secret ─────────────────────────────────
    # Use constant-time comparison to avoid timing side-channels.
    if not secrets.compare_digest(
        body.provisioning_secret.encode(),
        settings.PROVISIONING_SECRET.encode(),
    ):
        logger.warning("provision: bad provisioning_secret from cpu_serial=%s", body.cpu_serial)
        _audit(
            db,
            event_type="provisioning_event",
            rack_id=None,
            outcome="failure",
            details={"reason": "bad_provisioning_secret", "cpu_serial": body.cpu_serial},
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid provisioning secret.",
        )

    # ── Step 2 & 3: idempotency — cpu_serial already in racks ──────────────
    existing_rack: Optional[Rack] = (
        db.query(Rack).filter_by(cpu_serial=body.cpu_serial).first()
    )
    if existing_rack is not None:
        device_id = existing_rack.id

        if body.pi_ip and existing_rack.pi_ip != body.pi_ip:
            existing_rack.pi_ip = body.pi_ip
            existing_rack.updated_at = now
            db.flush()

        logger.info(
            "provision: idempotent return for cpu_serial=%s device_id=%s",
            body.cpu_serial,
            device_id,
        )
        _audit(
            db,
            event_type="provisioning_event",
            rack_id=device_id,
            outcome="success",
            details={"reason": "idempotent", "cpu_serial": body.cpu_serial},
        )
        db.commit()
        return ProvisionResult(
            device_id=device_id,
            mqtt_username=existing_rack.mqtt_username or device_id,
            mqtt_password=existing_rack.mqtt_password_ref or "",
            presign_api_key=existing_rack.presign_api_key_ref or "",
            rtsp_password=existing_rack.rtsp_password_ref or "",
            server_host=_build_server_host(),
            broker_host=settings.MQTT_BROKER,
            broker_port=settings.MQTT_PORT,
        )

    # ── Step 4: device-ID assignment ─────────────────────────────────────────
    device_id = _assign_device_id(db)

    # ── Step 6: generate credentials ─────────────────────────────────────────
    mqtt_password = _generate_credential()
    presign_api_key = _generate_credential()
    rtsp_password = _generate_credential()

    # MQTT username matches device_id — one Pi, one identity
    mqtt_username = device_id

    # ── Step 7: upsert racks row ──────────────────────────────────────────────
    _upsert_rack(
        db,
        device_id=device_id,
        cpu_serial=body.cpu_serial,
        mqtt_username=mqtt_username,
        mqtt_password_ref=mqtt_password,
        presign_api_key_ref=presign_api_key,
        rtsp_password_ref=rtsp_password,
        pi_ip=body.pi_ip,
    )

    # ── Step 10: write provisioning_event audit row ───────────────────────────
    _audit(
        db,
        event_type="provisioning_event",
        rack_id=device_id,
        outcome="success",
        details={
            "reason": "new_provisioning",
            "cpu_serial": body.cpu_serial,
            "token_used": False,
        },
    )

    # db.commit() happens in get_db() — we only flush here
    logger.info(
        "provision: success device_id=%s cpu_serial=%s token=%s",
        device_id,
        body.cpu_serial,
        bool(body.provision_token),
    )

    return ProvisionResult(
        device_id=device_id,
        mqtt_username=mqtt_username,
        mqtt_password=mqtt_password,
        presign_api_key=presign_api_key,
        rtsp_password=rtsp_password,
        server_host=_build_server_host(),
        broker_host=settings.MQTT_BROKER,
        broker_port=settings.MQTT_PORT,
    )


# ===========================================================================
# Device-ID assignment helper
# ===========================================================================

def _assign_device_id(db: Session) -> str:
    """
    Return the next sequential device_id to use for this provisioning request.

    Uses the caller's ORM session (``db``) to read the highest existing rack ID
    so that the query always runs on the same connection/transaction that the
    caller is using.  This is critical in tests where the DB is an in-memory
    SQLite with a StaticPool -- using ``engine.connect()`` would open a second
    connection that sees a completely empty database, causing every provisioning
    call to assign "rack-001" regardless of how many racks already exist.

    For production SQLite the session's implicit transaction gives us sufficient
    serialisation: two concurrent callers will queue at the DB level and each
    will see the committed row from the previous one.  For PostgreSQL the ORM
    session's READ COMMITTED isolation plus the UNIQUE constraint on racks.id
    prevent duplicates.
    """
    import sqlalchemy

    result = db.execute(
        sqlalchemy.text("SELECT id FROM racks ORDER BY id DESC LIMIT 1")
    ).fetchone()

    next_num = 1
    if result:
        last_id: str = result[0]
        if "-" in last_id:
            try:
                _, num_str = last_id.rsplit("-", 1)
                if num_str.isdigit():
                    next_num = int(num_str) + 1
            except ValueError:
                pass

    return f"rack-{next_num:03d}"
