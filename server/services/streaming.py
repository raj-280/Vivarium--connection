"""
server/services/streaming.py

Live streaming URL builder — Section 4.2 / 8 / Stage 10.

Implements the server side of the go2rtc streaming protocol described in
Section 8:

  1. build_stream_url(rack_id)  → the WebSocket message the browser uses to
     open its <video> element:
         { "type": "stream_url",
           "data": { "rack_id": "rack-047",
                     "url": "/camera/api/webrtc?src=rack-047",
                     "mjpeg_url": "/camera/mjpeg?src=rack-047" } }

  2. build_stream_close(rack_id) → the WebSocket message sent when the lock
     is released, so the browser tears down the <video> element:
         { "type": "stream_close",
           "data": { "rack_id": "rack-047" } }

  3. check_go2rtc_stream(rack_id) → probes the go2rtc HTTP API at
     GO2RTC_INTERNAL_URL/api/streams to confirm the stream is registered
     before issuing a stream_url.  Returns True/False.  Non-blocking timeout
     of 2s — if the API is unreachable the URL is issued anyway (go2rtc may
     just be starting up) and the fact is logged at WARNING.

  4. broadcast_stream_url(rack_id, user_id) → convenience wrapper that
     combines build_stream_url() + ws_registry.broadcast_from_thread()
     targeting only the lock-holder's WebSocket (same routing logic as
     capture_complete — Section 4.3).

  5. broadcast_stream_close(rack_id, user_id) → same for the close signal.

Security notes (Section 9 Layer 2D):
  • Stream URLs are only issued while the operator holds the rack lock
    (called from routes.py acquire_lock — see wiring note below).
  • user_rack_assignments check: the operator must be assigned to the rack.
    This is enforced by the require_rack_operator dependency in routes.py;
    streaming.py trusts that the caller has already validated assignment.
  • [PROD ONLY] go2rtc on the server is bound to localhost:1984 and
    reverse-proxied by Nginx with JWT validation at /camera/*.

Wiring (routes.py):
  After acquire_lock() returns LockResult.ACQUIRED, call:
      from services.streaming import broadcast_stream_url
      broadcast_stream_url(rack_id, user_id)

  After release_lock_endpoint() succeeds, call:
      from services.streaming import broadcast_stream_close
      broadcast_stream_close(rack_id, user_id)

  Both functions are fire-and-forget (run in the FastAPI thread;
  ws_registry.broadcast_from_thread() handles async bridge internally).
"""

from __future__ import annotations

import logging
import urllib.request
import json as _json
from typing import Any, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# Timeout for the go2rtc health probe (seconds) — short so we don't stall
# the lock-acquisition HTTP response.
_PROBE_TIMEOUT_S = 2


# ===========================================================================
# Stream URL / close message builders
# ===========================================================================

def build_stream_url(rack_id: str) -> dict[str, Any]:
    """
    Build the stream_url WebSocket message for rack_id (Section 8).

    URL shape:
        {GO2RTC_PROXY_PATH}/api/webrtc?src={rack_id}
    where GO2RTC_PROXY_PATH defaults to "/camera" (config-driven, Section 2.1).

    The browser opens a WebRTC session at this URL via the Nginx reverse proxy
    (localhost → go2rtc:1984, never exposed directly).

    A fallback MJPEG URL is also included for browsers that don't support
    WebRTC or when go2rtc's WebRTC ICE negotiation fails:
        {GO2RTC_PROXY_PATH}/mjpeg?src={rack_id}
    """
    proxy = settings.GO2RTC_PROXY_PATH.rstrip("/")
    webrtc_url = f"{proxy}/api/webrtc?src={rack_id}"
    mjpeg_url  = f"{proxy}/mjpeg?src={rack_id}"

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
# go2rtc stream presence probe (Section 8 / 9 Layer 2D)
# ===========================================================================

def check_go2rtc_stream(rack_id: str) -> bool:
    """
    Probe the go2rtc HTTP API to confirm the stream for rack_id is registered.

    GET {GO2RTC_INTERNAL_URL}/api/streams

    The go2rtc API returns a JSON object whose keys are stream names.  If the
    expected key is present, the stream is ready for the browser.

    Returns True  — stream is registered and ready.
    Returns False — go2rtc unreachable, non-200 response, or stream absent.

    This probe is advisory: on False the caller still issues the stream_url
    but logs a warning.  The browser will retry the WebRTC ICE handshake on
    its own timeline.
    """
    try:
        api_url = f"{settings.GO2RTC_INTERNAL_URL.rstrip('/')}/api/streams"
        req = urllib.request.Request(api_url, method="GET")
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            if resp.status != 200:
                logger.debug(
                    "check_go2rtc_stream: HTTP %d for rack=%s", resp.status, rack_id
                )
                return False
            body = _json.loads(resp.read().decode())

        if not isinstance(body, dict):
            logger.debug(
                "check_go2rtc_stream: unexpected response type %r", type(body)
            )
            return False

        present = rack_id in body
        logger.debug(
            "check_go2rtc_stream: rack=%s present=%s streams=%s",
            rack_id, present, list(body.keys()),
        )
        return present

    except OSError as exc:
        # Connection refused / timeout (go2rtc not running or not yet ready)
        logger.warning(
            "check_go2rtc_stream: go2rtc API unreachable (%s) — "
            "issuing stream_url anyway; browser will retry.", exc
        )
        return False
    except Exception as exc:
        logger.warning("check_go2rtc_stream: unexpected error: %s", exc)
        return False


# ===========================================================================
# Broadcast helpers (called from routes.py after lock acquire / release)
# ===========================================================================

def broadcast_stream_url(rack_id: str, user_id: str) -> None:
    """
    Build and send the stream_url message to the lock-holder's WebSocket.

    Probes go2rtc first; logs a warning if the stream is not yet registered
    but sends the message regardless (browser will retry ICE).

    Safe to call from any thread — uses ws_registry.broadcast_from_thread().
    """
    # Import here to avoid circular import at module load time.
    from api.websocket import ws_registry  # noqa: PLC0415

    stream_ready = check_go2rtc_stream(rack_id)
    if not stream_ready:
        logger.warning(
            "stream_url issued for rack=%s but go2rtc stream not yet registered "
            "(go2rtc may be starting up or the Pi is offline)", rack_id
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
