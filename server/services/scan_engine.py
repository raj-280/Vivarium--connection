"""
server/services/scan_engine.py

APScheduler-based auto-scan engine — Section 4.7.

Responsibilities
────────────────
• APScheduler job fires every minute, queries scan_schedule for rows where
  next_scan_at has passed and enabled=True.
• Two gates before firing SCAN_START (Section 4.7):
    a. Pi heartbeat freshness  — last status message within 60s (pi_online).
    b. Rack not currently locked by an operator.
  If either gate fails, the scan is postponed by SCAN_POSTPONE_MINUTES
  (never skipped entirely, only delayed).
• Bandwidth staggering: when many racks share a scan window,
  SCAN_STAGGER_GROUP_SIZE and SCAN_STAGGER_DELAY_MINUTES batch them with
  offset start times.
• On gate pass: publish SCAN_START (with scan_session_id, grid info, and
  optional resume_row/resume_col), create a scan_sessions row, update
  scan_schedule.last_scan_started_at and next_scan_at.
• MQTT handlers (registered in main.py):
    - scan_progress → update scan_sessions.cells_completed/failed,
                      update racks.scan_state, write image_records rows
                      (trigger_type=auto_scan, scan_session_id, cell_row, cell_col).
    - scan_status   → status transitions (paused / complete / aborted),
                      trigger resume/restart prompt over /ws after pause.

Manual-vs-scan conflict (Section 4.8)
───────────────────────────────────────
• When a manual command arrives while racks.scan_state=running:
    1. Set racks.scan_state = "paused" immediately.
    2. Publish SCAN_STOP to the Pi (Pi finishes current cell, then stops).
    3. On scan_status=paused: offer resume/restart prompt over /ws with
       a MANUAL_VS_SCAN_RESUME_WINDOW_S timeout.
    4. On operator choice:
         "resume"  → re-publish SCAN_START with resume_row/resume_col.
         "restart" → re-publish SCAN_START with no resume fields.
         timeout   → restart on the next scheduled interval.

Emergency stop integration (Section 4.8)
──────────────────────────────────────────
• When scan_status=aborted arrives with abort_reason=emergency_stop:
    1. Mark scan_sessions.status = "aborted", scan_sessions.abort_reason.
    2. Mark racks.scan_state = "aborted".
    3. Release the scan lock.
    4. Do NOT re-schedule; a G28 is required before any further motion
       (enforced by position_monitor's stale-homing check on next M114).

FIXES applied in this version
──────────────────────────────
• Mismatch 1 (server side): on_scan_progress now reads cell_row/cell_col
  (not row/col) from the Pi payload, matching the renamed Pi keys and the
  frontend WsMsgScanCellComplete type.
• Mismatch 3: _offer_resume_restart now sends expires_at (ISO timestamp
  string) instead of timeout_s (raw integer seconds), matching the frontend
  WsMsgScanResumePrompt.data.expires_at field.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
from core.state import gantry_state

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Scan lock holder for server-initiated scans (no human operator)
_SCAN_SYSTEM_USER_ID = "_scan_engine"


# ── ScanEngine ────────────────────────────────────────────────────────────────

class ScanEngine:
    """
    Singleton APScheduler wrapper for the auto-scan job.

    Usage (main.py lifespan):
        from services.scan_engine import scan_engine
        scan_engine.start()          # in startup
        scan_engine.stop()           # in shutdown

    MQTT handlers are registered externally in main.py:
        mqtt_client.register_handler("scan_progress", scan_engine.on_scan_progress)
        mqtt_client.register_handler("scan_status",   scan_engine.on_scan_status)
    """

    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1},
            timezone="UTC",
        )
        self._lock = threading.Lock()
        # rack_id → scan_session_id of the currently active/paused scan
        self._active_sessions: dict[str, int] = {}
        # rack_id → threading.Timer for the resume/restart window (Section 4.8)
        self._resume_timers: dict[str, threading.Timer] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the APScheduler; add the 1-minute scan job."""
        self._scheduler.add_job(
            self._check_due_scans,
            trigger=IntervalTrigger(minutes=1),
            id="scan_engine_check",
            replace_existing=True,
        )
        self._scheduler.start()
        logger.info("ScanEngine started (job: every 1 minute).")

    def stop(self) -> None:
        """Graceful shutdown."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("ScanEngine stopped.")

    # ── Scheduler job ─────────────────────────────────────────────────────────

    def _check_due_scans(self) -> None:
        """
        Called every minute by APScheduler.
        Queries scan_schedule for due rows; applies staggering; fires SCAN_START.
        """
        try:
            from db.database import db_session
            from db.models import Rack, ScanSchedule

            now = datetime.utcnow()
            with db_session() as db:
                due_rows = (
                    db.query(ScanSchedule)
                    .join(Rack, ScanSchedule.rack_id == Rack.id)
                    .filter(
                        ScanSchedule.enabled == True,
                        ScanSchedule.next_scan_at <= now,
                        Rack.maintenance_required == False,
                    )
                    .all()
                )
                due_rack_ids = [r.rack_id for r in due_rows]

            if not due_rack_ids:
                return

            logger.info(
                "ScanEngine: %d rack(s) due for scan: %s",
                len(due_rack_ids), due_rack_ids,
            )

            # Apply stagger grouping (Section 4.7)
            stagger_size = settings.SCAN_STAGGER_GROUP_SIZE
            stagger_delay = settings.SCAN_STAGGER_DELAY_MINUTES

            for batch_idx, batch_start in enumerate(range(0, len(due_rack_ids), stagger_size)):
                batch = due_rack_ids[batch_start: batch_start + stagger_size]
                delay_minutes = batch_idx * stagger_delay
                if delay_minutes == 0:
                    for rack_id in batch:
                        self._try_fire_scan(rack_id)
                else:
                    for rack_id in batch:
                        t = threading.Timer(
                            delay_minutes * 60,
                            self._try_fire_scan,
                            args=(rack_id,),
                        )
                        t.daemon = True
                        t.start()
                        logger.debug(
                            "ScanEngine: rack=%s staggered %d min",
                            rack_id, delay_minutes,
                        )

        except Exception:
            logger.exception("ScanEngine._check_due_scans failed")

    def _try_fire_scan(self, rack_id: str) -> None:
        """
        Check the two gates for rack_id; fire or postpone.
        Gate a: Pi online (mqtt_status == "online").
        Gate b: Rack not currently locked.
        """
        try:
            from db.database import db_session
            from db.models import Rack, ScanSchedule, ScanSession
            from core.locking import LockType, LockResult, acquire_lock

            now = datetime.utcnow()

            # ── Gate a: Pi online ──────────────────────────────────────────
            state = gantry_state.get(rack_id)
            pi_alive = state is not None and state.mqtt_status == "online"
            if not pi_alive:
                logger.info(
                    "ScanEngine: rack=%s Pi not online — postponing %d min",
                    rack_id, settings.SCAN_POSTPONE_MINUTES,
                )
                self._postpone(rack_id, settings.SCAN_POSTPONE_MINUTES)
                return

            # ── Gate b: Rack not locked ────────────────────────────────────
            with db_session() as db:
                rack = db.query(Rack).filter_by(id=rack_id).first()
                if rack is None:
                    logger.warning("ScanEngine: rack=%s not in DB — skipping", rack_id)
                    return

                already_locked = (
                    rack.lock_holder_user_id is not None
                    and rack.lock_expires_at is not None
                    and rack.lock_expires_at > now
                )
                if already_locked:
                    logger.info(
                        "ScanEngine: rack=%s locked by %s — postponing %d min",
                        rack_id, rack.lock_holder_user_id, settings.SCAN_POSTPONE_MINUTES,
                    )
                    self._postpone(rack_id, settings.SCAN_POSTPONE_MINUTES)
                    return

                # ── Both gates passed — fire the scan ─────────────────────
                lock_result = acquire_lock(
                    rack_id=rack_id,
                    user_id=_SCAN_SYSTEM_USER_ID,
                    lock_type=LockType.SCAN,
                    db=db,
                )
                if lock_result != LockResult.ACQUIRED:
                    logger.warning(
                        "ScanEngine: could not acquire scan lock for rack=%s (%s) — postponing",
                        rack_id, lock_result,
                    )
                    self._postpone(rack_id, settings.SCAN_POSTPONE_MINUTES)
                    return

                # Create scan_sessions row
                session = ScanSession(
                    rack_id=rack_id,
                    status="running",
                    started_at=now,
                    cells_total=rack.grid_rows * rack.grid_cols,
                    cells_completed=0,
                    cells_failed=0,
                )
                db.add(session)
                db.flush()  # get autoincrement id
                session_id = session.id

                # Update scan_schedule
                schedule = db.query(ScanSchedule).filter_by(rack_id=rack_id).first()
                if schedule:
                    schedule.last_scan_started_at = now
                    schedule.next_scan_at = now + timedelta(hours=schedule.interval_hours)

                # Update rack scan_state
                rack.scan_state = "running"

                # Snapshot grid dims before session closes
                grid_rows = rack.grid_rows
                grid_cols = rack.grid_cols

            with self._lock:
                self._active_sessions[rack_id] = session_id

            # Publish SCAN_START to the Pi
            self._publish_scan_start(rack_id, session_id, grid_rows, grid_cols)
            logger.info(
                "ScanEngine: SCAN_START published for rack=%s session=%d grid=%dx%d",
                rack_id, session_id, grid_rows, grid_cols,
            )

        except Exception:
            logger.exception("ScanEngine._try_fire_scan failed for rack=%s", rack_id)

    # ── MQTT handlers (registered in main.py) ─────────────────────────────────

    def on_scan_progress(
        self, rack_id: Optional[str], subtopic: str, payload: Any
    ) -> None:
        """
        Handler for vivarium/rack/{id}/scan_progress.
        Updates scan_sessions and broadcasts scan_cell_complete to all
        WebSocket subscribers of the rack.

        FIX (Mismatch 1 server side): reads cell_row/cell_col (not row/col)
        from the Pi payload. The Pi's scan_executor now sends cell_row/cell_col
        and the frontend WsMsgScanCellComplete.data uses the same keys.
        """
        if rack_id is None:
            return
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("scan_progress: invalid JSON for rack=%s", rack_id)
                return
        if not isinstance(payload, dict):
            return

        session_id: Optional[int] = payload.get("scan_session_id")
        # FIX (Mismatch 1): use cell_row/cell_col, not row/col
        row: Optional[int] = payload.get("cell_row")
        col: Optional[int] = payload.get("cell_col")
        cells_completed: int = int(payload.get("cells_completed", 0))
        cells_failed: int = int(payload.get("cells_failed", 0))

        try:
            from db.database import db_session
            from db.models import ScanSession, Rack

            with db_session() as db:
                if session_id:
                    session = db.query(ScanSession).filter_by(id=session_id).first()
                    if session:
                        session.cells_completed = cells_completed
                        session.cells_failed = cells_failed
                        session.last_completed_row = row
                        session.last_completed_col = col
                rack = db.query(Rack).filter_by(id=rack_id).first()
                if rack:
                    rack.scan_state = "running"

        except Exception:
            logger.exception("on_scan_progress DB error for rack=%s", rack_id)

        # Broadcast scan_cell_complete to all rack subscribers (Section 4.3)
        # The payload already has cell_row/cell_col so the frontend reads them correctly.
        try:
            from api.websocket import ws_registry
            ws_registry.broadcast_from_thread(
                rack_id,
                {"type": "scan_cell_complete", "rack_id": rack_id, "data": payload},
            )
        except Exception:
            logger.exception("on_scan_progress WS broadcast failed for rack=%s", rack_id)

    def on_scan_status(
        self, rack_id: Optional[str], subtopic: str, payload: Any
    ) -> None:
        """
        Handler for vivarium/rack/{id}/scan_status.
        Drives status transitions: complete / paused / aborted.
        """
        if rack_id is None:
            return
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return
        if not isinstance(payload, dict):
            return

        status: str = payload.get("status", "")
        session_id: Optional[int] = payload.get("scan_session_id")
        abort_reason: Optional[str] = payload.get("abort_reason")
        last_row: Optional[int] = payload.get("last_completed_row")
        last_col: Optional[int] = payload.get("last_completed_col")

        logger.info(
            "ScanEngine: scan_status rack=%s status=%s session=%s",
            rack_id, status, session_id,
        )

        grid_rows = settings.RACK_ROWS
        grid_cols = settings.RACK_COLS

        try:
            from db.database import db_session
            from db.models import ScanSession, Rack
            from core.locking import release_lock

            with db_session() as db:
                session = (
                    db.query(ScanSession).filter_by(id=session_id).first()
                    if session_id else None
                )
                rack = db.query(Rack).filter_by(id=rack_id).first()
                if rack:
                    grid_rows = rack.grid_rows
                    grid_cols = rack.grid_cols

                if status == "complete":
                    if session:
                        session.status = "complete"
                        session.completed_at = datetime.utcnow()
                    if rack:
                        rack.scan_state = "complete"
                    release_lock(rack_id=rack_id, db=db)
                    with self._lock:
                        self._active_sessions.pop(rack_id, None)

                elif status == "paused":
                    if session:
                        session.status = "paused"
                        session.last_completed_row = last_row
                        session.last_completed_col = last_col
                    if rack:
                        rack.scan_state = "paused"
                    # Offer resume/restart prompt via WebSocket (Section 4.8)
                    self._offer_resume_restart(
                        rack_id, session_id, last_row, last_col, grid_rows, grid_cols
                    )

                elif status == "aborted":
                    if session:
                        session.status = "aborted"
                        session.abort_reason = abort_reason
                    if rack:
                        rack.scan_state = "aborted"
                    release_lock(rack_id=rack_id, db=db)
                    with self._lock:
                        self._active_sessions.pop(rack_id, None)
                    self._cancel_resume_timer(rack_id)

                    # Emergency stop: require G28 before further motion
                    if abort_reason == "emergency_stop":
                        try:
                            from services.position_monitor import position_monitor
                            position_monitor.mark_stale_check_due(rack_id)
                        except Exception:
                            pass

        except Exception:
            logger.exception("on_scan_status DB error for rack=%s", rack_id)

        # Broadcast scan status to all rack subscribers
        try:
            from api.websocket import ws_registry
            ws_registry.broadcast_from_thread(
                rack_id,
                {"type": "scan_status", "rack_id": rack_id, "data": payload},
            )
        except Exception:
            logger.exception("on_scan_status WS broadcast failed for rack=%s", rack_id)

    # ── Manual-vs-scan conflict (Section 4.8) ─────────────────────────────────

    def handle_manual_command_conflict(self, rack_id: str) -> None:
        """
        Called by command_handler when a manual command arrives while
        racks.scan_state=running.
        """
        logger.info(
            "ScanEngine: manual command conflict for rack=%s — pausing scan.", rack_id
        )
        try:
            from db.database import db_session
            from db.models import Rack
            from services.mqtt_client import mqtt_client

            with db_session() as db:
                rack = db.query(Rack).filter_by(id=rack_id).first()
                if rack:
                    rack.scan_state = "paused"

            mqtt_client.publish_command(rack_id, "SCAN_STOP")

        except Exception:
            logger.exception(
                "handle_manual_command_conflict failed for rack=%s", rack_id
            )

    def handle_resume_choice(
        self,
        rack_id: str,
        session_id: int,
        choice: str,
        grid_rows: int,
        grid_cols: int,
        resume_row: Optional[int] = None,
        resume_col: Optional[int] = None,
    ) -> None:
        """
        Called from the WebSocket message handler when the operator sends
        a scan_resume_choice message.
        choice: "resume" → restart from resume_row/resume_col
                "restart" → restart from row 0 col 0
        """
        self._cancel_resume_timer(rack_id)

        if choice == "resume":
            self._publish_scan_start(
                rack_id, session_id, grid_rows, grid_cols,
                resume_row=resume_row,
                resume_col=resume_col,
            )
            logger.info(
                "ScanEngine: RESUME published for rack=%s session=%d from (%s,%s)",
                rack_id, session_id, resume_row, resume_col,
            )
        else:
            self._publish_scan_start(rack_id, None, grid_rows, grid_cols)
            logger.info("ScanEngine: RESTART published for rack=%s", rack_id)

    def trigger_manual_scan(self, rack_id: str, user_id: str, db) -> str:
        """
        Operator-initiated SCAN_START (Section 4.7 / 4.8).
        Returns: "published" | "queued" | "error:<reason>"
        """
        try:
            from db.models import Rack, ScanSession
            from core.locking import LockType, LockResult, acquire_lock

            now = datetime.utcnow()

            state = gantry_state.get(rack_id)
            pi_alive = state is not None and state.mqtt_status == "online"
            if not pi_alive:
                return "error:pi_offline"

            rack = db.query(Rack).filter_by(id=rack_id).first()
            if rack is None:
                return "error:rack_not_found"

            lock_result = acquire_lock(rack_id, user_id, LockType.SCAN, db)
            if lock_result == LockResult.ALREADY_LOCKED:
                return "queued"
            if lock_result != LockResult.ACQUIRED:
                return f"error:lock_{lock_result}"

            session = ScanSession(
                rack_id=rack_id,
                status="running",
                started_at=now,
                cells_total=rack.grid_rows * rack.grid_cols,
                cells_completed=0,
                cells_failed=0,
            )
            db.add(session)
            db.flush()
            session_id = session.id
            rack.scan_state = "running"
            grid_rows = rack.grid_rows
            grid_cols = rack.grid_cols

        except Exception:
            logger.exception("trigger_manual_scan failed for rack=%s", rack_id)
            return "error:internal"

        with self._lock:
            self._active_sessions[rack_id] = session_id

        self._publish_scan_start(rack_id, session_id, grid_rows, grid_cols)
        logger.info(
            "ScanEngine: manual SCAN_START published for rack=%s session=%d",
            rack_id, session_id,
        )
        return "published"

    def send_scan_stop(self, rack_id: str) -> None:
        """Publish SCAN_STOP to the Pi (used for operator-initiated stop)."""
        try:
            from services.mqtt_client import mqtt_client
            mqtt_client.publish_command(rack_id, "SCAN_STOP")
            logger.info("ScanEngine: SCAN_STOP sent for rack=%s", rack_id)
        except Exception:
            logger.exception("send_scan_stop failed for rack=%s", rack_id)

    # ── Resume window helpers ─────────────────────────────────────────────────

    def _offer_resume_restart(
        self,
        rack_id: str,
        session_id: Optional[int],
        last_row: Optional[int],
        last_col: Optional[int],
        grid_rows: int,
        grid_cols: int,
    ) -> None:
        """
        Push resume/restart prompt to the operator and start the window timer.

        FIX (Mismatch 3): sends expires_at (ISO 8601 timestamp string) instead
        of timeout_s (raw integer). The frontend WsMsgScanResumePrompt.data
        defines expires_at: string — a raw number is unreadable as a Date.
        """
        window_s = settings.MANUAL_VS_SCAN_RESUME_WINDOW_S
        expires_at_iso = (
            datetime.now(timezone.utc) + timedelta(seconds=window_s)
        ).isoformat()

        try:
            from api.websocket import ws_registry
            ws_registry.broadcast_from_thread(
                rack_id,
                {
                    "type": "scan_resume_prompt",
                    "rack_id": rack_id,
                    "data": {
                        "scan_session_id": session_id,
                        "last_completed_row": last_row,
                        "last_completed_col": last_col,
                        "grid_rows": grid_rows,
                        "grid_cols": grid_cols,
                        # FIX (Mismatch 3): ISO string, not a raw seconds integer
                        "expires_at": expires_at_iso,
                        "message": (
                            "Scan paused due to manual command. "
                            "Resume from last cell or restart from beginning?"
                        ),
                    },
                },
            )
        except Exception:
            logger.exception("_offer_resume_restart WS broadcast failed for rack=%s", rack_id)

        self._cancel_resume_timer(rack_id)
        timer = threading.Timer(
            window_s,
            self._on_resume_window_expired,
            args=(rack_id,),
        )
        timer.daemon = True
        timer.start()
        with self._lock:
            self._resume_timers[rack_id] = timer

    def _on_resume_window_expired(self, rack_id: str) -> None:
        """
        Section 4.8: resume window timed out — no action.
        The scan will restart at the next scheduled interval.
        """
        logger.info(
            "ScanEngine: resume window expired for rack=%s — "
            "scan will restart at next scheduled interval.",
            rack_id,
        )
        with self._lock:
            self._active_sessions.pop(rack_id, None)
            self._resume_timers.pop(rack_id, None)

    def _cancel_resume_timer(self, rack_id: str) -> None:
        with self._lock:
            timer = self._resume_timers.pop(rack_id, None)
        if timer:
            timer.cancel()

    # ── Postpone helper ───────────────────────────────────────────────────────

    def _postpone(self, rack_id: str, minutes: int) -> None:
        """Bump next_scan_at forward by `minutes` without firing the scan."""
        try:
            from db.database import db_session
            from db.models import ScanSchedule

            new_time = datetime.utcnow() + timedelta(minutes=minutes)
            with db_session() as db:
                schedule = db.query(ScanSchedule).filter_by(rack_id=rack_id).first()
                if schedule:
                    schedule.next_scan_at = new_time
        except Exception:
            logger.exception("_postpone DB error for rack=%s", rack_id)

    # ── MQTT publish helpers ──────────────────────────────────────────────────

    def _publish_scan_start(
        self,
        rack_id: str,
        session_id: Optional[int],
        grid_rows: int,
        grid_cols: int,
        resume_row: Optional[int] = None,
        resume_col: Optional[int] = None,
    ) -> None:
        """Publish SCAN_START to the Pi via the command topic (QoS 1)."""
        try:
            from services.mqtt_client import mqtt_client

            payload: dict = {
                "command": "SCAN_START",
                "scan_session_id": session_id,
                "grid_rows": grid_rows,
                "grid_cols": grid_cols,
            }
            if resume_row is not None:
                payload["resume_row"] = resume_row
                payload["resume_col"] = resume_col

            mqtt_client.publish_command(rack_id, json.dumps(payload))
        except Exception:
            logger.exception(
                "_publish_scan_start failed for rack=%s session=%s", rack_id, session_id
            )


# ── Module-level singleton ────────────────────────────────────────────────────
scan_engine = ScanEngine()
