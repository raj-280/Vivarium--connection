"""
server/core/queue_manager.py

Per-rack FIFO Command Queue (Section 4.3).

How it works
────────────
1.  submit(rack_id, command, user_id, db)
      ↳  Emergency (!) → submit_emergency() — bypasses lock/queue entirely
      ↳  Try acquire_lock() →
            ACQUIRED      → publish immediately via _publish_fn; track in cache
            ALREADY_LOCKED→ enqueue the command for later
            RACK_NOT_FOUND→ return error

2.  When any lock releases, locking.py fires on_lock_released(rack_id).
    This module registers that callback at import time.
    on_lock_released():
        pop next command from the rack's queue
        call submit() again (new lock + publish)

3.  submit_emergency(rack_id)
      → clears the rack's queue (in-flight ops are moot after E-stop)
      → publishes "!" on the emergency MQTT topic (QoS 2) immediately
      → does NOT release the lock (caller releases it after writing abort record)

Thread safety
─────────────
Each rack has its own threading.Lock guarding its deque.
A global lock guards the dicts that hold per-rack locks and queues.

Publish injection
─────────────────
configure_publish(fn) must be called before any command is submitted.
In main.py, called after mqtt_client.connect():
    configure_publish(mqtt_client.publish_command)
In tests, replaced with a mock:
    configure_publish(lambda rack_id, cmd, qos: print(...))
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── Queued item ───────────────────────────────────────────────────────────────

@dataclass
class QueuedCommand:
    rack_id: str
    command: str
    user_id: Optional[str]
    enqueued_at: datetime = field(default_factory=datetime.utcnow)


# ── Publish function injection ────────────────────────────────────────────────
# Signature: (rack_id: str, command: str, qos: int) -> None
# Replaced at startup (or in tests) via configure_publish().

_publish_fn: Optional[Callable[[str, str, int], None]] = None


def configure_publish(fn: Callable[[str, str, int], None]) -> None:
    """Inject the MQTT publish callable.  Must be called before any submit()."""
    global _publish_fn
    _publish_fn = fn
    logger.info("QueueManager publish_fn configured: %s", fn)


def _do_publish(rack_id: str, command: str, qos: int) -> None:
    if _publish_fn is None:
        raise RuntimeError(
            "QueueManager has no publish_fn. Call configure_publish() first."
        )
    _publish_fn(rack_id, command, qos)


# ── QoS + timeout helpers (Section 11 topic reference) ───────────────────────

def _lock_type_for(command: str):
    from core.locking import LockType
    cmd = command.strip().split()[0].upper()
    if cmd == "CAPTURE":
        return LockType.CAPTURE
    if cmd in ("SCAN_START", "SCAN_STOP"):
        return LockType.SCAN
    return LockType.MOTION


def _qos_for(command: str) -> int:
    """All command-topic messages are QoS 1 (Section 11).  ! uses QoS 2 (emergency topic)."""
    cmd = command.strip().split()[0].upper()
    return 2 if cmd == "!" else 1


def _timeout_for(command: str) -> int:
    cmd = command.strip().split()[0].upper()
    if cmd == "CAPTURE":
        return settings.CAPTURE_LOCK_TIMEOUT_S
    if cmd in ("SCAN_START", "SCAN_STOP"):
        return settings.CAPTURE_LOCK_TIMEOUT_S * 2
    return settings.COMMAND_TIMEOUT_S


# Deferred import to avoid circular dependency at module load time
def _settings():
    from config.settings import settings as s
    return s


# Module-level shortcut (used inside _timeout_for at call time, not import time)
try:
    from config.settings import settings
except Exception:
    settings = None  # type: ignore[assignment]


# ── QueueManager ──────────────────────────────────────────────────────────────

class QueueManager:
    """
    Thread-safe per-rack FIFO command queue.
    Use the module-level `queue_manager` singleton everywhere.
    """

    def __init__(self) -> None:
        self._global_lock = threading.Lock()
        self._queues: dict[str, deque[QueuedCommand]] = {}
        self._rack_locks: dict[str, threading.Lock] = {}

    # ── Lock helpers ──────────────────────────────────────────────────────────

    def _rack_lock(self, rack_id: str) -> threading.Lock:
        with self._global_lock:
            if rack_id not in self._rack_locks:
                self._rack_locks[rack_id] = threading.Lock()
                self._queues[rack_id] = deque()
            return self._rack_locks[rack_id]

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(
        self,
        rack_id: str,
        command: str,
        user_id: Optional[str],
    ) -> str:
        """
        Main command-dispatch entry point.

        Opens its own db_session for lock acquisition, commits + closes it,
        then calls cache.set_pending_command() which opens its own session.
        This avoids concurrent write sessions on the same SQLite file.

        Flow:
          1.  '!'  → submit_emergency() (no lock, no queue)
          2.  acquire_lock() in internal db_session → commit → close
                ACQUIRED      → publish; track in cache  → "published"
                ALREADY_LOCKED→ enqueue                  → "queued"
                RACK_NOT_FOUND→                          → "error:rack_not_found"

        Returns: "published" | "queued" | "emergency" | "error:<reason>"
        """
        from core.locking import acquire_lock, LockType, LockResult
        from services.cache import cache
        from config.settings import settings as cfg
        from db.database import db_session

        cmd_token = command.strip().split()[0].upper()
        if cmd_token == "!":
            return self.submit_emergency(rack_id)

        lock_type = _lock_type_for(command)

        # Open a dedicated session for lock acquisition, commit, then close
        # before cache opens its own session (avoids concurrent write conflict).
        with db_session() as db:
            result = acquire_lock(rack_id, user_id or "__anonymous__", lock_type, db)
        # ↑ committed and closed

        if result == LockResult.RACK_NOT_FOUND:
            logger.warning("submit: rack '%s' not found in DB", rack_id)
            return "error:rack_not_found"

        if result == LockResult.ALREADY_LOCKED:
            self._enqueue(rack_id, command, user_id)
            logger.info("Command QUEUED: rack=%s cmd=%r user=%s depth=%d",
                        rack_id, command, user_id, self.depth(rack_id))
            return "queued"

        # Lock acquired + committed — publish, then track in cache
        qos = _qos_for(command)
        try:
            _do_publish(rack_id, command, qos)
        except Exception as exc:
            logger.exception("Publish failed: rack=%s cmd=%r", rack_id, command)
            return f"error:publish_failed:{exc}"

        # Track as in-flight command for the escalation sweep (Section 4.5)
        timeout_s = cfg.CAPTURE_LOCK_TIMEOUT_S if cmd_token == "CAPTURE" else cfg.COMMAND_TIMEOUT_S
        cache.set_pending_command(rack_id, command, user_id, timeout_s)

        logger.info("Command PUBLISHED: rack=%s cmd=%r qos=%d", rack_id, command, qos)
        return "published"

    def submit_emergency(self, rack_id: str) -> str:
        """
        Publish the emergency stop immediately.

        Rules (Section 4.3 / 4.8):
          - QoS 2 on vivarium/rack/{id}/emergency topic.
          - Queue for this rack is cleared: in-flight ops are moot after E-stop.
          - Lock is NOT released here — the caller must release it after writing
            the abort/audit record to the DB.

        Returns "emergency" on success, "error:..." on publish failure.
        """
        logger.warning("EMERGENCY STOP dispatched: rack=%s", rack_id)
        self._clear_queue(rack_id)

        try:
            _do_publish(rack_id, "!", qos=2)
        except Exception as exc:
            logger.exception("Emergency publish FAILED: rack=%s", rack_id)
            return f"error:emergency_failed:{exc}"

        return "emergency"

    def on_lock_released(self, rack_id: str) -> None:
        """
        Called by locking.py (via _fire_released) whenever a rack lock is released.

        Pops the next queued command and dispatches it through submit() which
        will acquire a fresh lock and publish.  If the queue is empty, does nothing.
        """
        item = self._pop(rack_id)
        if item is None:
            return  # Queue empty — nothing to do

        logger.info(
            "Lock released: rack=%s — dispatching next queue item cmd=%r user=%s",
            rack_id, item.command, item.user_id,
        )
        # submit() manages its own db_session internally
        outcome = self.submit(rack_id, item.command, item.user_id)

        if outcome not in ("published", "queued", "emergency"):
            logger.warning(
                "Queue-pop dispatch failed: rack=%s cmd=%r outcome=%s",
                rack_id, item.command, outcome,
            )


    # ── Queue inspection ──────────────────────────────────────────────────────

    def depth(self, rack_id: str) -> int:
        """Number of commands waiting in the queue for this rack."""
        lock = self._rack_lock(rack_id)
        with lock:
            return len(self._queues[rack_id])

    def peek(self, rack_id: str) -> Optional[QueuedCommand]:
        """Return the next command without removing it, or None."""
        lock = self._rack_lock(rack_id)
        with lock:
            q = self._queues[rack_id]
            return q[0] if q else None

    def list_queue(self, rack_id: str) -> list[QueuedCommand]:
        """Return a snapshot of the full queue (copy, not a live view)."""
        lock = self._rack_lock(rack_id)
        with lock:
            return list(self._queues[rack_id])

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _enqueue(
        self,
        rack_id: str,
        command: str,
        user_id: Optional[str],
    ) -> None:
        lock = self._rack_lock(rack_id)
        with lock:
            self._queues[rack_id].append(
                QueuedCommand(rack_id=rack_id, command=command, user_id=user_id)
            )

    def _pop(self, rack_id: str) -> Optional[QueuedCommand]:
        lock = self._rack_lock(rack_id)
        with lock:
            q = self._queues.get(rack_id, deque())
            return q.popleft() if q else None

    def _clear_queue(self, rack_id: str) -> None:
        lock = self._rack_lock(rack_id)
        with lock:
            if rack_id in self._queues:
                dropped = len(self._queues[rack_id])
                self._queues[rack_id].clear()
                if dropped:
                    logger.warning(
                        "Queue CLEARED: rack=%s (%d command(s) discarded)", rack_id, dropped
                    )


# ── Module-level singleton ────────────────────────────────────────────────────

queue_manager = QueueManager()

# Register the on_lock_released callback with locking.py at import time.
# locking.py does not import queue_manager, so there is no circular dependency.
from core.locking import register_on_lock_released   # noqa: E402
register_on_lock_released(queue_manager.on_lock_released)
