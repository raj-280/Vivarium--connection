"""
server/api/websocket.py

/ws WebSocket endpoint — Section 4.2 / Section 4.3 / Section 9 Layer 2A.

Connection lifecycle
────────────────────
1. Client connects with 'Authorization: Bearer <token>' header (or sends it
   as a first text frame when header-based WS auth is not possible in the
   browser — both paths are supported).
2. Server validates ADMIN_TOKEN or JWT.  Invalid credential → close(4001).
3. Server adds the connection to the per-rack subscriber registry.
4. MQTT messages arriving on vivarium/rack/+/# are forwarded to all
   subscribers of that rack who have a matching role.

Message routing rules (Section 4.3)
─────────────────────────────────────
• capture_complete  → sent ONLY to the WebSocket connection whose user_id
                      matches the rack's current lock_holder_user_id.
                      Viewer connections never receive this type.
• scan_cell_complete→ broadcast to ALL active connections for that rack
                      (operators and viewers).
• All other messages → broadcast to all connections for that rack.

CSRF on command/CAPTURE messages (Section 4.2 / Section 9 Layer 1)
──────────────────────────────────────────────────────────────────
When CSRF_ENABLED=True, any incoming WS message with type "command" or
"CAPTURE" must include a "csrf_token" field matching the cookie value.
The cookie token is captured at connect time from the Sec-WebSocket-Key
context (browsers send cookies with the WS upgrade request).
The validate_csrf_header() helper from middleware/csrf.py is used.

Thread safety
─────────────
All WebSocket send operations run in the asyncio event loop.  MQTT callbacks
run in paho's background thread and schedule coroutines via
asyncio.run_coroutine_threadsafe().
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from jose import JWTError

from config.settings import settings
from core.security import decode_token, is_valid_admin_token
from middleware.csrf import validate_csrf_header

logger = logging.getLogger(__name__)

router = APIRouter()


# ===========================================================================
# Connection registry
# ===========================================================================

@dataclass
class WSConnection:
    """Represents one active WebSocket session."""
    websocket: WebSocket
    user_id: str
    role: str
    # rack_ids this connection cares about — currently all racks (will be
    # scoped per-operator via user_rack_assignments in a later pass)
    rack_ids: set[str] = field(default_factory=set)
    # CSRF cookie value captured at connect time (may be None if CSRF disabled)
    csrf_cookie_token: Optional[str] = None


class ConnectionRegistry:
    """
    Thread-safe registry mapping rack_id → list of WSConnection.

    MQTT callbacks (background thread) call broadcast_to_rack() via
    asyncio.run_coroutine_threadsafe(); FastAPI route handlers use it directly.
    """

    def __init__(self) -> None:
        self._connections: list[WSConnection] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from main.py lifespan after the event loop is running."""
        self._loop = loop

    def add(self, conn: WSConnection) -> None:
        self._connections.append(conn)
        logger.info(
            "WS connected: user=%s role=%s total=%d",
            conn.user_id, conn.role, len(self._connections),
        )

    def remove(self, conn: WSConnection) -> None:
        try:
            self._connections.remove(conn)
        except ValueError:
            pass
        logger.info(
            "WS disconnected: user=%s total=%d",
            conn.user_id, len(self._connections),
        )

    def get_all(self) -> list[WSConnection]:
        return list(self._connections)

    # ------------------------------------------------------------------
    # Selective broadcast helpers
    # ------------------------------------------------------------------

    async def broadcast_to_rack(
        self,
        rack_id: str,
        message: dict,
        *,
        lock_holder_user_id: Optional[str] = None,
        lock_holder_only: bool = False,
    ) -> None:
        """
        Send *message* (as JSON) to WebSocket connections subscribed to *rack_id*.

        lock_holder_only=True  → only the connection whose user_id matches
                                 lock_holder_user_id receives the message.
                                 Used for capture_complete (Section 4.3).
        lock_holder_only=False → all connections for the rack receive it
                                 (viewers included), used for scan_cell_complete
                                 and all status/position messages.
        """
        payload = json.dumps(message)
        dead: list[WSConnection] = []

        for conn in self._connections:
            # Rack filter — skip if connection has no interest in this rack
            if rack_id and conn.rack_ids and rack_id not in conn.rack_ids:
                continue

            # Lock-holder filter (capture_complete)
            if lock_holder_only:
                if conn.role == "viewer":
                    continue
                if lock_holder_user_id and conn.user_id != lock_holder_user_id:
                    continue

            try:
                await conn.websocket.send_text(payload)
            except Exception:
                dead.append(conn)

        for conn in dead:
            self.remove(conn)

    def broadcast_from_thread(
        self,
        rack_id: str,
        message: dict,
        *,
        lock_holder_user_id: Optional[str] = None,
        lock_holder_only: bool = False,
    ) -> None:
        """
        Thread-safe wrapper for broadcast_to_rack().
        Called from MQTT paho background thread via asyncio.run_coroutine_threadsafe.
        """
        if self._loop is None or self._loop.is_closed():
            logger.warning("WS broadcast skipped — event loop not available")
            return

        asyncio.run_coroutine_threadsafe(
            self.broadcast_to_rack(
                rack_id,
                message,
                lock_holder_user_id=lock_holder_user_id,
                lock_holder_only=lock_holder_only,
            ),
            self._loop,
        )


# Module-level registry singleton — imported by main.py to wire MQTT handlers
ws_registry = ConnectionRegistry()


