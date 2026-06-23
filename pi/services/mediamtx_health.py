"""
pi/services/mediamtx_health.py

MediaMTX health-check agent — Section 5.6.

Periodically checks whether the MediaMTX process is running and the configured
stream path is reachable, then reports camera_status in the bridge heartbeat.

Two check strategies are available:

  1. systemctl (preferred on a real Pi):
     `systemctl is-active vivarium-camera` — checks the systemd unit state.
     Returns "active" when healthy, anything else when not.

  2. MediaMTX HTTP API (fallback and for local testing without systemd):
     GET http://127.0.0.1:9997/v3/paths/list
     Checks that the response is 200 AND the expected path name is present
     in the JSON. Works on any OS including Windows/macOS for dev/CI.

Strategy selection (in priority order):
  a. GANTRY_CAMERA_HEALTH_STRATEGY env var: "systemctl" | "api" | "auto"
  b. "auto" (default): try systemctl first; if systemd is not available or
     the unit doesn't exist, fall back to the HTTP API check.

For local testing without a real MediaMTX instance running:
  - Set GANTRY_MOCK_CAMERA=1 (same env var used by camera_handler.py).
    mediamtx_health always reports "online" and skips both checks.

Usage from bridge.py heartbeat:
    from services.mediamtx_health import mediamtx_health
    camera_status = mediamtx_health.check()   # returns "online" | "offline"
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

# Name of the MediaMTX systemd unit (matches vivarium-camera.service)
_SYSTEMD_UNIT = "vivarium-camera.service"

# MediaMTX REST API base URL — bound to localhost only (see mediamtx.example.yaml)
_MEDIAMTX_API_BASE = "http://127.0.0.1:9997"

# Timeout for the HTTP API check (seconds)
_HTTP_TIMEOUT_S = 3

# How often to re-check camera health in the background polling thread
_DEFAULT_POLL_INTERVAL_S = 30.0


class MediaMTXHealth:
    """
    Singleton health-check service for the MediaMTX streaming agent.

    Usage (bridge.py):
        from services.mediamtx_health import mediamtx_health
        # One-shot check (called from heartbeat)
        status = mediamtx_health.check()
        # Start a background polling thread (optional)
        mediamtx_health.start_background_polling()
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
            "online"  — MediaMTX is running and the stream path is present
            "offline" — MediaMTX unit is not active or path is absent
            "unknown" — check could not be performed (no systemd, API not up)

        Synchronous and safe to call from any thread.
        Updates the cached status returned by last_status.
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
            name="mediamtx-health",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info(
            "MediaMTX health polling started (strategy=%s interval=%.0fs)",
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
                logger.exception("MediaMTX health check error")
            self._stop_event.wait(interval_s)
        logger.info("MediaMTX health polling stopped.")

    # ── Strategy 1: systemctl ─────────────────────────────────────────────────

    def _check_systemctl(self) -> CameraStatus:
        """
        Check the vivarium-camera.service unit state using
        `systemctl is-active <unit>`.

        Returns "online" if active, "offline" if failed/inactive,
        "unknown" if systemctl is not available (non-systemd environment).
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
            # systemctl not found — not a systemd system (macOS, Windows, dev)
            logger.debug("systemctl not found — falling back to API check")
            return self._set("unknown")
        except subprocess.TimeoutExpired:
            logger.warning("systemctl is-active timed out — camera status unknown")
            return self._set("unknown")
        except Exception as exc:
            logger.warning("systemctl check failed: %s", exc)
            return self._set("unknown")

    # ── Strategy 2: MediaMTX HTTP API ─────────────────────────────────────────

    def _check_api(self) -> CameraStatus:
        """
        Check the MediaMTX REST API at GET /v3/paths/list.

        The response is a JSON object:
            { "items": [ {"name": "...", "ready": true/false, ...}, ... ] }

        Checks that:
          1. The request returns HTTP 200.
          2. The path name matching settings.stream_name is present.
          3. The path's 'ready' flag is True.

        With sourceOnDemand:true in mediamtx.yaml, MediaMTX registers the
        path at startup but sets ready=false until an active viewer connects
        (at which point the camera hardware is opened). The heartbeat runs
        every 30 s when nobody is watching, so requiring ready=true would
        always report camera_status='offline' even when MediaMTX is running
        perfectly and the camera is healthy.

        Fix: treat path-present (regardless of ready flag) as "online" —
        the service is up and the stream will start on demand. Only return
        "offline" when the path is absent entirely or the API is unreachable.
        """
        try:
            import urllib.request
            import json as _json

            url = f"{_MEDIAMTX_API_BASE}/v3/paths/list"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                if resp.status != 200:
                    logger.debug("MediaMTX API returned HTTP %d", resp.status)
                    return self._set("offline")

                body = _json.loads(resp.read().decode())

            # body: { "itemCount": N, "pageCount": N, "items": [{"name": "...", "ready": bool}, ...] }
            items = body.get("items", [])
            expected = settings.stream_name or settings.device_id

            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("name", "") == expected:
                    # Path registered = MediaMTX is running and configured correctly.
                    # ready=False just means sourceOnDemand and no active viewer —
                    # that's normal when nobody is watching. The camera will open
                    # on demand when a WHEP viewer connects.
                    logger.debug(
                        "MediaMTX API: path %r present (ready=%s) → online",
                        expected, item.get("ready"),
                    )
                    return self._set("online")

            # Path not found at all
            path_names = [i.get("name") for i in items if isinstance(i, dict)]
            if path_names:
                logger.debug(
                    "MediaMTX API: path %r not found in %s → offline",
                    expected, path_names,
                )
            else:
                logger.debug("MediaMTX API: running but no paths configured")
            return self._set("offline")

        except OSError as exc:
            # Connection refused / timeout — MediaMTX not running
            logger.debug("MediaMTX API not reachable: %s", exc)
            return self._set("offline")
        except Exception as exc:
            logger.warning("MediaMTX API check error: %s", exc)
            return self._set("unknown")

    # ── Strategy resolution ───────────────────────────────────────────────────

    @staticmethod
    def _resolve_strategy() -> HealthStrategy:
        """
        Determine the health check strategy from the environment variable
        GANTRY_CAMERA_HEALTH_STRATEGY.

        Values:
          "systemctl"  — use systemctl is-active only
          "api"        — use MediaMTX HTTP API only
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
# bridge.py imports:  from services.mediamtx_health import mediamtx_health
mediamtx_health = MediaMTXHealth()
