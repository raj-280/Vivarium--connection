"""
pi/services/go2rtc_health.py

go2rtc health-check agent — Section 5.6.

Periodically checks whether the go2rtc process is running and the configured
stream is reachable, then reports camera_status in the bridge heartbeat.

Two check strategies are available:

  1. systemctl (preferred on a real Pi):
     `systemctl is-active vivarium-camera` — checks the systemd unit state.
     Returns "active" when healthy, anything else when not.

  2. go2rtc HTTP API (fallback and for local testing without systemd):
     GET http://127.0.0.1:1984/api/streams  (from go2rtc.example.yaml [api])
     Checks that the response is 200 AND the expected stream name is present
     in the JSON. This works on any OS, including Windows/macOS for dev/CI.

Strategy selection (in priority order):
  a. GANTRY_CAMERA_HEALTH_STRATEGY env var: "systemctl" | "api" | "auto"
  b. "auto" (default): try systemctl first; if systemd is not available or
     the unit doesn't exist, fall back to the HTTP API check.

For local testing without a real go2rtc instance running:
  - Set GANTRY_MOCK_CAMERA=1 (same env var used by camera_handler.py).
    go2rtc_health always reports "online" and skips both checks.

Usage from bridge.py heartbeat:
    from services.go2rtc_health import go2rtc_health
    camera_status = go2rtc_health.check()   # returns "online" | "offline"

The Bridge._publish_heartbeat() method is updated in this stage to call this.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# ── Import resolution ─────────────────────────────────────────────────────────
try:
    from config.settings import settings          # python pi/...  (pi/ on sys.path)
except ImportError:
    from pi.config.settings import settings       # python -m pi.bridge


# ── Types ─────────────────────────────────────────────────────────────────────

CameraStatus = Literal["online", "offline", "unknown"]
HealthStrategy = Literal["systemctl", "api", "auto"]


# ── Constants ─────────────────────────────────────────────────────────────────

# Name of the go2rtc systemd unit (must match the .service file name)
_SYSTEMD_UNIT = "vivarium-camera.service"

# go2rtc HTTP API base URL — from go2rtc.example.yaml [api] listen address
# and pi/config/settings.py (no dedicated setting key yet; hard-coded to the
# documented default of 127.0.0.1:1984).  If you change the port in the YAML,
# update this constant to match.
_GO2RTC_API_BASE = "http://127.0.0.1:1984"

# Timeout for the HTTP API check (seconds)
_HTTP_TIMEOUT_S = 3

# How often to re-check camera health when running the background thread
_DEFAULT_POLL_INTERVAL_S = 30.0


class Go2RTCHealth:
    """
    Singleton health-check service for the go2rtc streaming agent.

    Usage (bridge.py):
        from services.go2rtc_health import go2rtc_health
        # One-shot check (called from heartbeat)
        status = go2rtc_health.check()
        # Start a background polling thread (optional)
        go2rtc_health.start_background_polling()
    """

    def __init__(self) -> None:
        self._last_status: CameraStatus = "unknown"
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        self._strategy: HealthStrategy = self._resolve_strategy()
        self._mock = bool(os.environ.get("GANTRY_MOCK_CAMERA"))

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self) -> CameraStatus:
        """
        Run a single health check and return the camera_status string.

        Returns:
            "online"  — go2rtc is running and the stream is present
            "offline" — go2rtc unit is not active or stream is absent
            "unknown" — check could not be performed (no systemd, API not up)

        This method is synchronous and safe to call from any thread.
        It updates the cached status returned by last_status.
        """
        if self._mock:
            return self._set("online")

        if self._strategy == "systemctl":
            return self._check_systemctl()
        elif self._strategy == "api":
            return self._check_api()
        else:
            # "auto": try systemctl first, fall back to API
            status = self._check_systemctl()
            if status == "unknown":
                status = self._check_api()
            return status

    @property
    def last_status(self) -> CameraStatus:
        """Return the most recently cached status (no I/O)."""
        with self._lock:
            return self._last_status

    def start_background_polling(
        self,
        interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        """
        Start a daemon thread that calls check() every interval_s seconds.

        Results are cached in self._last_status so bridge.py can read them
        cheaply in the heartbeat without blocking on I/O.

        Safe to call more than once — only starts one thread.
        """
        with self._lock:
            if self._poll_thread and self._poll_thread.is_alive():
                return

        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            args=(interval_s,),
            name="go2rtc-health",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info(
            "go2rtc health polling started (strategy=%s interval=%.0fs)",
            self._strategy, interval_s,
        )

    def stop_background_polling(self) -> None:
        """Signal the background thread to exit cleanly."""
        self._stop_event.set()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _set(self, status: CameraStatus) -> CameraStatus:
        """Update cached status and return it."""
        with self._lock:
            self._last_status = status
        return status

    def _poll_loop(self, interval_s: float) -> None:
        while not self._stop_event.is_set():
            try:
                self.check()
            except Exception:
                logger.exception("go2rtc health check error")
            self._stop_event.wait(interval_s)
        logger.info("go2rtc health polling stopped.")

    # ── Strategy 1: systemctl ─────────────────────────────────────────────────

    def _check_systemctl(self) -> CameraStatus:
        """
        Check the vivarium-camera.service unit state using
        `systemctl is-active <unit>`.

        Returns "online" if the unit reports "active", "offline" if it reports
        anything else (failed, inactive, activating, etc.), or "unknown" if
        systemctl is not available (non-Linux / non-systemd environment).
        """
        try:
            result = subprocess.run(
                ["systemctl", "is-active", _SYSTEMD_UNIT],
                capture_output=True,
                text=True,
                timeout=5,
            )
            state = result.stdout.strip().lower()
            logger.debug("systemctl is-active %s: %r", _SYSTEMD_UNIT, state)
            status: CameraStatus = "online" if state == "active" else "offline"
            return self._set(status)

        except FileNotFoundError:
            # systemctl not found — not a systemd system (macOS, Windows, WSL2 dev)
            logger.debug("systemctl not found — falling back to API check")
            return self._set("unknown")
        except subprocess.TimeoutExpired:
            logger.warning("systemctl is-active timed out — camera status unknown")
            return self._set("unknown")
        except Exception as exc:
            logger.warning("systemctl check failed: %s", exc)
            return self._set("unknown")

    # ── Strategy 2: go2rtc HTTP API ───────────────────────────────────────────

    def _check_api(self) -> CameraStatus:
        """
        Check the go2rtc HTTP API at GET /api/streams.

        The response is a JSON object whose keys are stream names.
        Checks that:
          1. The request returns HTTP 200.
          2. The stream name matching settings.go2rtc_stream_name is present.

        Falls back to "unknown" if the API is not reachable.
        Falls back gracefully to checking any stream is present if
        settings.go2rtc_stream_name is not configured.
        """
        try:
            import urllib.request  # stdlib — no extra dependency
            import json as _json

            url = f"{_GO2RTC_API_BASE}/api/streams"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                if resp.status != 200:
                    logger.debug("go2rtc API returned HTTP %d", resp.status)
                    return self._set("offline")

                body = _json.loads(resp.read().decode())

            # body is a dict: { "<stream_name>": { ... }, ... }
            expected_stream = settings.go2rtc_stream_name or settings.device_id
            if not isinstance(body, dict):
                logger.debug("go2rtc /api/streams returned unexpected type: %r", type(body))
                return self._set("unknown")

            if expected_stream and expected_stream in body:
                logger.debug("go2rtc API: stream %r present → online", expected_stream)
                return self._set("online")

            # If stream name not configured or missing, any non-empty response
            # means go2rtc is alive (though not necessarily streaming our rack)
            if body:
                logger.debug(
                    "go2rtc API: stream %r not found in %s, but API alive",
                    expected_stream, list(body.keys()),
                )
                return self._set("offline")

            # Empty stream dict — go2rtc running but no streams configured
            logger.debug("go2rtc API: running but no streams configured")
            return self._set("offline")

        except OSError as exc:
            # Connection refused / timeout — go2rtc not running
            logger.debug("go2rtc API not reachable: %s", exc)
            return self._set("offline")
        except Exception as exc:
            logger.warning("go2rtc API check error: %s", exc)
            return self._set("unknown")

    # ── Strategy resolution ───────────────────────────────────────────────────

    @staticmethod
    def _resolve_strategy() -> HealthStrategy:
        """
        Determine the health check strategy from the environment variable
        GANTRY_CAMERA_HEALTH_STRATEGY.

        Values:
          "systemctl"  — use systemctl is-active only
          "api"        — use go2rtc HTTP API only
          "auto"       — try systemctl; fall back to API (default)
        """
        raw = os.environ.get("GANTRY_CAMERA_HEALTH_STRATEGY", "auto").lower().strip()
        if raw in ("systemctl", "api", "auto"):
            return raw  # type: ignore[return-value]
        logger.warning(
            "Unknown GANTRY_CAMERA_HEALTH_STRATEGY=%r — using 'auto'", raw
        )
        return "auto"


# ── Module-level singleton ────────────────────────────────────────────────────
# bridge.py imports this:  from services.go2rtc_health import go2rtc_health
go2rtc_health = Go2RTCHealth()
