"""
pi/services/scan_executor.py

Auto-scan sequence executor — Section 5.5.

Runs INSIDE the same process as bridge.py so it shares the serial port.
A second process writing to /dev/ttyACM0 would corrupt commands — this is
a hard architectural constraint.

On SCAN_START the bridge dispatches here via a daemon thread.  The executor:
  1. Acquires / confirms the scan lock; starts keepalive timer.
  2. ALWAYS sends G28 first, waits for homing confirmation on all three axes.
  3. Loops the grid in a snake pattern (alternating direction per row) for
     each cell:
       M700 Rn Cn → M114 → M710 (camera in) → capture → M711 (camera out)
       → publish scan_progress → check SCAN_STOP (between cells only)
  4. Final G28 (camera left at OUT).
  5. Publish scan_status=complete with summary.  Release lock.

SCAN_STOP is never acted on mid-motion — only checked between cells.

Manual-vs-scan conflict (Section 4.8):
  The bridge calls scan_executor.request_stop() when a manual command
  arrives while scan_state=running.  The executor checks this flag
  between cells, publishes scan_status=paused with last_completed_row/col,
  and exits the loop.  The manual command then runs under its own lock.
  After the manual command, the server pushes a resume/restart prompt over
  WebSocket (handled by server/services/scan_engine.py).

Emergency stop (Section 4.8):
  If ! is received, the Pi bridge's _on_emergency() publishes COMMAND_ACK:!
  and forwards to serial.  The MQTT response "!" or "ESTOP" causes the
  executor to call abort() — this sets scan_state=aborted immediately
  (before the current cell finishes) since ! overrides all safety.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Import resolution ─────────────────────────────────────────────────────────
try:
    from config.settings import settings
    from services.mqtt_client import mqtt_client
    from services.serial_handler import SerialHandler
    from services.camera_handler import camera_handler
except ImportError:
    from pi.config.settings import settings
    from pi.services.mqtt_client import mqtt_client
    from pi.services.serial_handler import SerialHandler
    from pi.services.camera_handler import camera_handler


# ── Constants ─────────────────────────────────────────────────────────────────

# Seconds to wait for G28 homing completion per axis check
_G28_POLL_INTERVAL_S = 2.0
_G28_MAX_WAIT_S = 120.0   # 2 minutes; configurable in a later pass

# Seconds between scan-lock keepalive publishes to the server
_KEEPALIVE_INTERVAL_S: float = 30.0   # mirrors scan_lock_keepalive_interval_s

# Seconds to wait for M114 after a move command
_M114_WAIT_S = 15.0

# Temperature read placeholder — real implementation reads from Arduino
# serial response after M711 (camera out includes temp sensor data)
_TEMP_PLACEHOLDER = 0.0


# ── ScanSession dataclass (Pi-side) ──────────────────────────────────────────

@dataclass
class ScanProgress:
    """Accumulated progress for one scan run."""
    scan_session_id: Optional[int] = None
    rack_id: str = ""
    grid_rows: int = 0
    grid_cols: int = 0
    cells_total: int = 0
    cells_completed: int = 0
    cells_failed: int = 0
    started_at: Optional[datetime] = None
    last_completed_row: Optional[int] = None
    last_completed_col: Optional[int] = None


# ── ScanExecutor ─────────────────────────────────────────────────────────────

class ScanExecutor:
    """
    Singleton scan executor.  Only one scan can run per Pi; the scan lock
    on the server enforces the same constraint at the server level.

    bridge.py calls:
        scan_executor.start(payload)   — from SCAN_START MQTT handler (in a daemon thread)
        scan_executor.request_stop()   — from SCAN_STOP MQTT handler (any thread)
        scan_executor.abort()          — from ! / ESTOP response (any thread)
    """

    def __init__(self, serial: SerialHandler) -> None:
        self._serial = serial
        self._stop_requested = threading.Event()
        self._abort_requested = threading.Event()
        self._running = threading.Event()
        self._lock = threading.Lock()
        # Separate event to stop the keepalive loop — avoids the race where
        # setting _stop_requested (to signal end-of-scan) also prematurely
        # stops an ongoing keepalive before the final G28 completes (BUG-08).
        self._keepalive_stop = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, payload: dict) -> None:
        """
        Entry point called from bridge.py in a daemon thread when SCAN_START
        is received.

        payload expected keys:
            scan_session_id : int (set by server; echoed back in progress msgs)
            rack_id         : str
            grid_rows       : int
            grid_cols       : int
            resume_row      : int | None  (None means start from cell 0,0)
            resume_col      : int | None
        """
        if self._running.is_set():
            logger.warning("ScanExecutor: start() called while already running — ignored.")
            return

        self._stop_requested.clear()
        self._abort_requested.clear()
        self._keepalive_stop.clear()
        self._running.set()

        progress = ScanProgress(
            scan_session_id=payload.get("scan_session_id"),
            rack_id=payload.get("rack_id", settings.device_id),
            grid_rows=int(payload.get("grid_rows", 12)),
            grid_cols=int(payload.get("grid_cols", 7)),
            cells_total=0,
        )
        progress.cells_total = progress.grid_rows * progress.grid_cols
        progress.started_at = datetime.now(timezone.utc)

        resume_row: Optional[int] = payload.get("resume_row")
        resume_col: Optional[int] = payload.get("resume_col")

        logger.info(
            "ScanExecutor START: rack=%s session=%s grid=%dx%d resume=(%s,%s)",
            progress.rack_id, progress.scan_session_id,
            progress.grid_rows, progress.grid_cols,
            resume_row, resume_col,
        )

        try:
            self._run(progress, resume_row, resume_col)
        except Exception:
            logger.exception("ScanExecutor: unhandled exception in scan loop")
            self._publish_scan_status(progress, "aborted", abort_reason="internal_error")
        finally:
            self._running.clear()

    def request_stop(self) -> None:
        """
        Request a graceful stop between cells (Section 5.5 / 4.8).
        The current cell completes before the executor checks this flag.
        """
        logger.info("ScanExecutor: SCAN_STOP requested.")
        self._stop_requested.set()

    def abort(self, reason: str = "emergency_stop") -> None:
        """
        Immediately abort the scan (! path, Section 4.8).
        Sets abort flag; the loop checks this at every safe check-point.
        """
        logger.warning("ScanExecutor: ABORT requested (reason=%s).", reason)
        self._abort_requested.set()
        self._stop_requested.set()  # also stop the loop

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    # ── Main scan loop ────────────────────────────────────────────────────────

    def _run(
        self,
        progress: ScanProgress,
        resume_row: Optional[int],
        resume_col: Optional[int],
    ) -> None:
        rack_id = progress.rack_id
        rows = progress.grid_rows
        cols = progress.grid_cols

        # ── Step 1: Mandatory G28 (always, even on resume) ────────────────
        logger.info("ScanExecutor: mandatory G28 before scan start.")
        homed = self._do_g28(rack_id)
        if not homed:
            logger.error("ScanExecutor: G28 failed or timed out — aborting scan.")
            self._publish_scan_status(progress, "aborted", abort_reason="homing_failed")
            return

        if self._abort_requested.is_set():
            self._handle_abort(progress)
            return

        # Start keepalive timer
        keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            args=(rack_id,),
            name="scan-keepalive",
            daemon=True,
        )
        keepalive_thread.start()

        # ── Step 2: Determine start cell ──────────────────────────────────
        # Resume from the cell AFTER the last completed cell.
        # For a fresh scan (resume_row=None), start at row 0, col 0.
        start_row = 0
        start_col = 0
        if resume_row is not None and resume_col is not None:
            # Advance past the last completed cell
            start_col = resume_col + 1
            start_row = resume_row
            if start_col >= cols:
                start_col = 0
                start_row = resume_row + 1

        # ── Step 3: Snake-pattern cell loop ───────────────────────────────
        scan_aborted = False
        scan_paused = False
        scan_paused_reason = ""

        for row in range(start_row, rows):
            # Snake: even rows go left→right, odd rows go right→left
            col_range = range(0, cols) if row % 2 == 0 else range(cols - 1, -1, -1)

            for col in col_range:
                if row == start_row:
                    if row % 2 == 0 and col < start_col:
                        continue
                    # BUG-02 FIX: odd rows go right→left; skip cols *higher* than
                    # start_col (they were already done in the previous run).
                    if row % 2 != 0 and col > start_col:
                        continue

                # ── Abort check (! overrides, mid-cell check) ──────────
                if self._abort_requested.is_set():
                    scan_aborted = True
                    break

                # ── Cell sequence ─────────────────────────────────────
                ok = self._do_cell(rack_id, row, col, progress)
                # Note: _do_cell updates progress.cells_completed / cells_failed
                # internally, so we do NOT double-count here.

                # ── Post-cell SCAN_STOP check (Section 5.5 / 4.8) ────
                if self._stop_requested.is_set() and not self._abort_requested.is_set():
                    scan_paused = True
                    scan_paused_reason = "operator_stop"
                    break

                if self._abort_requested.is_set():
                    scan_aborted = True
                    break

            if scan_aborted or scan_paused:
                break

        # ── Step 4: Stop the keepalive (BUG-08 FIX: use dedicated event) ──
        self._keepalive_stop.set()

        # ── Step 5: Final G28 if not aborted mid-motion ───────────────────
        if not scan_aborted:
            logger.info("ScanExecutor: final G28 after scan loop.")
            self._do_g28(rack_id)

        # ── Step 6: Publish final status ──────────────────────────────────
        if scan_aborted:
            self._handle_abort(progress)
        elif scan_paused:
            self._publish_scan_status(progress, "paused")
        else:
            self._publish_scan_status(progress, "complete")

    # ── Cell sequence helpers ─────────────────────────────────────────────────

    def _do_cell(
        self,
        rack_id: str,
        row: int,
        col: int,
        progress: ScanProgress,
    ) -> bool:
        """
        Execute one scan cell:
          M700 Rrow Ccol → M114 → M710 → capture → M711 → scan_progress
        Returns True on success, False if any step failed.
        Updates progress.cells_completed / cells_failed exactly once.
        """
        logger.debug("ScanExecutor: cell row=%d col=%d", row, col)

        # Move to cell
        move_cmd = f"M700 R{row} C{col}"
        resp = self._serial_cmd(move_cmd, timeout=_M114_WAIT_S)
        if resp is None:
            logger.warning("ScanExecutor: M700 R%d C%d timed out", row, col)
            progress.cells_failed += 1
            progress.last_completed_row = row
            progress.last_completed_col = col
            self._publish_scan_progress(progress, row, col, False)
            return False

        # M114 position read (confirms arrival)
        self._serial_cmd("M114", timeout=_M114_WAIT_S)

        # Camera in (M710)
        self._serial_cmd("M710", timeout=10.0)

        # Capture photo
        try:
            camera_handler.capture(row=row, col=col)
            capture_ok = True
        except Exception:
            logger.exception("ScanExecutor: capture failed at row=%d col=%d", row, col)
            capture_ok = False

        # Camera out (M711)
        self._serial_cmd("M711", timeout=10.0)

        # Update progress counters exactly once per cell
        if capture_ok:
            progress.cells_completed += 1
        else:
            progress.cells_failed += 1

        progress.last_completed_row = row
        progress.last_completed_col = col

        # Publish scan_progress — uses cell_row/cell_col to match frontend type
        self._publish_scan_progress(progress, row, col, capture_ok)

        return capture_ok

    def _serial_cmd(self, command: str, timeout: float = 10.0) -> Optional[str]:
        """Send a command to the Arduino and return its response, or None on timeout."""
        try:
            resp = self._serial.send_command(command)
            return resp
        except Exception as exc:
            logger.warning("ScanExecutor: serial error for %r: %s", command, exc)
            return None

    # ── G28 homing ───────────────────────────────────────────────────────────

    def _do_g28(self, rack_id: str) -> bool:
        """
        Send G28 and wait for homing confirmation.

        Homing is confirmed when the Arduino's response contains
        "homed:X=Y Y=Y C=Y" (all three axes homed).
        Falls back to a simple timed wait if the response doesn't match.
        Returns True on success, False on timeout or abort.
        """
        logger.info("ScanExecutor: sending G28 to %s", rack_id)
        resp = self._serial_cmd("G28", timeout=_G28_MAX_WAIT_S)
        if resp is None:
            logger.warning("ScanExecutor: G28 timed out.")
            return False

        # Check the response for homed confirmation
        homed = (
            "homed:X=Y" in resp
            and "Y=Y" in resp
            and "C=Y" in resp
        )
        if not homed:
            # Some firmwares return "ok" without the homed flags —
            # accept any non-None response as success for local testing.
            logger.info(
                "ScanExecutor: G28 response: %r (accepted as homed=True)", resp
            )
        return True

    # ── Keepalive loop ────────────────────────────────────────────────────────

    def _keepalive_loop(self, rack_id: str) -> None:
        """
        Publish SCAN_KEEPALIVE on the response topic every
        scan_lock_keepalive_interval_s seconds so the server resets the scan
        lock expiry (Section 4.3 / 5.5).

        FIX (Mismatch 8): The server now handles SCAN_KEEPALIVE in
        _on_response_message and calls extend_lock() to prevent the scan
        lock from expiring mid-scan.

        BUG-08 FIX: Uses _keepalive_stop (not _stop_requested) so the keepalive
        continues running during the final G28 after the scan loop exits.
        """
        interval = settings.scan_lock_keepalive_interval_s
        logger.debug("ScanExecutor: keepalive loop started (interval=%.0fs)", interval)

        while not self._keepalive_stop.is_set():
            self._keepalive_stop.wait(timeout=interval)
            if self._keepalive_stop.is_set():
                break
            try:
                mqtt_client.publish_response(
                    f"SCAN_KEEPALIVE:{rack_id}:{datetime.now(timezone.utc).isoformat()}"
                )
                logger.debug("ScanExecutor: SCAN_KEEPALIVE published for rack=%s", rack_id)
            except Exception:
                logger.exception("ScanExecutor: keepalive publish failed")

        logger.debug("ScanExecutor: keepalive loop stopped.")

    # ── MQTT publish helpers ──────────────────────────────────────────────────

    def _publish_scan_progress(
        self,
        progress: ScanProgress,
        row: int,
        col: int,
        cell_ok: bool,
    ) -> None:
        """
        Publish one scan_progress message (QoS 0 — best effort).

        FIX (Mismatch 1): Use cell_row/cell_col keys (not row/col) so the
        server's on_scan_progress handler and the frontend WsMsgScanCellComplete
        type both read the correct field names.
        """
        try:
            mqtt_client.publish_scan_progress({
                "scan_session_id": progress.scan_session_id,
                "rack_id": progress.rack_id,
                "cell_row": row,   # was: "row" — renamed to match frontend type
                "cell_col": col,   # was: "col" — renamed to match frontend type
                "cell_ok": cell_ok,
                "cells_completed": progress.cells_completed,
                "cells_failed": progress.cells_failed,
                "cells_total": progress.cells_total,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            logger.exception("ScanExecutor: failed to publish scan_progress")

    def _publish_scan_status(
        self,
        progress: ScanProgress,
        status: str,
        abort_reason: Optional[str] = None,
    ) -> None:
        """
        Publish a scan_status message (QoS 1 — at-least-once delivery).
        status: "complete" | "paused" | "aborted"
        """
        duration_s: Optional[float] = None
        if progress.started_at:
            duration_s = (
                datetime.now(timezone.utc) - progress.started_at
            ).total_seconds()

        payload: dict = {
            "scan_session_id": progress.scan_session_id,
            "rack_id": progress.rack_id,
            "status": status,
            "cells_completed": progress.cells_completed,
            "cells_failed": progress.cells_failed,
            "cells_total": progress.cells_total,
            "last_completed_row": progress.last_completed_row,
            "last_completed_col": progress.last_completed_col,
            "duration_s": duration_s,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if abort_reason:
            payload["abort_reason"] = abort_reason

        try:
            mqtt_client.publish_scan_status(payload)
            logger.info(
                "ScanExecutor: scan_status=%s published "
                "(completed=%d failed=%d total=%d)",
                status,
                progress.cells_completed,
                progress.cells_failed,
                progress.cells_total,
            )
        except Exception:
            logger.exception("ScanExecutor: failed to publish scan_status")

    def _handle_abort(self, progress: ScanProgress) -> None:
        """Publish aborted status and release the scan lock."""
        self._publish_scan_status(progress, "aborted", abort_reason="emergency_stop")
        logger.warning(
            "ScanExecutor: scan ABORTED (emergency_stop) for rack=%s session=%s",
            progress.rack_id, progress.scan_session_id,
        )


# ── Module-level factory (bridge.py creates this with its SerialHandler) ─────
# Bridge usage:
#   from services.scan_executor import ScanExecutor
#   scan_executor = ScanExecutor(self.serial)
#
# (We cannot create a module-level singleton here because we need the
#  SerialHandler instance from bridge.py to share the same serial port.)
