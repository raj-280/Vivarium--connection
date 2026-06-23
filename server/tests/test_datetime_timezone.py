"""
server/tests/test_datetime_timezone.py
=======================================

Regression tests for BUG-01: all datetime values used in comparisons must be
timezone-aware (UTC). Naive datetimes cause TypeError in Python 3.12+ when
compared to aware datetimes in SQLAlchemy filter clauses and model properties.

What is tested
--------------
  1.  GantryState.upsert() sets updated_at to timezone-aware UTC
  2.  LockRecord.is_expired does not raise TypeError (aware expires_at)
  3.  LockRecord.seconds_remaining does not raise TypeError
  4.  PendingCommandEntry.is_timed_out does not raise TypeError
  5.  Expired LockRecord.is_expired returns True
  6.  Future LockRecord.is_expired returns False
  7.  Future LockRecord.seconds_remaining > 0
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


class TestGantryStateDatetime:
    """GantryState.upsert() — updated_at must be timezone-aware."""

    def test_updated_at_is_utc_aware(self):
        from core.state import GantryState
        gs = GantryState()
        gs.upsert("rack-tz-01", mqtt_status="online")
        state = gs.get("rack-tz-01")
        assert state.updated_at is not None
        assert state.updated_at.tzinfo is not None
        assert state.updated_at.tzinfo == timezone.utc

    def test_updated_at_is_recent(self):
        from core.state import GantryState
        gs = GantryState()
        before = datetime.now(timezone.utc)
        gs.upsert("rack-tz-02", mqtt_status="offline")
        after = datetime.now(timezone.utc)
        state = gs.get("rack-tz-02")
        assert before <= state.updated_at <= after


class TestLockRecordDatetime:
    """LockRecord property comparisons must not raise TypeError."""

    def _make_lock(self, expires_at: datetime):
        from core.locking import LockRecord, LockType
        return LockRecord(
            rack_id="rack-tz-lock",
            lock_type=LockType.MOTION,
            holder_user_id="user-1",
            acquired_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )

    def test_expired_lock_is_expired_true(self):
        lock = self._make_lock(datetime.now(timezone.utc) - timedelta(hours=1))
        assert lock.is_expired is True  # must not raise TypeError

    def test_future_lock_is_expired_false(self):
        lock = self._make_lock(datetime.now(timezone.utc) + timedelta(hours=1))
        assert lock.is_expired is False

    def test_seconds_remaining_positive_for_future_lock(self):
        lock = self._make_lock(datetime.now(timezone.utc) + timedelta(seconds=30))
        remaining = lock.seconds_remaining  # must not raise TypeError
        assert isinstance(remaining, float)
        assert remaining > 0

    def test_seconds_remaining_zero_for_expired_lock(self):
        lock = self._make_lock(datetime.now(timezone.utc) - timedelta(seconds=5))
        assert lock.seconds_remaining == 0.0


class TestPendingCommandDatetime:
    """PendingCommandEntry.is_timed_out must not raise TypeError."""

    def _make_entry(self, timeout_at: datetime):
        from services.cache import PendingCommandEntry
        return PendingCommandEntry(
            rack_id="rack-tz-cmd",
            command="G28",
            operator_id="op-1",
            published_at=datetime.now(timezone.utc),
            retry_count=0,
            timeout_at=timeout_at,
        )

    def test_timed_out_entry_returns_true(self):
        entry = self._make_entry(datetime.now(timezone.utc) - timedelta(seconds=1))
        assert entry.is_timed_out is True

    def test_active_entry_returns_false(self):
        entry = self._make_entry(datetime.now(timezone.utc) + timedelta(seconds=30))
        assert entry.is_timed_out is False
