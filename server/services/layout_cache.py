"""
server/services/layout_cache.py

Short-lived in-memory cache for rack layout query responses (M705/M706/M707).

When GET /rack/{rack_id}/layout is called with ?live=true, the server publishes
M705/M706/M707 to MQTT and waits up to 5s for the Pi to relay the Arduino's
responses. This cache bridges that async gap.

Mirrors the same pattern as serial_manager.py's response_cache dict in the
sample code, but adapted for the MQTT-async server architecture.

Usage:
    from services.layout_cache import layout_cache

    # Publisher side (in _on_response_message, main.py):
    layout_cache.set(rack_id, "M705", raw_line)

    # Consumer side (in GET /rack/{rack_id}/layout route):
    rows_line = layout_cache.wait_for(rack_id, "M705", timeout_s=5.0)
"""

from __future__ import annotations

import threading
import time
import logging
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# Default TTL for cached layout responses (seconds)
_DEFAULT_TTL_S: float = 10.0
# Default poll timeout for wait_for()
_DEFAULT_TIMEOUT_S: float = 5.0
# Poll interval inside wait_for()
_POLL_INTERVAL_S: float = 0.05


class LayoutCache:
    """
    Thread-safe short-lived key-value store for layout query responses.

    Keys are (rack_id, response_key) tuples where response_key is one of:
        "M705"  — ROWS= COLS= line
        "M706"  — Pitch X= Y= line
        "M707"  — Offsets X0= Y0= line

    Values expire after _DEFAULT_TTL_S to avoid stale data from old queries.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Structure: { rack_id: { key: (value, expiry_time) } }
        self._store: Dict[str, Dict[str, Tuple[str, float]]] = {}

    def set(
        self,
        rack_id: str,
        key: str,
        value: str,
        ttl_s: float = _DEFAULT_TTL_S,
    ) -> None:
        """
        Store a response value for (rack_id, key) with a TTL.

        Called from the MQTT response handler in main.py when a layout
        response line arrives from the Pi.
        """
        expiry = time.monotonic() + ttl_s
        with self._lock:
            if rack_id not in self._store:
                self._store[rack_id] = {}
            self._store[rack_id][key] = (value, expiry)
        logger.debug("LayoutCache.set: rack=%s key=%s ttl=%.1fs", rack_id, key, ttl_s)

    def get(self, rack_id: str, key: str) -> Optional[str]:
        """
        Return the cached value for (rack_id, key), or None if absent/expired.
        Expired entries are evicted on read.
        """
        with self._lock:
            rack_cache = self._store.get(rack_id)
            if rack_cache is None:
                return None
            entry = rack_cache.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if time.monotonic() > expiry:
                del rack_cache[key]
                logger.debug("LayoutCache.get: rack=%s key=%s EXPIRED", rack_id, key)
                return None
            return value

    def wait_for(
        self,
        rack_id: str,
        key: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> Optional[str]:
        """
        Poll until the value for (rack_id, key) is available, or timeout_s elapses.

        Returns the value string or None on timeout.
        Called by GET /rack/{rack_id}/layout?live=true after publishing M705/M706/M707.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            v = self.get(rack_id, key)
            if v is not None:
                return v
            time.sleep(_POLL_INTERVAL_S)
        logger.warning(
            "LayoutCache.wait_for: TIMEOUT rack=%s key=%s after %.1fs",
            rack_id, key, timeout_s,
        )
        return None

    def clear(self, rack_id: str) -> None:
        """Remove all cached entries for a rack (e.g. before a fresh query)."""
        with self._lock:
            self._store.pop(rack_id, None)

    def evict_expired(self) -> None:
        """Remove all expired entries across all racks. Call periodically if needed."""
        now = time.monotonic()
        with self._lock:
            for rack_id in list(self._store.keys()):
                rack_cache = self._store[rack_id]
                expired_keys = [k for k, (_, exp) in rack_cache.items() if now > exp]
                for k in expired_keys:
                    del rack_cache[k]
                if not rack_cache:
                    del self._store[rack_id]


# Module-level singleton
layout_cache = LayoutCache()
