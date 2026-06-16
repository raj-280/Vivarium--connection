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

APScheduler and pending-command sweep will be added in Stage 12 (auto-scan).
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
from middleware.csrf import CSRFMiddleware
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

    Relevant for Stage 9:
      CAPTURE_STARTED — reset lock_expires_at to CAPTURE_LOCK_TIMEOUT_S from now
                         (lock-keepalive, Section 4.3). Prevents the lock from
                         expiring during a slow save or future S3 upload.
      CAPTURE_DONE    — release the capture lock immediately so the next queued
                         command (if any) can run.

    All other response messages are passed through to the relay unchanged.
    The catch-all relay handler (relay_mqtt_to_ws) also runs for every message
    because it is registered with subtopic="*"; we only need side-effects here.
    """
    if rack_id is None:
        return

    # Normalise payload to a string for prefix matching
    raw = payload if isinstance(payload, str) else json.dumps(payload)

    # ── M114 position verification (Section 4.4) ──────────────────────────
    # position_monitor.on_m114 parses the line, updates gantry_state + DB,
    # and triggers recovery if tolerance or homing checks fail.
    # Require both 'X:' and 'homed:' to avoid false positives from other
    # messages that happen to contain the word "homed".
    if "X:" in raw and "homed:" in raw:
        position_monitor.on_m114(rack_id, raw)

    # ── Auto-M114 follow-up after motion command ACK (Section 4.4) ────────
    # When the Pi ACKs a motion command (COMMAND_ACK:M700 / COMMAND_ACK:G28
    # etc.) the server publishes M114 so position_monitor can record where the
    # gantry actually stopped.  Without this, last_position_x/y stay None
    # for standalone manual moves (the scan executor does its own M114 inline).
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
    # Pi publishes this after every reconnect-cleanup sequence.
    # Signal the position_monitor so the next M114 runs a stale-homing check.
    if raw.startswith("BRIDGE_RECONNECTED"):
        logger.info("BRIDGE_RECONNECTED received for rack=%s — marking stale-homing check due.", rack_id)
        position_monitor.mark_stale_check_due(rack_id)

    # ── LAYOUT_CONFIG (Item 1 / Section 5.2 step 5) ───────────────────────
    # Pi publishes this JSON blob on every reconnect after querying M705/M706/
    # M707/M799. The raw string starts with '{' because publish_response sends
    # it as json.dumps(payload). We parse it here and update gantry_state +
    # the DB in a background thread.
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
    # Handles standalone M799 responses: "LIMITS X=300.00 Y=200.00 C=180.00"
    # These arrive when the Pi sends a direct M799 command (e.g. from the
    # POST /rack/{rack_id}/query-limits endpoint or from a manual M799 command).
    # LAYOUT_CONFIG already handles limits if M799 is sent during reconnect, but
    # direct M799 commands bypass that path.
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
    # Handles "ROWS=12 COLS=7" from a direct M705 command.
    # Stores in layout_cache for GET /rack/{rack_id}/layout?live=true.
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
    # Handles "Pitch X=50.0 Y=50.0" from a direct M706 command.
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
    # Handles "Offsets X0=0.0 Y0=0.0" from a direct M707 command.
    if raw.startswith("Offsets"):
        import re as _re
        m_off = _re.search(r'Offsets\s+X0=([-\d.]+)\s+Y0=([-\d.]+)', raw, _re.IGNORECASE)
        if m_off:
            x0, y0 = float(m_off.group(1)), float(m_off.group(2))
            gantry_state.upsert(rack_id, x0_offset_mm=x0, y0_offset_mm=y0)
            from services.layout_cache import layout_cache as _lc
            _lc.set(rack_id, "M707", raw)
            logger.info("M707 Offsets received rack=%s: X0=%.2f Y0=%.2f", rack_id, x0, y0)

    if raw.startswith("CAPTURE_STARTED"):


        # Keepalive: reset the capture lock expiry (Section 4.3)
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

    elif raw.startswith("CAPTURE_DONE"):
        # Capture cycle finished — lock released by the image handler when the
        # /image MQTT message arrives.  CAPTURE_DONE is a belt-and-suspenders
        # release in case the /image message was lost or delayed.
        logger.info("CAPTURE_DONE received for rack=%s", rack_id)
        # The authoritative release is in _on_image_message below; only release
        # here if there is still a capture-type lock (avoids releasing a scan
        # lock that was acquired after the capture).
        state = gantry_state.get(rack_id)
        if state and state.lock_type == "capture":
            with db_session() as db:
                release_lock(rack_id=rack_id, db=db)
            logger.info("Capture lock released on CAPTURE_DONE for rack=%s", rack_id)


def _on_image_message(
    rack_id: Optional[str], subtopic: str, payload: Any
) -> None:
    """
    Handler for vivarium/rack/{id}/image messages (Section 4.3 / 5.4 / 9).

    Flow:
      1. Parse payload — expect {local_path|s3_key, sha256_checksum,
         capture_timestamp, rack_id, [cell_row, cell_col]}.
      2. Validate local_path (or s3_key) via s3_handler.validate_image_path()
         (Section 9 Layer 2A: pattern + rack-id cross-check).
      3. Consume capture_attribution to get operator_id (may be None if expired).
      4. Write an image_records row, catching UNIQUE constraint violations
         (duplicate MQTT notifications) and logging duplicate_image_notification.
      5. Release the capture lock (the Pi has finished).
      6. Send capture_complete to the lock holder's WebSocket only
         (Section 4.3 / 4.2 routing rules).
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

    # ── 3. Consume capture attribution (Section 3.12 / 4.3) ──────────────
    # consume_capture_attribution() reads + deletes atomically.
    # Returns None if attribution expired — image_records.triggered_by_operator
    # is written as null and a validation_failure audit row is emitted.
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

    # ── 5. Write image_records row; handle UNIQUE constraint duplicate ────
    # trigger_type: 'manual' if cell_row/col absent, 'auto_scan' if present.
    trigger_type = "auto_scan" if (cell_row is not None and cell_col is not None) else "manual"

    image_record_id: Optional[int] = None
    duplicate = False

    try:
        with db_session() as db:
            record = ImageRecord(
                rack_id=rack_id,
                s3_key=s3_key,              # None when S3_ENABLED=false
                local_path=local_path,      # populated for S3_ENABLED=false
                sha256_checksum=sha256,
                triggered_by_operator=operator_id,
                trigger_type=trigger_type,
                cell_row=cell_row,
                cell_col=cell_col,
                capture_timestamp=capture_ts,
                created_at=datetime.utcnow(),
            )
            db.add(record)
            db.flush()  # flush to get the autoincrement id before commit
            image_record_id = record.id

            # Audit: image received
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
        # UNIQUE constraint on s3_key/local_path fired — duplicate MQTT notification
        # (Section 3.4 / 9 Layer 2C)
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
        # Emit a separate validation_failure audit entry for the attribution expiry
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

    # ── 6. Release the capture lock (Section 4.3) ─────────────────────────
    # Only release if this rack holds a capture-type lock; guards against
    # releasing a scan lock that was acquired after the capture completed.
    if not duplicate:
        state = gantry_state.get(rack_id)
        if state and state.lock_type == "capture":
            with db_session() as db:
                release_lock(rack_id=rack_id, db=db)
            logger.info("Capture lock released on image arrival for rack=%s", rack_id)

    # ── 7. Send capture_complete to the lock-holder’s WebSocket only ──────
    # Section 4.3: "capture_complete is delivered over /ws ONLY to the WebSocket
    # connection belonging to the lock_holder_user_id recorded at the time the
    # capture was triggered — never broadcast."
    # We read lock_holder from the operator_id we consumed (pre-release), not
    # from the current state (which was just cleared above).
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
                    "image_record_id": image_record_id,
                    "trigger_type": trigger_type,
                    "attribution_expired": attribution_expired,
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

    Updates both the in-memory GantryState mirror and the racks DB row so that:
      - The scan engine can gate auto-scans to online racks (Section 4.7).
      - The frontend can display correct online/offline indicators.
      - Scenario 4 (Last Will) and Scenario 5 (Reconnect) integration tests pass.
    """
    if rack_id is None:
        return

    # Normalise payload to dict
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

    # Update the in-memory state mirror immediately (no DB round-trip)
    gantry_state.upsert(
        rack_id,
        mqtt_status=new_status,
        camera_status=camera_status,
    )

    # Persist to the DB in a background thread so the MQTT loop is not blocked
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

    # 1. Create all DB tables (idempotent — skips existing)
    logger.info("Creating database tables …")
    create_tables()

    # 2. Seed in-memory gantry state from DB
    logger.info("Seeding gantry state from DB …")
    with db_session() as db:
        racks = db.query(Rack).all()
        gantry_state.reconcile_from_db(racks)
    logger.info("Seeded %d rack(s) into gantry state.", len(gantry_state.rack_ids()))

    # 3. Connect to MQTT broker and register handlers
    try:
        mqtt_client.connect(timeout_s=5.0)
        # Wire the MQTT publish function into the queue manager (Section 4.3)
        from core.queue_manager import configure_publish
        configure_publish(mqtt_client.publish_command)
        # Catch-all handler: routes all MQTT subtopics → WebSocket subscribers
        mqtt_client.register_handler("*", relay_mqtt_to_ws)
        # Specific handlers for the capture flow (Stage 9)
        mqtt_client.register_handler("response",      _on_response_message)
        mqtt_client.register_handler("image",         _on_image_message)
        # Stage 11: auto-scan progress and status
        mqtt_client.register_handler("scan_progress", scan_engine.on_scan_progress)
        mqtt_client.register_handler("scan_status",   scan_engine.on_scan_status)
        # Stage 12: Pi heartbeat + Last Will → updates mqtt_status / camera_status
        mqtt_client.register_handler("status",        _on_status_message)
        logger.info("MQTT client connected and handlers registered.")
    except (RuntimeError, OSError) as exc:
        # Server starts even if MQTT is unavailable (local dev without broker)
        logger.warning("MQTT connect failed — server will run without MQTT: %s", exc)

    # 4. Give the WebSocket registry the running event loop so MQTT callbacks
    #    can schedule coroutines from their background thread.
    ws_registry.set_loop(asyncio.get_event_loop())

    # 5. Start background lock sweep task (every 2 s)
    start_lock_sweep_task()
    logger.info("Lock sweep task started.")

    # 6. Start scan engine (APScheduler — 1-minute auto-scan job) — Stage 11
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
    scan_engine.stop()          # stop APScheduler before MQTT so no jobs fire mid-shutdown
    if mqtt_client.is_connected:
        mqtt_client.disconnect()
    logger.info("Shutdown complete.")


# ===========================================================================
# Application factory
# ===========================================================================

def create_app() -> FastAPI:
    app = FastAPI(
        title="Vivarium Gantry System API",
        version="0.6.0",         # bumped at Stage 11 (scan engine)
        description=(
            "Central control server for the Vivarium Gantry System. "
            "See the implementation plan for full architecture details."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── Middleware (outermost first) ─────────────────────────────────────────

    # CORS — allow configured frontend origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # CSRF — double-submit cookie (no-op locally when CSRF_ENABLED=False)
    app.add_middleware(CSRFMiddleware)

    # slowapi rate limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Routers ──────────────────────────────────────────────────────────────
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