# ===========================================================================
# Auth helpers for WebSocket
# ===========================================================================

def _auth_token_from_headers(headers: dict) -> Optional[str]:
    """Extract Bearer token from request headers (case-insensitive)."""
    auth: str = headers.get("authorization", "") or headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer":
        return token.strip() or None
    return None


def _resolve_ws_identity(token: Optional[str]) -> Optional[tuple[str, str]]:
    """
    Validate token and return (user_id, role) or None on failure.

    Accepts ADMIN_TOKEN or a valid JWT.
    """
    if not token:
        return None
    if is_valid_admin_token(token):
        return ("_admin_token_user", "admin")
    try:
        payload = decode_token(token)
        return (payload.user_id, payload.role)
    except (JWTError, ValueError):
        return None


# ===========================================================================
# /ws WebSocket endpoint
# ===========================================================================

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Main browser ↔ server WebSocket (Section 4.2 / 8).

    Auth: Bearer token in the 'Authorization' upgrade header.
    If absent, the first text message from the client may carry:
        {"type": "auth", "token": "<token>"}
    which gives browser environments that can't set WS headers a fallback.

    Close codes:
        4001 — authentication failed / invalid token
        4003 — forbidden
    """
    await websocket.accept()

    # ── Step 1: resolve credentials ──────────────────────────────────────────
    raw_headers = dict(websocket.headers)
    token = _auth_token_from_headers(raw_headers)

    # Fallback: first text message carries auth
    if not token:
        try:
            first_msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            data = json.loads(first_msg)
            if data.get("type") == "auth":
                token = data.get("token")
        except (asyncio.TimeoutError, json.JSONDecodeError, KeyError):
            pass

    identity = _resolve_ws_identity(token)
    if identity is None:
        logger.warning("WS auth failed — closing 4001")
        await websocket.close(code=4001, reason="Authentication failed")
        return

    user_id, role = identity

    # ── Step 2: capture CSRF cookie value (Section 4.2) ──────────────────────
    csrf_cookie = websocket.cookies.get("csrftoken")

    # ── Step 3: register connection ───────────────────────────────────────────
    conn = WSConnection(
        websocket=websocket,
        user_id=user_id,
        role=role,
        csrf_cookie_token=csrf_cookie,
        # For now all racks are subscribed; per-rack scoping is enforced on
        # broadcast by checking lock_holder_user_id (capture_complete) or
        # user_rack_assignments (future pass).
        rack_ids=set(),
    )
    ws_registry.add(conn)

    # Send a connected confirmation
    await websocket.send_text(json.dumps({
        "type": "connected",
        "user_id": user_id,
        "role": role,
    }))

    # ── Step 4: message loop ──────────────────────────────────────────────────
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "detail": "Invalid JSON.",
                }))
                continue

            msg_type = msg.get("type", "")

            # ── CSRF check on command/CAPTURE messages ────────────────────────
            if msg_type in ("command", "CAPTURE"):
                try:
                    validate_csrf_header(
                        cookie_token=conn.csrf_cookie_token,
                        header_token=msg.get("csrf_token"),
                    )
                except Exception:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "detail": "CSRF validation failed.",
                    }))
                    continue

            # ── Rack subscription ─────────────────────────────────────────────
            if msg_type == "subscribe":
                rack_id = msg.get("rack_id")
                if rack_id:
                    conn.rack_ids.add(rack_id)
                    await websocket.send_text(json.dumps({
                        "type": "subscribed",
                        "rack_id": rack_id,
                    }))

            elif msg_type == "unsubscribe":
                rack_id = msg.get("rack_id")
                if rack_id:
                    conn.rack_ids.discard(rack_id)

            elif msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            else:
                # Unknown message types are silently ignored (forward compat)
                logger.debug("WS unknown msg type=%r from user=%s", msg_type, user_id)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("WS error for user=%s: %s", user_id, exc)
    finally:
        ws_registry.remove(conn)


# ===========================================================================
# MQTT → WebSocket bridge helper (called from main.py MQTT handlers)
# ===========================================================================

def _get_lock_holder(rack_id: str) -> Optional[str]:
    """
    Look up the current lock_holder_user_id for a rack from the in-memory
    gantry_state mirror (no DB query needed — state is kept in sync by the
    MQTT response handler).
    """
    from core.state import gantry_state
    state = gantry_state.get(rack_id)
    return state.lock_holder_user_id if state else None


def relay_mqtt_to_ws(rack_id: Optional[str], subtopic: str, payload) -> None:
    """
    Registered as the catch-all MQTT handler in main.py.

    Implements Section 4.3 routing:
      • capture_complete  → lock-holder only (viewers excluded)
      • scan_cell_complete→ all rack subscribers (broadcast)
      • everything else   → broadcast to all rack subscribers
    """
    if rack_id is None:
        return

    message = {
        "type": subtopic,
        "rack_id": rack_id,
        "data": payload,
    }

    if subtopic == "capture_complete":
        # Deliver only to the operator who holds the lock (Section 4.3)
        holder = _get_lock_holder(rack_id)
        ws_registry.broadcast_from_thread(
            rack_id,
            message,
            lock_holder_user_id=holder,
            lock_holder_only=True,
        )
    else:
        # scan_cell_complete + all status/position/scan messages → broadcast
        ws_registry.broadcast_from_thread(rack_id, message)
