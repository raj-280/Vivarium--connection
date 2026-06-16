"""
server/core/locking.py

Rack Locking (Section 4.3).

Lock lifecycle
──────────────
acquire_lock()
    Writes lock_holder_user_id / lock_type / lock_acquired_at / lock_expires_at
    to the racks DB row AND to the gantry_state in-memory mirror.
    Returns LockResult.ACQUIRED or ALREADY_LOCKED or RACK_NOT_FOUND.

release_lock()
    Clears all four lock columns; fires on_lock_released callbacks so
    queue_manager can immediately dispatch the next queued command.

extend_lock()
    Resets lock_expires_at to now + N seconds.
    Called when CAPTURE_STARTED arrives (Section 4.3 keepalive).
    Called by scan_executor for scan-lock keepalives (Section 5.5).

sweep_expired_locks()
    Finds every rack whose lock_expires_at < now and releases them.
    Fires callbacks after committing so no nested DB sessions open.

start_lock_sweep_task()
    Starts a daemon thread that calls sweep_expired_locks() every 2 seconds.
    Called once from main.py lifespan at startup.

Lock-type expiry durations (Section 4.3 / Section 2.1 Timeouts group):
    motion  → MOTION_TIMEOUT_S         (default 30s)
    capture → CAPTURE_LOCK_TIMEOUT_S   (default 120s; reset on CAPTURE_STARTED)
    scan    → 2 × CAPTURE_LOCK_TIMEOUT_S (keepalive extends it continuously)
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

from config.settings import settings
from core.state import gantry_state

logger = logging.getLogger(__name__)


# ── Enumerations ──────────────────────────────────────────────────────────────

class LockType(str, Enum):
    MOTION  = "motion"
    CAPTURE = "capture"
    SCAN    = "scan"


class LockResult(str, Enum):
    ACQUIRED          = "acquired"
    ALREADY_LOCKED    = "already_locked"
    RACK_NOT_FOUND    = "rack_not_found"


# ── LockInfo ──────────────────────────────────────────────────────────────────

@dataclass
class LockInfo:
    rack_id: str
    holder_user_id: str
    lock_type: LockType
    acquired_at: datetime
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, (self.expires_at - datetime.utcnow()).total_seconds())


# ── Callback registry (queue_manager registers here at import time) ───────────

_on_released_callbacks: list[Callable[[str], None]] = []


def register_on_lock_released(callback: Callable[[str], None]) -> None:
    """
    Register a function to be called whenever a rack lock is released.
    queue_manager calls this at module load time so it can pop + dispatch
    the next queued command automatically.
    """
    _on_released_callbacks.append(callback)
    logger.debug("Registered on_lock_released callback: %s", callback)


def _fire_released(rack_id: str) -> None:
    """Fire all registered lock-released callbacks for rack_id."""
    for cb in _on_released_callbacks:
        try:
            cb(rack_id)
        except Exception:
            logger.exception(
                "Error in on_lock_released callback %s for rack=%s", cb, rack_id
            )


# ── Expiry helpers ────────────────────────────────────────────────────────────

def _expiry_seconds(lock_type: LockType) -> int:
    if lock_type == LockType.MOTION:
        return settings.MOTION_TIMEOUT_S
    if lock_type == LockType.CAPTURE:
        return settings.CAPTURE_LOCK_TIMEOUT_S
    if lock_type == LockType.SCAN:
        # Initial scan-lock window; kept alive by scan_executor keepalives.
        return max(settings.MOTION_TIMEOUT_S, settings.CAPTURE_LOCK_TIMEOUT_S) * 2
    return settings.MOTION_TIMEOUT_S


# ── Core operations ───────────────────────────────────────────────────────────

def acquire_lock(
    rack_id: str,
    user_id: str,
    lock_type: LockType,
    db,
) -> LockResult:
    """
    Attempt to acquire a lock for rack_id on behalf of user_id.

    Checks racks.lock_holder_user_id and lock_expires_at in the same
    transaction; a lock whose expiry has already passed is treated as absent
    (the sweep will clean it up separately, but we don't block on it).

    Writes to DB row AND in-memory gantry_state mirror.

    Returns:
      ACQUIRED         — lock granted; caller may proceed to publish.
      ALREADY_LOCKED   — a valid lock exists; caller should enqueue.
      RACK_NOT_FOUND   — rack_id not in the DB.
    """
    from db.models import Rack

    rack = db.query(Rack).filter_by(id=rack_id).first()
    if rack is None:
        logger.warning("acquire_lock: rack %s not found", rack_id)
        return LockResult.RACK_NOT_FOUND

    now = datetime.utcnow()

    # Treat unexpired locks as blocking
    if (
        rack.lock_holder_user_id is not None
        and rack.lock_expires_at is not None
        and rack.lock_expires_at > now
    ):
        logger.debug(
            "acquire_lock BLOCKED: rack=%s locked_by=%s expires_in=%.1fs",
            rack_id,
            rack.lock_holder_user_id,
            (rack.lock_expires_at - now).total_seconds(),
        )
        return LockResult.ALREADY_LOCKED

    # Acquire (or overwrite an expired lock)
    expires_at = now + timedelta(seconds=_expiry_seconds(lock_type))
    rack.lock_holder_user_id = user_id
    rack.lock_type            = lock_type.value
    rack.lock_acquired_at     = now
    rack.lock_expires_at      = expires_at
    rack.updated_at           = now
    db.flush()  # Write to DB within caller's transaction; caller commits.

    # Mirror to in-memory state
    gantry_state.upsert(
        rack_id,
        lock_holder_user_id=user_id,
        lock_type=lock_type.value,
        lock_expires_at=expires_at,
    )

    logger.info(
        "Lock ACQUIRED: rack=%s user=%s type=%s expires_in=%ds",
        rack_id, user_id, lock_type.value, _expiry_seconds(lock_type),
    )
    return LockResult.ACQUIRED


def release_lock(rack_id: str, db, trigger_queue: bool = True) -> bool:
    """
    Release the lock on rack_id.

    trigger_queue=True (default): fires on_lock_released callbacks so
    queue_manager auto-dispatches the next queued command.

    Pass trigger_queue=False from sweep_expired_locks() — the sweep fires
    callbacks itself after the transaction commits to avoid nested sessions.

    Returns True if a lock was actually released, False if the rack had none.
    """
    from db.models import Rack

    rack = db.query(Rack).filter_by(id=rack_id).first()
    if rack is None or rack.lock_holder_user_id is None:
        return False

    now = datetime.utcnow()
    rack.lock_holder_user_id = None
    rack.lock_type            = None
    rack.lock_acquired_at     = None
    rack.lock_expires_at      = None
    rack.updated_at           = now
    db.flush()

    gantry_state.upsert(
        rack_id,
        lock_holder_user_id=None,
        lock_type=None,
        lock_expires_at=None,
    )

    logger.info("Lock RELEASED: rack=%s trigger_queue=%s", rack_id, trigger_queue)

    if trigger_queue:
        _fire_released(rack_id)

    return True


def extend_lock(rack_id: str, additional_seconds: int, db) -> bool:
    """
    Reset lock_expires_at to now + additional_seconds.

    Called:
      - When CAPTURE_STARTED arrives (lock-keepalive for slow S3 uploads).
      - Periodically by scan_executor (scan_lock_keepalive_interval_s).

    Returns True if a lock was found and extended, False otherwise.
    """
    from db.models import Rack

    rack = db.query(Rack).filter_by(id=rack_id).first()
    if rack is None or rack.lock_holder_user_id is None:
        return False

    now         = datetime.utcnow()
    new_expires = now + timedelta(seconds=additional_seconds)
    rack.lock_expires_at = new_expires
    rack.updated_at      = now
    db.flush()

    gantry_state.upsert(rack_id, lock_expires_at=new_expires)

    logger.debug(
        "Lock EXTENDED: rack=%s +%ds → expires %s",
        rack_id, additional_seconds, new_expires.isoformat(),
    )
    return True


def get_lock_info(rack_id: str, db) -> Optional[LockInfo]:
    """Return current LockInfo for a rack, or None if unlocked."""
    from db.models import Rack

    rack = db.query(Rack).filter_by(id=rack_id).first()
    if rack is None or rack.lock_holder_user_id is None:
        return None

    return LockInfo(
        rack_id=rack_id,
        holder_user_id=rack.lock_holder_user_id,
        lock_type=LockType(rack.lock_type),
        acquired_at=rack.lock_acquired_at,
        expires_at=rack.lock_expires_at,
    )


def is_locked(rack_id: str, db) -> bool:
    """Return True if rack has a valid (non-expired) lock in the DB."""
    info = get_lock_info(rack_id, db)
    return info is not None and not info.is_expired


# ── Background sweep ──────────────────────────────────────────────────────────

def sweep_expired_locks() -> list[str]:
    """
    Find every rack whose lock has expired and release it.

    Strategy:
      1. Open a db_session, find all expired locks, clear the columns, commit.
      2. AFTER the session closes, fire on_lock_released callbacks.
         This keeps the transaction tight and avoids nested sessions inside
         callbacks (which themselves open new sessions to dispatch commands).

    Returns the list of rack_ids that were swept.
    """
    from db.database import db_session
    from db.models import Rack

    now = datetime.utcnow()
    released: list[str] = []

    with db_session() as db:
        expired = (
            db.query(Rack)
            .filter(
                Rack.lock_holder_user_id.isnot(None),
                Rack.lock_expires_at.isnot(None),
                Rack.lock_expires_at < now,
            )
            .all()
        )
        for rack in expired:
            rack.lock_holder_user_id = None
            rack.lock_type            = None
            rack.lock_acquired_at     = None
            rack.lock_expires_at      = None
            rack.updated_at           = now
            released.append(rack.id)
            gantry_state.upsert(
                rack.id,
                lock_holder_user_id=None,
                lock_type=None,
                lock_expires_at=None,
            )
            logger.info("Lock SWEPT (expired): rack=%s", rack.id)
    # Session committed and closed ↑

    # Fire callbacks after commit so on_lock_released can open its own session
    for rack_id in released:
        _fire_released(rack_id)

    # Also send stream_close to any WebSocket connections watching the swept
    # rack — the browser must tear down its <video> element when the lock
    # expires (Section 8 / Section 9 Layer 2D).
    # user_id is None here because the sweep daemon doesn't track who last
    # held the lock; broadcast_stream_close broadcasts to all subscribers in
    # that case (the targeted-close already fired when the explicit release
    # endpoint was called by the operator).
    if released:
        try:
            from services.streaming import broadcast_stream_close  # lazy, avoids circ import
            for rack_id in released:
                broadcast_stream_close(rack_id, user_id=None)
        except Exception:
            logger.exception("broadcast_stream_close failed during lock sweep")

    return released


# ── Daemon sweep thread ───────────────────────────────────────────────────────

_sweep_thread: Optional[threading.Thread] = None
_sweep_stop = threading.Event()


def start_lock_sweep_task(interval_s: float = 2.0) -> None:
    """
    Start the background daemon thread that calls sweep_expired_locks()
    every interval_s seconds.  Called once from main.py lifespan.
    """
    global _sweep_thread, _sweep_stop

    if _sweep_thread and _sweep_thread.is_alive():
        logger.warning("start_lock_sweep_task: sweep thread is already running.")
        return

    _sweep_stop.clear()

    def _loop() -> None:
        logger.info("Lock sweep thread started (interval=%.1fs)", interval_s)
        while not _sweep_stop.is_set():
            try:
                swept = sweep_expired_locks()
                if swept:
                    logger.debug("Sweep: released %d lock(s): %s", len(swept), swept)
            except Exception:
                logger.exception("Lock sweep thread error")
            _sweep_stop.wait(interval_s)
        logger.info("Lock sweep thread stopped.")

    _sweep_thread = threading.Thread(target=_loop, daemon=True, name="lock-sweep")
    _sweep_thread.start()


def stop_lock_sweep_task() -> None:
    """Signal the sweep thread to exit cleanly.  Called from main.py shutdown."""
    _sweep_stop.set()
