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

Thread safety
─────────────
All WebSocket send operations run in the asyncio event loop.  MQTT callbacks
run in paho's background thread and schedule coroutines via
asyncio.run_coroutine_threadsafe().

FIXES applied in this version
──────────────────────────────
• Mismatch 6: Emergency stop ("!" command) dispatched via WebSocket now
  calls release_lock() and broadcast_stream_close() exactly like the HTTP
  route does, so the rack lock is not left alive after an e-stop.
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
    rack_ids: set[str] = field(default_factory=set)
    csrf_cookie_token: Optional[str] = None


class ConnectionRegistry:
    """
    Thread-safe registry mapping rack_id → list of WSConnection.
    """

    def __init__(self) -> None:
        self._connections: list[WSConnection] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
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

    async def broadcast_to_rack(
        self,
        rack_id: str,
        message: dict,
        *,
        lock_holder_user_id: Optional[str] = None,
        lock_holder_only: bool = False,
    ) -> None:
        payload = json.dumps(message)
        dead: list[WSConnection] = []

        for conn in self._connections:
            if rack_id and conn.rack_ids and rack_id not in conn.rack_ids:
                continue

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


ws_registry = ConnectionRegistry()


# ===========================================================================
# Auth helpers
# ===========================================================================

def _auth_token_from_headers(headers: dict) -> Optional[str]:
    auth: str = headers.get("authorization", "") or headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer":
        return token.strip() or None
    return None


def _resolve_ws_identity(token: Optional[str]) -> Optional[tuple[str, str]]:
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

    Close codes:
        4001 — authentication failed / invalid token
        4003 — forbidden
    """
    await websocket.accept()

    # ── Step 1: resolve credentials ──────────────────────────────────────────
    raw_headers = dict(websocket.headers)
    token = _auth_token_from_headers(raw_headers)

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

    # ── Step 2: capture CSRF cookie value ────────────────────────────────────
    csrf_cookie = websocket.cookies.get("csrftoken")

    # ── Step 3: register connection ───────────────────────────────────────────
    conn = WSConnection(
        websocket=websocket,
        user_id=user_id,
        role=role,
        csrf_cookie_token=csrf_cookie,
        rack_ids=set(),
    )
    ws_registry.add(conn)

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

            elif msg_type in ("command", "CAPTURE"):
                rack_id = msg.get("rack_id")
                command  = msg.get("command", "CAPTURE" if msg_type == "CAPTURE" else "")
                if rack_id and command:
                    from db.database import get_db
                    from services.command_handler import CommandValidationError, handle_command
                    db = next(get_db())
                    try:
                        result = handle_command(
                            rack_id=rack_id,
                            raw_command=command,
                            user_id=conn.user_id,
                            db=db,
                        )

                        # FIX (Mismatch 6): After an emergency stop dispatched via
                        # WebSocket, release the rack lock and close the stream —
                        # exactly mirroring what the HTTP route handler does.
                        # Previously the WS path never called release_lock(), so the
                        # rack stayed locked until the 2-second sweep task expired it.
                        if result.outcome == "emergency":
                            from db.database import db_session
                            from core.locking import release_lock
                            from services.streaming import broadcast_stream_close
                            try:
                                with db_session() as db_rel:
                                    release_lock(rack_id=rack_id, db=db_rel)
                            except Exception:
                                logger.exception(
                                    "WS: release_lock failed after e-stop rack=%s", rack_id
                                )
                            try:
                                broadcast_stream_close(rack_id, conn.user_id)
                            except Exception:
                                logger.exception(
                                    "WS: broadcast_stream_close failed after e-stop rack=%s",
                                    rack_id,
                                )

                        await websocket.send_text(json.dumps({
                            "type": "command_ack",
                            "rack_id": rack_id,
                            "command": command,
                            "outcome": result.outcome,
                        }))
                    except CommandValidationError as exc:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "detail": str(exc),
                        }))
                    finally:
                        db.close()
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "detail": "command and rack_id are required.",
                    }))

            else:
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
        holder = _get_lock_holder(rack_id)
        ws_registry.broadcast_from_thread(
            rack_id,
            message,
            lock_holder_user_id=holder,
            lock_holder_only=True,
        )
    else:
        ws_registry.broadcast_from_thread(rack_id, message)
