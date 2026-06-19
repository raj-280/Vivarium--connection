"""
server/services/position_monitor.py

M114 Position Verification and Recovery — Section 4.4.

Called from main.py's MQTT response handler on every M114 response that
arrives on vivarium/rack/{id}/response.

Three checks run on every M114:
  1. Homed-flag check  — if any axis is un-homed (N), the position is
     unreliable.  For auto-scan, this always triggers G28 before proceeding.
     For manual commands, it blocks and prompts the operator.
  2. Tolerance check   — reported position vs. the commanded target.  If the
     difference exceeds the per-rack position_tolerance_x_mm /
     position_tolerance_y_mm, triggers the automatic recovery sequence.
  3. Stale-homing check — on first command after server restart or Pi
     reconnect, if racks.last_homed_at is null or older than
     STALE_HOMING_THRESHOLD_HOURS, prompts operator to home first.

Automatic recovery sequence (Section 4.4, triggered on tolerance failure
or STALL_DETECTED / SERIAL_TIMEOUT in the command handler):
  Step 1 — Publish ! (QoS 2) then G28 (QoS 1).
  Step 2 — Set rack UI status to "re-homing" (orange) while this runs.
  Step 3 — On successful homing (next M114 with all axes homed), retry the
            original failed command.
  Step 4 — If the position error recurs after re-home:
              racks.maintenance_required = True  (red indicator)
              scan_schedule.enabled = False
              admin alert pushed over /ws

M114 parsing
────────────
Expected format from the Arduino (Section 6):
    "X:12.50 Y:24.00 C:0.00 homed:X=Y Y=Y C=N"
Position values are the axis coordinates in mm.
Homed flags: Y = homed, N = not homed.

FIX (Mismatch 2): All alert WebSocket messages now use the correct envelope:
    { type: "alert", rack_id: "...", data: { level: "...", code: "...", message: "..." } }
matching frontend WsMsgAlert exactly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from config.settings import settings
from core.state import gantry_state

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Hours before a last_homed_at timestamp is considered stale.
# Operator is prompted to home before any motion if this threshold is exceeded.
STALE_HOMING_THRESHOLD_HOURS: float = 12.0

# M114 line parser — handles the Arduino's exact output format (Section 6)
_M114_RE = re.compile(
    r"X:(?P<x>-?[\d.]+)\s+Y:(?P<y>-?[\d.]+)\s+C:(?P<c>-?[\d.]+)"
    r".*?homed:X=(?P<hx>[YN])\s*Y=(?P<hy>[YN])\s*C=(?P<hc>[YN])",
    re.IGNORECASE,
)


def _alert_msg(rack_id: str, level: str, code: str, message: str) -> dict:
    """
    Build a correctly-shaped WsMsgAlert envelope.

    FIX (Mismatch 2): The old code sent flat top-level fields
    (severity=, detail=). The frontend type WsMsgAlert expects:
        { type: "alert", rack_id?: str, data: { level, code, message } }
    All alert broadcasts in this module now call this helper.
    """
    return {
        "type": "alert",
        "rack_id": rack_id,
        "data": {
            "level": level,    # was: "severity" — renamed to match WsMsgAlert.data.level
            "code": code,
            "message": message,  # was: "detail" — renamed to match WsMsgAlert.data.message
        },
    }


# ── Parsed M114 result ────────────────────────────────────────────────────────

@dataclass
class M114Result:
    """Typed result of parsing one M114 response line."""
    rack_id: str
    x: float
    y: float
    c: float
    homed_x: bool
    homed_y: bool
    homed_c: bool

    @property
    def all_homed(self) -> bool:
        return self.homed_x and self.homed_y and self.homed_c

    @property
    def any_unhomed(self) -> bool:
        return not self.all_homed


# ── Per-rack recovery state ───────────────────────────────────────────────────

@dataclass
class RecoveryState:
    """Tracks the recovery sequence state for a single rack."""
    in_recovery: bool = False
    recovery_attempt: int = 0
    original_command: Optional[str] = None
    triggered_at: Optional[datetime] = None


# ── PositionMonitor ───────────────────────────────────────────────────────────

class PositionMonitor:
    """
    Singleton position monitor.  Called from the MQTT response handler for
    every M114 line.

    Thread safety:
        All mutable state is protected by _lock since MQTT callbacks and the
        FastAPI request handler thread both call into this monitor.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Per-rack recovery state (populated on demand)
        self._recovery: dict[str, RecoveryState] = {}
        # Per-rack: last commanded target position (x_mm, y_mm) for the
        # tolerance check — set by command_handler before publishing M700.
        self._commanded_targets: dict[str, tuple[Optional[float], Optional[float]]] = {}
        # Per-rack: whether the next M114 should run the stale-homing check.
        self._stale_check_due: dict[str, bool] = {}

    # ── Public interface ──────────────────────────────────────────────────────

    def on_m114(self, rack_id: str, raw: str) -> Optional[M114Result]:
        """
        Parse a raw M114 response and run all three checks.

        Returns the parsed M114Result (or None if the line is not M114 format).
        Side-effects:
          • Updates gantry_state (position + homing flags).
          • Updates DB (racks.last_homed_at, racks.homed_*, racks.last_position_*).
          • Triggers recovery sequence on tolerance failure.
          • Sends alert over WebSocket on maintenance_required.
        """
        result = self._parse_m114(rack_id, raw)
        if result is None:
            return None

        logger.debug(
            "M114 rack=%s  X=%.2f Y=%.2f C=%.2f  homed=(%s,%s,%s)",
            rack_id, result.x, result.y, result.c,
            "Y" if result.homed_x else "N",
            "Y" if result.homed_y else "N",
            "Y" if result.homed_c else "N",
        )

        # Update the in-memory state mirror
        self._update_state(result)

        # Update the DB asynchronously to avoid blocking the MQTT loop
        threading.Thread(
            target=self._persist_position,
            args=(result,),
            daemon=True,
            name=f"pos-persist-{rack_id}",
        ).start()

        # ── Check 1: Homed-flag check ─────────────────────────────────────
        if result.any_unhomed:
            logger.warning(
                "rack=%s M114: axis unhomed (homed_x=%s homed_y=%s homed_c=%s) — "
                "position unreliable. Scan engine will mandate G28 before proceeding.",
                rack_id,
                result.homed_x, result.homed_y, result.homed_c,
            )

        # ── Check 2: Tolerance check ──────────────────────────────────────
        with self._lock:
            target = self._commanded_targets.get(rack_id)
        if target is not None:
            target_x, target_y = target
            self._check_tolerance(result, target_x, target_y)

        # ── Check 3: Stale-homing check ───────────────────────────────────
        with self._lock:
            stale_due = self._stale_check_due.pop(rack_id, False)
        if stale_due:
            self._check_stale_homing(result)

        # ── Recovery follow-up: did re-home succeed? ──────────────────────
        original_cmd: Optional[str] = None
        attempt = 0
        with self._lock:
            rec = self._recovery.get(rack_id)
            if rec and rec.in_recovery and result.all_homed:
                original_cmd = rec.original_command
                attempt = rec.recovery_attempt
                rec.in_recovery = False  # clear before retry

        if original_cmd is not None:
            logger.info(
                "rack=%s re-home complete after recovery (attempt %d) — "
                "retrying command: %s",
                rack_id, attempt, original_cmd,
            )
            self._retry_command(rack_id, original_cmd, attempt)

        return result

    def set_commanded_target(
        self,
        rack_id: str,
        target_x: Optional[float],
        target_y: Optional[float],
    ) -> None:
        """
        Record the intended position before publishing a move command so the
        next M114 response can run the tolerance check against it.

        Called by command_handler before publishing M700/M701-M704.
        Pass (None, None) to clear the target after a successful move.
        """
        with self._lock:
            if target_x is None and target_y is None:
                self._commanded_targets.pop(rack_id, None)
            else:
                self._commanded_targets[rack_id] = (target_x, target_y)

    def mark_stale_check_due(self, rack_id: str) -> None:
        """
        Signal that the next M114 for this rack should run the stale-homing
        check.  Called when a Pi comes back online (BRIDGE_RECONNECTED).
        """
        with self._lock:
            self._stale_check_due[rack_id] = True

    def trigger_recovery(
        self,
        rack_id: str,
        reason: str,
        original_command: Optional[str] = None,
    ) -> None:
        """
        Manually trigger the automatic recovery sequence from outside this
        module (e.g. from the pending-command escalation in Section 4.5).
        """
        self._start_recovery(rack_id, reason, original_command)

    # ── M114 parsing ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_m114(rack_id: str, raw: str) -> Optional[M114Result]:
        """Parse a raw M114 line; return None if the line is not M114 format."""
        if not isinstance(raw, str):
            try:
                raw = str(raw)
            except Exception:
                return None

        m = _M114_RE.search(raw)
        if not m:
            return None

        return M114Result(
            rack_id=rack_id,
            x=float(m.group("x")),
            y=float(m.group("y")),
            c=float(m.group("c")),
            homed_x=(m.group("hx").upper() == "Y"),
            homed_y=(m.group("hy").upper() == "Y"),
            homed_c=(m.group("hc").upper() == "Y"),
        )

    # ── State update ──────────────────────────────────────────────────────────

    def _update_state(self, r: M114Result) -> None:
        """Update the in-memory gantry_state from a parsed M114."""
        kwargs: dict = {
            "last_position_x": r.x,
            "last_position_y": r.y,
            "last_position_c": r.c,
            "homed_x": r.homed_x,
            "homed_y": r.homed_y,
            "homed_c": r.homed_c,
        }
        if r.all_homed:
            kwargs["last_homed_at"] = datetime.utcnow()
        gantry_state.upsert(r.rack_id, **kwargs)

    def _persist_position(self, r: M114Result) -> None:
        """Write position + homing flags to the DB asynchronously."""
        try:
            from db.database import db_session
            from db.models import Rack
            with db_session() as db:
                rack = db.query(Rack).filter_by(id=r.rack_id).first()
                if rack is None:
                    return
                rack.last_position_x = r.x
                rack.last_position_y = r.y
                rack.last_position_c = r.c
                rack.homed_x = r.homed_x
                rack.homed_y = r.homed_y
                rack.homed_c = r.homed_c
                if r.all_homed:
                    rack.last_homed_at = datetime.utcnow()
        except Exception:
            logger.exception("_persist_position failed for rack=%s", r.rack_id)

    # ── Check 2: Tolerance check ──────────────────────────────────────────────

    def _check_tolerance(
        self,
        r: M114Result,
        target_x: Optional[float],
        target_y: Optional[float],
    ) -> None:
        """Compare reported position to commanded target; start recovery on failure."""
        tol_x = settings.POSITION_TOLERANCE_X_MM
        tol_y = settings.POSITION_TOLERANCE_Y_MM
        try:
            from db.database import db_session
            from db.models import Rack
            with db_session() as db:
                rack = db.query(Rack).filter_by(id=r.rack_id).first()
                if rack:
                    tol_x = rack.position_tolerance_x_mm
                    tol_y = rack.position_tolerance_y_mm
        except Exception:
            pass  # Use defaults on DB error

        err_x = abs(r.x - target_x) if target_x is not None else 0.0
        err_y = abs(r.y - target_y) if target_y is not None else 0.0

        if err_x > tol_x or err_y > tol_y:
            logger.error(
                "rack=%s POSITION TOLERANCE EXCEEDED — "
                "target=(%.2f, %.2f) actual=(%.2f, %.2f) "
                "error=(%.2f, %.2f) tolerance=(%.2f, %.2f)",
                r.rack_id,
                target_x or 0.0, target_y or 0.0,
                r.x, r.y,
                err_x, err_y,
                tol_x, tol_y,
            )
            with self._lock:
                self._commanded_targets.pop(r.rack_id, None)

            threading.Thread(
                target=self._write_position_error_audit,
                args=(r, target_x, target_y, err_x, err_y),
                daemon=True,
                name=f"audit-pos-{r.rack_id}",
            ).start()

            self._start_recovery(r.rack_id, "tolerance_failure", original_command=None)

    def _write_position_error_audit(
        self,
        r: M114Result,
        target_x: Optional[float],
        target_y: Optional[float],
        err_x: float,
        err_y: float,
    ) -> None:
        try:
            from db.database import db_session
            from db.models import AuditLog
            with db_session() as db:
                db.add(AuditLog(
                    event_type="position_error",
                    rack_id=r.rack_id,
                    outcome="failure",
                    details=json.dumps({
                        "actual_x": r.x, "actual_y": r.y,
                        "target_x": target_x, "target_y": target_y,
                        "error_x": err_x, "error_y": err_y,
                    }),
                    created_at=datetime.utcnow(),
                ))
        except Exception:
            logger.exception("_write_position_error_audit failed for rack=%s", r.rack_id)

    # ── Check 3: Stale-homing check ───────────────────────────────────────────

    def _check_stale_homing(self, r: M114Result) -> None:
        """
        If last_homed_at is null or older than STALE_HOMING_THRESHOLD_HOURS,
        notify the operator via WebSocket to home before proceeding.
        """
        state = gantry_state.get(r.rack_id)
        last_homed = state.last_homed_at if state else None

        stale = (
            last_homed is None
            or (datetime.utcnow() - last_homed)
               > timedelta(hours=STALE_HOMING_THRESHOLD_HOURS)
        )
        if not stale:
            return

        logger.warning(
            "rack=%s stale-homing detected (last_homed_at=%s) — "
            "prompting operator to home before motion.",
            r.rack_id, last_homed,
        )

        try:
            from api.websocket import ws_registry
            # FIX (Mismatch 2): wrapped in data:{level, code, message}
            ws_registry.broadcast_from_thread(
                r.rack_id,
                _alert_msg(
                    rack_id=r.rack_id,
                    level="warning",
                    code="stale_homing",
                    message=(
                        f"Gantry not homed recently "
                        f"(last homed: {last_homed or 'never'}). "
                        "Please run G28 before issuing motion commands."
                    ),
                ),
            )
        except Exception:
            logger.exception("Failed to send stale_homing alert for rack=%s", r.rack_id)

    # ── Recovery sequence (Section 4.4) ───────────────────────────────────────

    def _start_recovery(
        self,
        rack_id: str,
        reason: str,
        original_command: Optional[str],
    ) -> None:
        """
        Automatic recovery sequence:
          1. Publish ! (QoS 2)  — stop all motion immediately.
          2. Publish G28 (QoS 1) — full homing sequence.
          3. Alert the operator via WebSocket ("re-homing").
          4. Record the original_command for retry on successful re-home.
          5. If attempt >= 2: escalate to maintenance_required.
        """
        with self._lock:
            rec = self._recovery.setdefault(rack_id, RecoveryState())
            if rec.in_recovery:
                logger.warning(
                    "rack=%s recovery already in progress — ignoring new trigger (%s)",
                    rack_id, reason,
                )
                return
            rec.in_recovery = True
            rec.recovery_attempt += 1
            rec.original_command = original_command
            rec.triggered_at = datetime.utcnow()
            attempt = rec.recovery_attempt

        logger.error(
            "rack=%s RECOVERY TRIGGERED (reason=%s attempt=%d original_cmd=%r)",
            rack_id, reason, attempt, original_command,
        )

        try:
            from services.mqtt_client import mqtt_client
            mqtt_client.publish_emergency(rack_id)
            mqtt_client.publish_command(rack_id, "G28")
        except Exception:
            logger.exception("Recovery: failed to publish ! / G28 for rack=%s", rack_id)

        try:
            from api.websocket import ws_registry
            # FIX (Mismatch 2): wrapped in data:{level, code, message}
            ws_registry.broadcast_from_thread(
                rack_id,
                _alert_msg(
                    rack_id=rack_id,
                    level="warning",
                    code="re_homing",
                    message=(
                        f"Position error detected ({reason}). "
                        "Automatic re-home initiated. "
                        "Gantry status: re-homing."
                    ),
                ),
            )
        except Exception:
            logger.exception(
                "Recovery: failed to broadcast re_homing alert for rack=%s", rack_id
            )

        threading.Thread(
            target=self._write_recovery_audit,
            args=(rack_id, reason, attempt),
            daemon=True,
            name=f"audit-rec-{rack_id}",
        ).start()

        if attempt >= 2:
            self._escalate_to_maintenance(rack_id)

    def _retry_command(self, rack_id: str, command: str, attempt: int) -> None:
        """Re-publish the original command after a successful re-home."""
        if not command:
            return
        try:
            from services.mqtt_client import mqtt_client
            mqtt_client.publish_command(rack_id, command)
            logger.info(
                "rack=%s retrying command %r after re-home (attempt %d)",
                rack_id, command, attempt,
            )
        except Exception:
            logger.exception(
                "Failed to retry command %r for rack=%s after re-home", command, rack_id
            )

    def _escalate_to_maintenance(self, rack_id: str) -> None:
        """
        Escalation ladder step L3 (Section 4.5):
          racks.maintenance_required = True
          scan_schedule.enabled = False
          admin alert over /ws
        """
        logger.error(
            "rack=%s ESCALATED TO MAINTENANCE_REQUIRED — "
            "position error recurred after re-home. Auto-scan disabled.",
            rack_id,
        )

        try:
            from db.database import db_session
            from db.models import AuditLog, Rack, ScanSchedule

            with db_session() as db:
                rack = db.query(Rack).filter_by(id=rack_id).first()
                if rack:
                    rack.maintenance_required = True
                schedule = db.query(ScanSchedule).filter_by(rack_id=rack_id).first()
                if schedule:
                    schedule.enabled = False
                db.add(AuditLog(
                    event_type="maintenance_flagged",
                    rack_id=rack_id,
                    outcome="flagged",
                    details=json.dumps({"reason": "position_error_after_re_home"}),
                    created_at=datetime.utcnow(),
                ))
        except Exception:
            logger.exception(
                "_escalate_to_maintenance DB write failed for rack=%s", rack_id
            )

        gantry_state.upsert(rack_id, maintenance_required=True)

        try:
            from api.websocket import ws_registry
            # FIX (Mismatch 2): wrapped in data:{level, code, message}
            ws_registry.broadcast_from_thread(
                rack_id,
                _alert_msg(
                    rack_id=rack_id,
                    level="error",
                    code="maintenance_required",
                    message=(
                        "Position error recurred after re-home. "
                        "Rack marked maintenance_required. "
                        "Auto-scan disabled. Admin intervention required."
                    ),
                ),
            )
        except Exception:
            logger.exception(
                "Failed to broadcast maintenance_required alert for rack=%s", rack_id
            )

    def _write_recovery_audit(self, rack_id: str, reason: str, attempt: int) -> None:
        try:
            from db.database import db_session
            from db.models import AuditLog
            with db_session() as db:
                db.add(AuditLog(
                    event_type="re_home_triggered",
                    rack_id=rack_id,
                    outcome="success",
                    details=json.dumps({"reason": reason, "attempt": attempt}),
                    created_at=datetime.utcnow(),
                ))
        except Exception:
            logger.exception("_write_recovery_audit failed for rack=%s", rack_id)


# ── Module-level singleton ────────────────────────────────────────────────────
position_monitor = PositionMonitor()
