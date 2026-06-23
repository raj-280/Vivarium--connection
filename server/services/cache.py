"""
server/services/cache.py

Uniform get/set/expire cache interface for two store types:
  1. Pending-command tracking  (mirrors Redis key  pending_cmd:{rack_id})
  2. Capture attribution        (mirrors Redis key  {rack_id→operator_id, TTL})

CACHE_BACKEND=sqlite (default):
    Backed by the pending_commands (Section 3.11) and capture_attribution
    (Section 3.12) ORM tables.  Every method opens and closes its own
    db_session() so it is safe to call from any thread.

CACHE_BACKEND=redis:
    Stub — raises NotImplementedError with a clear message.  Implement by
    filling in RedisCacheBackend; no callers need to change.

All callers import the module-level singleton:
    from services.cache import cache
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


# ── Shared data types ─────────────────────────────────────────────────────────

@dataclass
class PendingCommandEntry:
    """Mirrors one row of the pending_commands table (Section 3.11)."""
    rack_id: str
    command: str
    operator_id: Optional[str]
    published_at: datetime
    retry_count: int
    timeout_at: datetime

    @property
    def is_timed_out(self) -> bool:
        return datetime.now(timezone.utc) > self.timeout_at


# ── Abstract interface ────────────────────────────────────────────────────────

class CacheBackend(ABC):
    """
    Abstract cache interface.  Every caller codes against this type so
    swapping SQLite → Redis is purely a settings change.
    """

    # ── Pending command tracking ──────────────────────────────────────────────

    @abstractmethod
    def set_pending_command(
        self,
        rack_id: str,
        command: str,
        operator_id: Optional[str],
        timeout_s: int,
    ) -> None:
        """
        Write or overwrite the single in-flight command for a rack.
        Called by queue_manager immediately after publishing.
        """

    @abstractmethod
    def get_pending_command(self, rack_id: str) -> Optional[PendingCommandEntry]:
        """Return the in-flight command entry for a rack, or None."""

    @abstractmethod
    def clear_pending_command(self, rack_id: str) -> None:
        """
        Delete the in-flight record.
        Called when COMMAND_ACK arrives or the command completes.
        """

    @abstractmethod
    def increment_pending_retry(self, rack_id: str) -> int:
        """Bump retry_count by 1 and return the new value (for escalation logic)."""

    @abstractmethod
    def get_all_timed_out_commands(self) -> list[PendingCommandEntry]:
        """Return every pending entry where timeout_at < now (for the sweep task)."""

    # ── Capture attribution ───────────────────────────────────────────────────

    @abstractmethod
    def set_capture_attribution(
        self,
        rack_id: str,
        operator_id: str,
        ttl_s: int,
    ) -> None:
        """
        Record who triggered the capture and for how long.
        TTL mirrors the Redis key TTL: now + ttl_s.
        """

    @abstractmethod
    def get_capture_attribution(self, rack_id: str) -> Optional[str]:
        """
        Return the operator_id if attribution is still valid (not expired).
        Returns None if the attribution never existed or has expired.
        Does NOT delete the record; use consume_capture_attribution for that.
        """

    @abstractmethod
    def consume_capture_attribution(self, rack_id: str) -> Optional[str]:
        """
        Return the operator_id (if not expired) and delete the record.
        Called when the image MQTT notification arrives for a rack.
        Returns None if expired — callers should write triggered_by_operator=null
        and emit a validation_failure audit log entry (Section 3.12).
        """


# ── SQLite implementation ─────────────────────────────────────────────────────

class SQLiteCacheBackend(CacheBackend):
    """
    SQLite implementation backed by the pending_commands and
    capture_attribution tables (Section 3.11 / 3.12).

    Each method opens its own db_session() — thread-safe, no shared state.
    """

    # ── Pending commands ──────────────────────────────────────────────────────

    def set_pending_command(
        self, rack_id: str, command: str, operator_id: Optional[str], timeout_s: int
    ) -> None:
        from db.database import db_session
        from db.models import PendingCommand

        now = datetime.now(timezone.utc)
        with db_session() as db:
            row = db.query(PendingCommand).filter_by(rack_id=rack_id).first()
            if row:
                row.command = command
                row.operator_id = operator_id
                row.published_at = now
                row.retry_count = 0
                row.timeout_at = now + timedelta(seconds=timeout_s)
            else:
                db.add(PendingCommand(
                    rack_id=rack_id,
                    command=command,
                    operator_id=operator_id,
                    published_at=now,
                    retry_count=0,
                    timeout_at=now + timedelta(seconds=timeout_s),
                ))
        logger.debug(
            "set_pending_command: rack=%s cmd=%s timeout_s=%d", rack_id, command, timeout_s
        )

    def get_pending_command(self, rack_id: str) -> Optional[PendingCommandEntry]:
        from db.database import db_session
        from db.models import PendingCommand

        with db_session() as db:
            row = db.query(PendingCommand).filter_by(rack_id=rack_id).first()
            if not row:
                return None
            return PendingCommandEntry(
                rack_id=row.rack_id,
                command=row.command,
                operator_id=row.operator_id,
                published_at=row.published_at,
                retry_count=row.retry_count,
                timeout_at=row.timeout_at,
            )

    def clear_pending_command(self, rack_id: str) -> None:
        from db.database import db_session
        from db.models import PendingCommand

        with db_session() as db:
            deleted = (
                db.query(PendingCommand)
                .filter_by(rack_id=rack_id)
                .delete(synchronize_session=False)
            )
        if deleted:
            logger.debug("clear_pending_command: rack=%s", rack_id)

    def increment_pending_retry(self, rack_id: str) -> int:
        from db.database import db_session
        from db.models import PendingCommand

        with db_session() as db:
            row = db.query(PendingCommand).filter_by(rack_id=rack_id).first()
            if not row:
                return 0
            row.retry_count += 1
            new_count = row.retry_count
        logger.debug("increment_pending_retry: rack=%s retry_count=%d", rack_id, new_count)
        return new_count

    def get_all_timed_out_commands(self) -> list[PendingCommandEntry]:
        from db.database import db_session
        from db.models import PendingCommand

        now = datetime.now(timezone.utc)
        with db_session() as db:
            rows = (
                db.query(PendingCommand)
                .filter(PendingCommand.timeout_at < now)
                .all()
            )
            return [
                PendingCommandEntry(
                    rack_id=r.rack_id,
                    command=r.command,
                    operator_id=r.operator_id,
                    published_at=r.published_at,
                    retry_count=r.retry_count,
                    timeout_at=r.timeout_at,
                )
                for r in rows
            ]

    # ── Capture attribution ───────────────────────────────────────────────────

    def set_capture_attribution(
        self, rack_id: str, operator_id: str, ttl_s: int
    ) -> None:
        from db.database import db_session
        from db.models import CaptureAttribution

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
        with db_session() as db:
            row = db.query(CaptureAttribution).filter_by(rack_id=rack_id).first()
            if row:
                row.operator_id = operator_id
                row.expires_at = expires_at
            else:
                db.add(CaptureAttribution(
                    rack_id=rack_id,
                    operator_id=operator_id,
                    expires_at=expires_at,
                ))
        logger.debug(
            "set_capture_attribution: rack=%s operator=%s ttl=%ds",
            rack_id, operator_id, ttl_s,
        )

    def get_capture_attribution(self, rack_id: str) -> Optional[str]:
        from db.database import db_session
        from db.models import CaptureAttribution

        now = datetime.now(timezone.utc)
        with db_session() as db:
            row = db.query(CaptureAttribution).filter_by(rack_id=rack_id).first()
            if row and row.expires_at > now:
                return row.operator_id
            return None

    def consume_capture_attribution(self, rack_id: str) -> Optional[str]:
        """Read + delete in one session so no other caller can consume the same entry."""
        from db.database import db_session
        from db.models import CaptureAttribution

        now = datetime.now(timezone.utc)
        with db_session() as db:
            row = db.query(CaptureAttribution).filter_by(rack_id=rack_id).first()
            if not row:
                return None
            # Check expiry before consuming
            operator_id = row.operator_id if row.expires_at > now else None
            db.delete(row)
            # db_session commits on exit — deletion is atomic with the read
        if operator_id is None:
            logger.warning(
                "consume_capture_attribution: rack=%s attribution EXPIRED", rack_id
            )
        return operator_id


# ── Redis stub ────────────────────────────────────────────────────────────────

class RedisCacheBackend(CacheBackend):
    """
    Redis implementation stub.
    Activate by setting CACHE_BACKEND=redis and REDIS_URL in .env.
    Implementation left for the production hardening pass (Stage 15).
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "Redis cache backend is not yet implemented. "
            "Use CACHE_BACKEND=sqlite (the default) for local development."
        )

    # Stub implementations so ABC is satisfied (never reached due to __init__ raise)
    def set_pending_command(self, *a, **kw): raise NotImplementedError
    def get_pending_command(self, *a, **kw): raise NotImplementedError
    def clear_pending_command(self, *a, **kw): raise NotImplementedError
    def increment_pending_retry(self, *a, **kw): raise NotImplementedError
    def get_all_timed_out_commands(self, *a, **kw): raise NotImplementedError
    def set_capture_attribution(self, *a, **kw): raise NotImplementedError
    def get_capture_attribution(self, *a, **kw): raise NotImplementedError
    def consume_capture_attribution(self, *a, **kw): raise NotImplementedError


# ── Factory + singleton ───────────────────────────────────────────────────────

def _make_cache() -> CacheBackend:
    backend = settings.CACHE_BACKEND
    if backend == "sqlite":
        logger.info("Cache backend: SQLite (pending_commands / capture_attribution tables)")
        return SQLiteCacheBackend()
    if backend == "redis":
        logger.info("Cache backend: Redis (%s)", settings.REDIS_URL)
        return RedisCacheBackend()
    raise ValueError(
        f"Unknown CACHE_BACKEND={backend!r}. Valid values: 'sqlite', 'redis'."
    )


cache: CacheBackend = _make_cache()
