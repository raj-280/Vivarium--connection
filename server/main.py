"""
server/main.py

FastAPI application + lifespan startup sequence (Section 4.2 / Stage 5).

Startup order (lifespan):
  1. create_tables()              — create vivarium.db tables on first run
  2. gantry_state.reconcile_from_db() — seed in-memory mirror from DB
  3. mqtt_client.connect()        — connect to Mosquitto; register handlers
  4. ws_registry.set_loop()       — give the WS registry the running event loop
  5. start_lock_sweep_task()      — background sweep for expired locks (every 2s)
  6. scan_engine.start()          — APScheduler: 1-minute auto-scan job (Stage 11)

Middleware stack (outermost → innermost):
  CORSMiddleware   — allow configured origins (CORS_ALLOWED_ORIGINS)
  CSRFMiddleware   — double-submit cookie (no-op when CSRF_ENABLED=False)
  slowapi Limiter  — rate limiting on command + presign endpoints

Routers:
  /         — api/routes.py  (HTTP)
  /ws       — api/websocket.py (WebSocket)

FIXES applied in this version
──────────────────────────────
• Mismatch 7: CAPTURE_ERROR from Pi now has a handler that releases the
  capture lock so the rack isn't permanently stuck.
• Mismatch 8: SCAN_KEEPALIVE from Pi now has a handler that calls extend_lock()
  to reset the scan lock expiry, preventing the lock sweep from killing a
  running scan mid-flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from db.database import create_tables, db_session
from db.models import AuditLog, ImageRecord, Rack
from core.locking import extend_lock, release_lock, start_lock_sweep_task
from core.state import gantry_state
# from middleware.csrf import CSRFMiddleware
from middleware.rate_limit import limiter
from services.mqtt_client import mqtt_client
from services.cache import cache
from services.s3_handler import ImagePathError, validate_image_path
from services.scan_engine import scan_engine          # Stage 11
from services.position_monitor import position_monitor  # Stage 11

# Import routers
from api.routes import router as http_router
from api.websocket import router as ws_router, ws_registry, relay_mqtt_to_ws

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ===========================================================================
# Stage 9 — MQTT message handlers for the capture flow
# ===========================================================================

def _on_response_message(
    rack_id: Optional[str], subtopic: str, payload: Any
) -> None:
    """
    Handler for vivarium/rack/{id}/response messages (Section 5.2 / 4.3).

    Relevant response prefixes handled:
      CAPTURE_STARTED  — extend capture lock (keepalive).
      CAPTURE_DONE     — belt-and-suspenders capture lock release.
      CAPTURE_ERROR    — release capture lock so rack isn't permanently stuck
                         (FIX Mismatch 7).
      SCAN_KEEPALIVE   — extend scan lock so it doesn't expire mid-scan
                         (FIX Mismatch 8).
      BRIDGE_RECONNECTED — mark stale-homing check due.
      LAYOUT_CONFIG    — update gantry_state + DB.
      M799 LIMITS      — update limit fields in gantry_state + DB.
      M705/M706/M707   — update layout fields in gantry_state + layout_cache.
      M114             — position verification via position_monitor.
      COMMAND_ACK:*    — auto-publish M114 after motion commands.
    """
    if rack_id is None:
        return

    # Normalise payload to a string for prefix matching
    raw = payload if isinstance(payload, str) else json.dumps(payload)

    # ── M114 position verification (Section 4.4) ──────────────────────────
    if "X:" in raw and "homed:" in raw:
        position_monitor.on_m114(rack_id, raw)

    # ── Auto-M114 follow-up after motion command ACK (Section 4.4) ────────
    _MOTION_CMDS = ("M700", "M701", "M702", "M703", "M704", "G28")
    if raw.startswith("COMMAND_ACK:"):
        acked_cmd = raw.split(":", 1)[1].strip().split()[0].upper()
        if acked_cmd in _MOTION_CMDS:
            try:
                from services.mqtt_client import mqtt_client as _mc
                _mc.publish_command(rack_id, "M114", qos=1)
                logger.debug(
                    "Auto-M114 published for rack=%s after ACK of %s",
                    rack_id, acked_cmd,
                )
            except Exception:
                logger.exception(
                    "Auto-M114 publish failed for rack=%s after ACK of %s",
                    rack_id, acked_cmd,
                )

    # ── BRIDGE_RECONNECTED (Section 5.2 / 4.4) ───────────────────────────
    if raw.startswith("BRIDGE_RECONNECTED"):
        logger.info("BRIDGE_RECONNECTED received for rack=%s — marking stale-homing check due.", rack_id)
        position_monitor.mark_stale_check_due(rack_id)

    # ── LAYOUT_CONFIG (Item 1 / Section 5.2 step 5) ───────────────────────
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict) and data.get("type") == "LAYOUT_CONFIG":
            _fields = {
                k: data[k] for k in (
                    "grid_rows", "grid_cols",
                    "pitch_x_mm", "pitch_y_mm",
                    "x0_offset_mm", "y0_offset_mm",
                    "limit_x_mm", "limit_y_mm", "limit_c_mm",
                ) if k in data
            }
            if _fields:
                gantry_state.upsert(rack_id, **_fields)
                logger.info(
                    "LAYOUT_CONFIG applied to gantry_state for rack=%s: %s",
                    rack_id, _fields,
                )
                def _persist_layout(rid: str, fields: dict) -> None:
                    try:
                        with db_session() as db:
                            rack = db.query(Rack).filter_by(id=rid).first()
                            if rack:
                                for col, val in fields.items():
                                    if hasattr(rack, col):
                                        setattr(rack, col, val)
                    except Exception:
                        logger.exception("_persist_layout failed for rack=%s", rid)
                import threading as _thr
                _thr.Thread(
                    target=_persist_layout,
                    args=(rack_id, _fields),
                    daemon=True,
                    name=f"layout-persist-{rack_id}",
                ).start()

    # ── M799 LIMITS response handler ──────────────────────────────────────
    if raw.startswith("LIMITS"):
        import re as _re
        m_limits = _re.search(
            r'LIMITS\s+X=([-\d.]+)\s+Y=([-\d.]+)\s+C=([-\d.]+)', raw, _re.IGNORECASE
        )
        if m_limits:
            lx = float(m_limits.group(1))
            ly = float(m_limits.group(2))
            lc = float(m_limits.group(3))
            gantry_state.upsert(rack_id, limit_x_mm=lx, limit_y_mm=ly, limit_c_mm=lc)
            logger.info("M799 LIMITS received rack=%s: X=%.2f Y=%.2f C=%.2f", rack_id, lx, ly, lc)
            def _update_limits(rid: str, x: float, y: float, c: float) -> None:
                try:
                    with db_session() as db:
                        rack = db.query(Rack).filter_by(id=rid).first()
                        if rack:
                            rack.limit_x_mm = x
                            rack.limit_y_mm = y
                            rack.limit_c_mm = c
                except Exception:
                    logger.exception("_update_limits failed for rack=%s", rid)
            import threading as _thr2
            _thr2.Thread(
                target=_update_limits, args=(rack_id, lx, ly, lc),
                daemon=True, name=f"limits-persist-{rack_id}",
            ).start()

    # ── M705 ROWS/COLS response handler ───────────────────────────────────
    if raw.startswith("ROWS"):
        import re as _re
        m_rc = _re.search(r'ROWS=(\d+)\s+COLS=(\d+)', raw, _re.IGNORECASE)
        if m_rc:
            rows, cols = int(m_rc.group(1)), int(m_rc.group(2))
            gantry_state.upsert(rack_id, grid_rows=rows, grid_cols=cols)
            from services.layout_cache import layout_cache as _lc
            _lc.set(rack_id, "M705", raw)
            logger.info("M705 ROWS/COLS received rack=%s: rows=%d cols=%d", rack_id, rows, cols)

    # ── M706 Pitch response handler ───────────────────────────────────────
    if raw.startswith("Pitch"):
        import re as _re
        m_pitch = _re.search(r'Pitch\s+X=([-\d.]+)\s+Y=([-\d.]+)', raw, _re.IGNORECASE)
        if m_pitch:
            px, py = float(m_pitch.group(1)), float(m_pitch.group(2))
            gantry_state.upsert(rack_id, pitch_x_mm=px, pitch_y_mm=py)
            from services.layout_cache import layout_cache as _lc
            _lc.set(rack_id, "M706", raw)
            logger.info("M706 Pitch received rack=%s: X=%.2f Y=%.2f", rack_id, px, py)

    # ── M707 Offsets response handler ─────────────────────────────────────
    if raw.startswith("Offsets"):
        import re as _re
        m_off = _re.search(r'Offsets\s+X0=([-\d.]+)\s+Y0=([-\d.]+)', raw, _re.IGNORECASE)
        if m_off:
            x0, y0 = float(m_off.group(1)), float(m_off.group(2))
            gantry_state.upsert(rack_id, x0_offset_mm=x0, y0_offset_mm=y0)
            from services.layout_cache import layout_cache as _lc
            _lc.set(rack_id, "M707", raw)
            logger.info("M707 Offsets received rack=%s: X0=%.2f Y0=%.2f", rack_id, x0, y0)

    # ── CAPTURE_STARTED ───────────────────────────────────────────────────
    if raw.startswith("CAPTURE_STARTED"):
        logger.info("CAPTURE_STARTED received for rack=%s — extending capture lock", rack_id)
        with db_session() as db:
            extended = extend_lock(
                rack_id=rack_id,
                additional_seconds=settings.CAPTURE_LOCK_TIMEOUT_S,
                db=db,
            )
        if not extended:
            logger.warning(
                "CAPTURE_STARTED: no active lock found for rack=%s — lock may have expired",
                rack_id,
            )

    # ── CAPTURE_DONE ──────────────────────────────────────────────────────
    elif raw.startswith("CAPTURE_DONE"):
        logger.info("CAPTURE_DONE received for rack=%s", rack_id)
        state = gantry_state.get(rack_id)
        if state and state.lock_type == "capture":
            with db_session() as db:
                release_lock(rack_id=rack_id, db=db)
            logger.info("Capture lock released on CAPTURE_DONE for rack=%s", rack_id)

    # ── CAPTURE_ERROR (FIX Mismatch 7) ───────────────────────────────────
    # Pi publishes "CAPTURE_ERROR:{exception}" when the photo fails.
    # Without this handler the capture lock was never released, permanently
    # locking the rack until the 2-second sweep task timed it out.
    elif raw.startswith("CAPTURE_ERROR"):
        error_detail = raw[len("CAPTURE_ERROR:"):].strip() if ":" in raw else "unknown"
        logger.error(
            "CAPTURE_ERROR received for rack=%s: %s — releasing capture lock",
            rack_id, error_detail,
        )
        state = gantry_state.get(rack_id)
        if state and state.lock_type == "capture":
            with db_session() as db:
                release_lock(rack_id=rack_id, db=db)
            logger.info(
                "Capture lock released after CAPTURE_ERROR for rack=%s", rack_id
            )
        # Audit the failure so operators can diagnose it
        try:
            with db_session() as db:
                db.add(AuditLog(
                    event_type="capture_error",
                    rack_id=rack_id,
                    outcome="failure",
                    details=json.dumps({"error": error_detail}),
                    created_at=datetime.utcnow(),
                ))
        except Exception:
            logger.exception(
                "CAPTURE_ERROR audit write failed for rack=%s", rack_id
            )

    # ── SCAN_KEEPALIVE (FIX Mismatch 8) ──────────────────────────────────
    # Pi's scan_executor publishes "SCAN_KEEPALIVE:{rack_id}:{iso_timestamp}"
    # every 30 s so the server resets the scan lock expiry.  Without this
    # handler the scan lock expired on its own timeout and the lock sweep
    # released it mid-scan, allowing other commands to steal the lock while
    # the scan was still physically running on the Arduino.
    elif raw.startswith("SCAN_KEEPALIVE"):
        logger.debug("SCAN_KEEPALIVE received for rack=%s — extending scan lock", rack_id)
        with db_session() as db:
            extended = extend_lock(
                rack_id=rack_id,
                additional_seconds=settings.SCAN_LOCK_TIMEOUT_S,
                db=db,
            )
        if extended:
            logger.debug(
                "Scan lock extended by %ds for rack=%s",
                settings.SCAN_LOCK_TIMEOUT_S, rack_id,
            )
        else:
            logger.warning(
                "SCAN_KEEPALIVE: no active scan lock found for rack=%s — "
                "lock may have already expired",
                rack_id,
            )


def _on_image_message(
    rack_id: Optional[str], subtopic: str, payload: Any
) -> None:
    """
    Handler for vivarium/rack/{id}/image messages (Section 4.3 / 5.4 / 9).

    Flow:
      1. Parse payload — expect {local_path|s3_key, sha256_checksum,
         capture_timestamp, rack_id, [cell_row, cell_col]}.
      2. Validate local_path (or s3_key) via s3_handler.validate_image_path().
      3. Consume capture_attribution to get operator_id.
      4. Write an image_records row, handling UNIQUE duplicates.
      5. Release the capture lock.
      6. Send capture_complete to the lock holder's WebSocket only.

    FIX (Mismatch 4): capture_complete now includes cell_row, cell_col,
    and capture_timestamp in the data payload, matching WsMsgCaptureComplete.
    """
    if rack_id is None:
        return

    # ── 1. Parse payload ──────────────────────────────────────────────────
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            logger.error("image message: invalid JSON payload for rack=%s: %r", rack_id, payload)
            return

    if not isinstance(payload, dict):
        logger.error("image message: expected dict payload for rack=%s, got %r", rack_id, payload)
        return

    local_path: Optional[str] = payload.get("local_path")
    s3_key: Optional[str]     = payload.get("s3_key")
    sha256: str               = payload.get("sha256_checksum", "")
    ts_str: str               = payload.get("capture_timestamp", "")
    cell_row: Optional[int]   = payload.get("cell_row")
    cell_col: Optional[int]   = payload.get("cell_col")

    # ── 2. Validate path (Section 9 Layer 2A) ────────────────────────────
    try:
        validate_image_path(
            rack_id=rack_id,
            local_path=local_path,
            s3_key=s3_key,
        )
    except ImagePathError as exc:
        logger.warning(
            "image message validation_failure for rack=%s: %s", rack_id, exc
        )
        with db_session() as db:
            db.add(AuditLog(
                event_type="validation_failure",
                rack_id=rack_id,
                outcome="failure",
                details=json.dumps({
                    "reason": str(exc),
                    "local_path": local_path,
                    "s3_key": s3_key,
                }),
                created_at=datetime.utcnow(),
            ))
        return

    # ── 3. Consume capture attribution ────────────────────────────────────
    operator_id: Optional[str] = cache.consume_capture_attribution(rack_id)
    attribution_expired = False
    if operator_id is None:
        logger.warning(
            "image message: capture attribution expired or missing for rack=%s — "
            "image_records.triggered_by_operator will be null.",
            rack_id,
        )
        attribution_expired = True

    # ── 4. Parse capture_timestamp ────────────────────────────────────────
    try:
        capture_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        capture_ts = datetime.utcnow()
        logger.warning(
            "image message: could not parse capture_timestamp %r for rack=%s, using now",
            ts_str, rack_id,
        )

    # ── 5. Write image_records row ────────────────────────────────────────
    trigger_type = "auto_scan" if (cell_row is not None and cell_col is not None) else "manual"

    image_record_id: Optional[int] = None
    duplicate = False

    try:
        with db_session() as db:
            record = ImageRecord(
                rack_id=rack_id,
                s3_key=s3_key,
                local_path=local_path,
                sha256_checksum=sha256,
                triggered_by_operator=operator_id,
                trigger_type=trigger_type,
                cell_row=cell_row,
                cell_col=cell_col,
                capture_timestamp=capture_ts,
                created_at=datetime.utcnow(),
            )
            db.add(record)
            db.flush()
            image_record_id = record.id

            db.add(AuditLog(
                event_type="image_notification_received",
                rack_id=rack_id,
                user_id=operator_id,
                outcome="success",
                details=json.dumps({
                    "local_path": local_path,
                    "s3_key": s3_key,
                    "sha256": sha256,
                    "trigger_type": trigger_type,
                    "attribution_expired": attribution_expired,
                }),
                created_at=datetime.utcnow(),
            ))

    except IntegrityError:
        duplicate = True
        logger.warning(
            "image message: duplicate notification for rack=%s path=%s — "
            "UNIQUE constraint prevented double insert.",
            rack_id, local_path or s3_key,
        )
        with db_session() as db:
            db.add(AuditLog(
                event_type="duplicate_image_notification",
                rack_id=rack_id,
                outcome="flagged",
                details=json.dumps({
                    "local_path": local_path,
                    "s3_key": s3_key,
                }),
                created_at=datetime.utcnow(),
            ))

    if attribution_expired and not duplicate:
        with db_session() as db:
            db.add(AuditLog(
                event_type="validation_failure",
                rack_id=rack_id,
                outcome="flagged",
                details=json.dumps({
                    "reason": "capture_attribution_expired",
                    "local_path": local_path,
                    "s3_key": s3_key,
                }),
                created_at=datetime.utcnow(),
            ))

    # ── 6. Release the capture lock ───────────────────────────────────────
    if not duplicate:
        state = gantry_state.get(rack_id)
        if state and state.lock_type == "capture":
            with db_session() as db:
                release_lock(rack_id=rack_id, db=db)
            logger.info("Capture lock released on image arrival for rack=%s", rack_id)

    # ── 7. Send capture_complete to the lock-holder's WebSocket only ──────
    # FIX (Mismatch 4): include cell_row, cell_col, capture_timestamp so the
    # frontend WsMsgCaptureComplete.data fields are fully populated.
    if not duplicate:
        ws_registry.broadcast_from_thread(
            rack_id,
            {
                "type": "capture_complete",
                "rack_id": rack_id,
                "data": {
                    "local_path": local_path,
                    "s3_key": s3_key,
                    "sha256": sha256,
                    # FIX (Mismatch 4): these three were missing from the payload
                    "cell_row": cell_row,
                    "cell_col": cell_col,
                    "capture_timestamp": ts_str,  # ISO string as the Pi sent it
                },
            },
            lock_holder_user_id=operator_id,
            lock_holder_only=True,
        )
        logger.info(
            "capture_complete sent to operator=%s for rack=%s",
            operator_id, rack_id,
        )


# ===========================================================================
# Stage 12 — MQTT status handler (Section 3.1 / 5.2)
# ===========================================================================

def _on_status_message(
    rack_id: Optional[str], subtopic: str, payload: Any
) -> None:
    """
    Handler for vivarium/rack/{id}/status messages.

    Handles two scenarios:
      • Heartbeat  — payload: {"status": "online", "camera_status": "online", ...}
      • Last Will  — payload: {"status": "offline", "reason": "unexpected_disconnect"}
    """
    if rack_id is None:
        return

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("status message: invalid JSON for rack=%s: %r", rack_id, payload)
            return

    if not isinstance(payload, dict):
        logger.warning("status message: unexpected payload type for rack=%s: %r", rack_id, payload)
        return

    new_status: str = payload.get("status", "offline")
    camera_status: str = payload.get("camera_status", "unknown")

    logger.info(
        "rack=%s status message received: mqtt_status=%s camera_status=%s",
        rack_id, new_status, camera_status,
    )

    gantry_state.upsert(
        rack_id,
        mqtt_status=new_status,
        camera_status=camera_status,
    )

    def _persist() -> None:
        try:
            with db_session() as db:
                rack = db.query(Rack).filter_by(id=rack_id).first()
                if rack is None:
                    logger.debug(
                        "status message: rack=%s not found in DB — skipping persist",
                        rack_id,
                    )
                    return
                rack.mqtt_status = new_status
                rack.camera_status = camera_status
        except Exception:
            logger.exception(
                "status message: DB persist failed for rack=%s", rack_id
            )

    import threading as _threading
    _threading.Thread(
        target=_persist,
        daemon=True,
        name=f"status-persist-{rack_id}",
    ).start()


# ===========================================================================
# Lifespan — startup and shutdown
# ===========================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ──────────────────────────────────────────────────────────────

    logger.info("Creating database tables …")
    create_tables()

    logger.info("Seeding gantry state from DB …")
    with db_session() as db:
        racks = db.query(Rack).all()
        gantry_state.reconcile_from_db(racks)
    logger.info("Seeded %d rack(s) into gantry state.", len(gantry_state.rack_ids()))

    try:
        mqtt_client.connect(timeout_s=5.0)
        from core.queue_manager import configure_publish
        configure_publish(mqtt_client.publish_command)
        mqtt_client.register_handler("*", relay_mqtt_to_ws)
        mqtt_client.register_handler("response",      _on_response_message)
        mqtt_client.register_handler("image",         _on_image_message)
        mqtt_client.register_handler("scan_progress", scan_engine.on_scan_progress)
        mqtt_client.register_handler("scan_status",   scan_engine.on_scan_status)
        mqtt_client.register_handler("status",        _on_status_message)
        logger.info("MQTT client connected and handlers registered.")
    except (RuntimeError, OSError) as exc:
        logger.warning("MQTT connect failed — server will run without MQTT: %s", exc)

    ws_registry.set_loop(asyncio.get_event_loop())

    start_lock_sweep_task()
    logger.info("Lock sweep task started.")

    scan_engine.start()
    logger.info("Scan engine started.")

    logger.info(
        "Vivarium Gantry Server ready — http://%s:%d",
        settings.BACKEND_HOST,
        settings.BACKEND_PORT,
    )

    yield  # ← server is running

    # ── SHUTDOWN ─────────────────────────────────────────────────────────────
    logger.info("Shutting down …")
    scan_engine.stop()
    if mqtt_client.is_connected:
        mqtt_client.disconnect()
    logger.info("Shutdown complete.")


# ===========================================================================
# Application factory
# ===========================================================================

def create_app() -> FastAPI:
    app = FastAPI(
        title="Vivarium Gantry System API",
        version="0.6.1",
        description=(
            "Central control server for the Vivarium Gantry System. "
            "See the implementation plan for full architecture details."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.include_router(http_router)
    app.include_router(ws_router)

    return app


# Module-level app instance — used by uvicorn and tests
app = create_app()


# ===========================================================================
# Dev entrypoint
# ===========================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.BACKEND_HOST,
        port=settings.BACKEND_PORT,
        reload=True,
        log_level="info",
    )
