"""
server/core/state.py

In-memory GantryState mirror per rack (Section 4.2).

Mirrors the live `racks` DB row (position, online flags, scan state) so the
WebSocket broadcaster and MQTT message handlers can read rack status without
hitting the database on every incoming message.

Design:
  - RackState  — dataclass holding the fields mirrored from `racks`
  - GantryState — thread-safe dict[rack_id → RackState] with upsert / reconcile
  - gantry_state — module-level singleton; imported everywhere as:
        from core.state import gantry_state

The reconcile_from_db() method is called:
  - At startup (in main.py lifespan) to seed the mirror from the DB.
  - Periodically (future: by a background task) to catch any DB changes
    that bypassed the in-memory layer (e.g. a direct SQL admin edit).

Thread safety: all mutations are protected by a threading.Lock so MQTT
callbacks (running in paho's background thread) and FastAPI route handlers
(running in uvicorn's thread pool) can both write safely.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # Avoid a circular import at runtime: models import nothing from here.
    from db.models import Rack as RackModel


# ---------------------------------------------------------------------------
# RackState — the fields mirrored from the `racks` table row
# ---------------------------------------------------------------------------
@dataclass
class RackState:
    """
    Snapshot of a single rack's runtime state.  All fields have safe defaults
    so a newly provisioned rack can be added to the mirror before its first
    MQTT message arrives.
    """
    rack_id: str

    # ----- Position (from M114 responses) -----------------------------------
    last_position_x: Optional[float] = None
    last_position_y: Optional[float] = None
    last_position_c: Optional[float] = None

    # ----- Homing flags (from M114 homed: X=?/Y=?/C=?) ---------------------
    homed_x: bool = False
    homed_y: bool = False
    homed_c: bool = False
    last_homed_at: Optional[datetime] = None

    # ----- Connectivity ------------------------------------------------------
    # mqtt_status / camera_status mirror racks.mqtt_status / racks.camera_status.
    # pi_online is a derived convenience flag kept in sync on every upsert.
    mqtt_status: str = "offline"        # online / offline
    camera_status: str = "unknown"      # online / offline / unknown
    pi_online: bool = False             # derived: mqtt_status == "online"

    # ----- Scan state --------------------------------------------------------
    scan_state: str = "idle"            # idle / running / paused / complete / aborted

    # ----- Lock state (Section 4.3) -----------------------------------------
    lock_holder_user_id: Optional[str] = None
    lock_type: Optional[str] = None     # motion / capture / scan
    lock_expires_at: Optional[datetime] = None

    # ----- Escalation --------------------------------------------------------
    maintenance_required: bool = False

    # ----- Rack geometry (from LAYOUT_CONFIG / DB) ---------------------------
    # These are populated from DB at startup and refreshed when the Pi sends
    # a LAYOUT_CONFIG message on every reconnect.
    grid_rows: Optional[int] = None
    grid_cols: Optional[int] = None
    pitch_x_mm: Optional[float] = None
    pitch_y_mm: Optional[float] = None
    x0_offset_mm: Optional[float] = None
    y0_offset_mm: Optional[float] = None

    # ----- Machine limits (from M799 / LAYOUT_CONFIG) -----------------------
    limit_x_mm: Optional[float] = None
    limit_y_mm: Optional[float] = None
    limit_c_mm: Optional[float] = None

    # ----- Bookkeeping -------------------------------------------------------
    # Timestamp of the last in-memory update — used by reconcile_from_db()
    # to decide whether the DB row or the in-memory value is more recent.
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# GantryState — thread-safe per-rack state registry
# ---------------------------------------------------------------------------
class GantryState:
    """
    Thread-safe in-memory mirror of all rack states.

    All MQTT message handlers call upsert() to update the mirror first;
    the DB write happens separately (and slightly later) in the same handler.
    The WebSocket broadcaster reads from here so it never blocks on a DB query
    for high-frequency position updates (M114 can arrive several times per
    second during a scan).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._racks: dict[str, RackState] = {}

    # ----- Read access -------------------------------------------------------

    def get(self, rack_id: str) -> Optional[RackState]:
        """Return the current state for a rack, or None if unknown."""
        with self._lock:
            return self._racks.get(rack_id)

    def get_all(self) -> dict[str, RackState]:
        """Return a shallow copy of the full rack-state dict."""
        with self._lock:
            return dict(self._racks)

    def rack_ids(self) -> list[str]:
        """Return a list of all known rack IDs."""
        with self._lock:
            return list(self._racks.keys())

    # ----- Write access ------------------------------------------------------

    def upsert(self, rack_id: str, **kwargs) -> RackState:
        """
        Create or update a rack's state entry.

        Keyword arguments must match RackState field names.
        The derived `pi_online` flag is recalculated whenever `mqtt_status`
        is provided (or when a new entry is created).
        `updated_at` is always set to datetime.utcnow() on every call.

        Example:
            gantry_state.upsert(
                "rack-001",
                last_position_x=120.5,
                last_position_y=80.0,
                homed_x=True, homed_y=True, homed_c=True,
            )
        """
        with self._lock:
            if rack_id not in self._racks:
                self._racks[rack_id] = RackState(rack_id=rack_id)

            state = self._racks[rack_id]
            for key, value in kwargs.items():
                if hasattr(state, key):
                    setattr(state, key, value)
                # Unknown keys are silently ignored — callers should not
                # rely on this; use explicit field names for clarity.

            # Always refresh derived flag and timestamp
            state.pi_online = state.mqtt_status == "online"
            state.updated_at = datetime.utcnow()
            return state

    def remove(self, rack_id: str) -> None:
        """Remove a rack from the mirror (e.g. after decommissioning)."""
        with self._lock:
            self._racks.pop(rack_id, None)

    # ----- DB reconciliation -------------------------------------------------

    def reconcile_from_db(self, db_racks: list[RackModel]) -> None:
        """
        Seed / reconcile the in-memory mirror from a list of ORM Rack objects.

        Strategy — last-write-wins by timestamp:
          - If no in-memory entry exists for a rack_id, create one from the DB row.
          - If an in-memory entry exists and its updated_at is MORE recent than
            the DB row's updated_at, keep the in-memory value (a live MQTT message
            has already superseded the DB snapshot).
          - Otherwise (DB row is equal or newer) overwrite with the DB row.

        Called by main.py lifespan at startup, and optionally by a periodic
        background task for drift correction.  It is safe to call at any time.
        """
        with self._lock:
            for rack in db_racks:
                rack_id: str = rack.id
                existing = self._racks.get(rack_id)

                db_updated: Optional[datetime] = rack.updated_at
                mem_updated: Optional[datetime] = existing.updated_at if existing else None

                # Decide whether the DB value should overwrite the in-memory value.
                should_overwrite = (
                    existing is None
                    or mem_updated is None
                    or db_updated is None
                    or db_updated >= mem_updated
                )

                if should_overwrite:
                    self._racks[rack_id] = RackState(
                        rack_id=rack_id,
                        last_position_x=rack.last_position_x,
                        last_position_y=rack.last_position_y,
                        last_position_c=rack.last_position_c,
                        homed_x=rack.homed_x,
                        homed_y=rack.homed_y,
                        homed_c=rack.homed_c,
                        last_homed_at=rack.last_homed_at,
                        mqtt_status=rack.mqtt_status,
                        camera_status=rack.camera_status,
                        pi_online=rack.mqtt_status == "online",
                        scan_state=rack.scan_state,
                        lock_holder_user_id=rack.lock_holder_user_id,
                        lock_type=rack.lock_type,
                        lock_expires_at=rack.lock_expires_at,
                        maintenance_required=rack.maintenance_required,
                        # Rack geometry (from DB or LAYOUT_CONFIG)
                        grid_rows=getattr(rack, 'grid_rows', None),
                        grid_cols=getattr(rack, 'grid_cols', None),
                        pitch_x_mm=getattr(rack, 'pitch_x_mm', None),
                        pitch_y_mm=getattr(rack, 'pitch_y_mm', None),
                        x0_offset_mm=getattr(rack, 'x0_offset_mm', None),
                        y0_offset_mm=getattr(rack, 'y0_offset_mm', None),
                        # Machine limits (from M799 / LAYOUT_CONFIG)
                        limit_x_mm=getattr(rack, 'limit_x_mm', None),
                        limit_y_mm=getattr(rack, 'limit_y_mm', None),
                        limit_c_mm=getattr(rack, 'limit_c_mm', None),
                        updated_at=db_updated,
                    )


# ---------------------------------------------------------------------------
# Module-level singleton
# Import everywhere as:  from core.state import gantry_state
# ---------------------------------------------------------------------------
gantry_state = GantryState()
