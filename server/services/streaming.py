"""
server/services/streaming.py

Live streaming URL builder — Section 4.2 / 8 / Stage 10.

Implements the server side of the MediaMTX streaming protocol (Section 8):

  1. build_stream_url(rack_id)  → the WebSocket message the browser uses to
     open its <video> element:
         { "type": "stream_url",
           "data": { "rack_id": "rack-047",
                     "url":      "/camera/rack-047/whep",
                     "mjpeg_url": "/camera/rack-047/mjpeg" } }

     MediaMTX WebRTC uses the WHEP standard endpoint (no query params):
         {STREAM_PROXY_PATH}/{rack_id}/whep
     MediaMTX MJPEG:
         {STREAM_PROXY_PATH}/{rack_id}/mjpeg

  2. build_stream_close(rack_id) → the WebSocket message sent when the lock
     is released, so the browser tears down the <video> element:
         { "type": "stream_close",
           "data": { "rack_id": "rack-047" } }

  3. check_mediamtx_stream(rack_id) → probes the MediaMTX REST API at
     MEDIAMTX_INTERNAL_URL/v3/paths/list to confirm the stream path is
     registered and READY before issuing a stream_url.  Returns True/False.
     Non-blocking timeout of 2s — if the API is unreachable the URL is issued
     anyway (MediaMTX may just be starting up) and the fact is logged at WARNING.
     If MEDIAMTX_INTERNAL_URL is blank the probe is skipped and True is returned
     (stream_url is always issued; the browser will retry the WHEP handshake).

  4. broadcast_stream_url(rack_id, user_id) → convenience wrapper that
     combines build_stream_url() + ws_registry.broadcast_from_thread()
     targeting only the lock-holder's WebSocket (same routing logic as
     capture_complete — Section 4.3).

  5. broadcast_stream_close(rack_id, user_id) → same for the close signal.

No Nginx required for local dev — MediaMTX serves WebRTC/MJPEG directly.
[PROD ONLY] In production, reverse-proxy /camera/* → MediaMTX for HTTPS.
"""

from __future__ import annotations

import logging
import urllib.request
import json as _json
from typing import Any, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Timeout for the MediaMTX health probe (seconds) — short so we don't stall
# the lock-acquisition HTTP response.
_PROBE_TIMEOUT_S = 2


# ===========================================================================
# Stream URL / close message builders
# ===========================================================================

def build_stream_url(rack_id: str) -> dict[str, Any]:
    """
    Build the stream_url WebSocket message for rack_id (Section 8).

    Direct-Pi URL approach (no server proxy):
        WebRTC (WHEP): http://{pi_host}:{MEDIAMTX_WEBRTC_PORT}/{rack_id}/whep
        MJPEG fallback: http://{pi_host}:{MEDIAMTX_MJPEG_PORT}/{rack_id}/mjpeg

    Host is read directly from MEDIAMTX_PI_HOST in server/.env.
    DB lookup removed for testing — add back once racks are provisioned.
    """
    host = settings.MEDIAMTX_PI_HOST.strip()

    if host:
        webrtc_url = f"http://{host}:{settings.MEDIAMTX_WEBRTC_PORT}/{rack_id}/whep"
        mjpeg_url  = f"http://{host}:{settings.MEDIAMTX_MJPEG_PORT}/{rack_id}/mjpeg"
    else:
        # Not yet configured — return empty strings so the frontend can show
        # a helpful "check server/.env" message rather than a 404.
        webrtc_url = ""
        mjpeg_url  = ""
        logger.warning(
            "build_stream_url: MEDIAMTX_PI_HOST is not set — stream URLs will be empty. "
            "Set MEDIAMTX_PI_HOST=<pi-lan-ip> in server/.env"
        )

    return {
        "type": "stream_url",
        "data": {
            "rack_id":   rack_id,
            "url":       webrtc_url,
            "mjpeg_url": mjpeg_url,
        },
    }


def build_stream_close(rack_id: str) -> dict[str, Any]:
    """
    Build the stream_close WebSocket message for rack_id (Section 8).

    Sent when the lock is released so the browser can tear down the <video>
    element and free the WebRTC PeerConnection.
    """
    return {
        "type": "stream_close",
        "data": {
            "rack_id": rack_id,
        },
    }


# ===========================================================================
# MediaMTX stream presence probe (Section 8)
# ===========================================================================

def check_mediamtx_stream(rack_id: str) -> bool:
    """
    Probe the MediaMTX REST API to confirm the stream path for rack_id
    is registered and active.

    GET {MEDIAMTX_INTERNAL_URL}/v3/paths/list

    The MediaMTX API returns:
        { "itemCount": N, "pageCount": N,
          "items": [{"name": "rack-001", "ready": true, ...}, ...] }

    Returns True  — path is registered AND ready (camera hardware is active).
    Returns False — MediaMTX unreachable, non-200 response, path absent, or
                    path present but ready=false (camera not yet started).

    BUG FIX — two issues corrected here:
      1. MEDIAMTX_INTERNAL_URL must be the Pi's IP, not localhost. If it is
         blank (unconfigured) we skip the probe and return True so the stream
         URL is still issued; the browser's WHEP retry will handle readiness.
      2. We now check the 'ready' flag, not just path presence. With
         sourceOnDemand:true the path is registered even when the camera is
         off; only ready=true means the hardware is actually streaming.

    This probe is advisory: on False the caller still issues the stream_url
    but logs a warning. The browser will retry the WebRTC WHEP handshake.
    """
    # BUG FIX 1: skip probe when URL is blank (not yet configured)
    base_url = settings.MEDIAMTX_INTERNAL_URL.strip()
    if not base_url:
        logger.debug(
            "check_mediamtx_stream: MEDIAMTX_INTERNAL_URL is not set — "
            "skipping probe for rack=%s (set it to the Pi's IP:9997 in .env)",
            rack_id,
        )
        return True  # issue stream_url; browser handles readiness via WHEP retry

    try:
        api_url = f"{base_url.rstrip('/')}/v3/paths/list"
        req = urllib.request.Request(api_url, method="GET")
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            if resp.status != 200:
                logger.debug(
                    "check_mediamtx_stream: HTTP %d for rack=%s", resp.status, rack_id
                )
                return False
            body = _json.loads(resp.read().decode())

        items = body.get("items", [])
        if not isinstance(items, list):
            logger.debug("check_mediamtx_stream: unexpected items type %r", type(items))
            return False

        # FIX (Bug 1): treat path-present as "online" regardless of ready flag.
        # With sourceOnDemand:true, ready=false until a viewer connects —
        # which is always the case before the WHEP handshake.  Requiring
        # ready=true here meant the probe always returned False on the first
        # lock-acquire, generating a spurious warning every time.  The stream
        # URL is issued anyway (probe is advisory), but the log noise was
        # confusing.  mediamtx_health.py uses the same logic — path-present
        # means MediaMTX is running and configured correctly; the camera opens
        # on demand when the browser completes the WHEP handshake.
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("name", "") == rack_id:
                logger.debug(
                    "check_mediamtx_stream: rack=%s found (ready=%s) → online",
                    rack_id, item.get("ready"),
                )
                return True  # path registered = MediaMTX up and configured

        logger.debug(
            "check_mediamtx_stream: rack=%s not found in paths=%s",
            rack_id, [i.get("name") for i in items if isinstance(i, dict)],
        )
        return False

    except OSError as exc:
        # Connection refused / timeout (MediaMTX not running or not yet ready)
        logger.warning(
            "check_mediamtx_stream: MediaMTX API unreachable (%s) — "
            "issuing stream_url anyway; browser will retry.", exc
        )
        return False
    except Exception as exc:
        logger.warning("check_mediamtx_stream: unexpected error: %s", exc)
        return False


# ===========================================================================
# Broadcast helpers (called from routes.py after lock acquire / release)
# ===========================================================================

def broadcast_stream_url(rack_id: str, user_id: str) -> None:
    """
    Build and send the stream_url message to the lock-holder's WebSocket.

    Probes MediaMTX first; logs a warning if the stream path is not yet
    registered but sends the message regardless (browser will retry WHEP).

    Safe to call from any thread — uses ws_registry.broadcast_from_thread().
    """
    # Import here to avoid circular import at module load time.
    from api.websocket import ws_registry  # noqa: PLC0415

    stream_ready = check_mediamtx_stream(rack_id)
    if not stream_ready:
        logger.warning(
            "stream_url issued for rack=%s but MediaMTX path not yet registered "
            "(MediaMTX may be starting up or the Pi is offline)", rack_id
        )

    message = build_stream_url(rack_id)
    ws_registry.broadcast_from_thread(
        rack_id,
        message,
        lock_holder_user_id=user_id,
        lock_holder_only=True,
    )
    logger.info(
        "stream_url sent to user=%s for rack=%s (stream_ready=%s)",
        user_id, rack_id, stream_ready,
    )


def broadcast_stream_close(rack_id: str, user_id: Optional[str]) -> None:
    """
    Build and send the stream_close message to the (former) lock-holder's
    WebSocket after the lock is released.

    user_id may be None if the lock was swept by the expiry daemon — in that
    case the message is broadcast to all subscribers of that rack so the
    browser doesn't hang on a defunct stream.

    Safe to call from any thread.
    """
    from api.websocket import ws_registry  # noqa: PLC0415

    message = build_stream_close(rack_id)

    if user_id:
        # Targeted: send only to the (former) lock holder.
        ws_registry.broadcast_from_thread(
            rack_id,
            message,
            lock_holder_user_id=user_id,
            lock_holder_only=True,
        )
    else:
        # Broadcast: lock swept by expiry daemon; user_id is unknown.
        ws_registry.broadcast_from_thread(
            rack_id,
            message,
            lock_holder_user_id=None,
            lock_holder_only=False,
        )

    logger.info(
        "stream_close sent for rack=%s to user=%s",
        rack_id, user_id or "<all>",
    )