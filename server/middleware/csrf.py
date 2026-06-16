"""
server/middleware/csrf.py

Section 9 Layer 1 / Section 4.2 — Double-submit cookie CSRF protection.

Behaviour
---------
When settings.CSRF_ENABLED=True:
  - A CSRF token (32 random hex bytes) is set in a cookie named "csrftoken"
    on every response that does not already have one.
  - On every state-changing request (POST, PUT, DELETE, PATCH) and on
    WebSocket CAPTURE/command messages, the server reads the value from the
    request header "X-CSRF-Token" and compares it to the cookie value using
    a constant-time comparison.  A mismatch → 403.
  - Cookie flags:
      HttpOnly=False  (the JS layer must be able to read it)
      SameSite=Strict
      Secure          controlled by settings.COOKIE_SECURE
                      (False locally over HTTP, True in production)

When settings.CSRF_ENABLED=False (default):
  - The middleware is a no-op pass-through.  The flag can be flipped to True
    without any code changes (Section 9 [PROD ONLY] notes that CSRF works
    locally too, just with non-Secure cookies).

Public surface
--------------
  CSRFMiddleware   — Starlette BaseHTTPMiddleware; mount in main.py:
                       app.add_middleware(CSRFMiddleware)

  get_csrf_token   — FastAPI dependency; returns the token string from the
                     cookie (for WebSocket handshake validation in
                     api/websocket.py).

  validate_csrf_header — standalone helper called by the WebSocket message
                         handler (Section 4.2) when a CAPTURE or command
                         message arrives over /ws.

Cookie name and header name are constants here so they are never duplicated
in the frontend wiring (Section 7).
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import Cookie, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from config.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants  (match these in the frontend config/app.config.ts)
# ---------------------------------------------------------------------------

CSRF_COOKIE_NAME  = "csrftoken"
CSRF_HEADER_NAME  = "x-csrf-token"   # lowercase — ASGI normalises headers

# State-changing HTTP methods that require the CSRF header check
_CSRF_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Paths that are exempt from CSRF enforcement even when CSRF_ENABLED=True.
# - /auth/login  uses credentials (not a cookie-authenticated endpoint)
# - /provision   is authenticated by PROVISIONING_SECRET, not a session cookie
_EXEMPT_PREFIXES = ("/auth/", "/provision")


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    """Return a 64-hex-char (32-byte) cryptographically random CSRF token."""
    return secrets.token_hex(32)


def _tokens_match(a: str, b: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return secrets.compare_digest(a, b)


def _get_cookie_token(request: Request) -> Optional[str]:
    """Extract the CSRF token from the request cookie jar."""
    return request.cookies.get(CSRF_COOKIE_NAME)


def _get_header_token(request: Request) -> Optional[str]:
    """Extract the CSRF token from the X-CSRF-Token request header."""
    return request.headers.get(CSRF_HEADER_NAME)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class CSRFMiddleware(BaseHTTPMiddleware):
    """
    Double-submit cookie CSRF middleware.

    Mount order: must be AFTER authentication middleware so that unauthenticated
    requests are rejected before we attempt to read/set cookies.

    Mount in main.py:
        app.add_middleware(CSRFMiddleware)

    When CSRF_ENABLED=False the middleware calls through immediately.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        # Fast path — CSRF disabled
        if not settings.CSRF_ENABLED:
            return await call_next(request)

        # WebSocket upgrade — validation happens at the message level via
        # validate_csrf_header(); skip middleware check here.
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)

        # Exempt paths (login, provisioning)
        if any(request.url.path.startswith(p) for p in _EXEMPT_PREFIXES):
            return await call_next(request)

        # State-changing requests: validate double-submit
        if request.method in _CSRF_METHODS:
            cookie_token = _get_cookie_token(request)
            header_token = _get_header_token(request)

            if not cookie_token:
                logger.warning(
                    "csrf: missing cookie on %s %s", request.method, request.url.path
                )
                return Response(
                    content='{"detail":"CSRF cookie missing."}',
                    status_code=403,
                    media_type="application/json",
                )

            if not header_token:
                logger.warning(
                    "csrf: missing header on %s %s", request.method, request.url.path
                )
                return Response(
                    content='{"detail":"X-CSRF-Token header required."}',
                    status_code=403,
                    media_type="application/json",
                )

            if not _tokens_match(cookie_token, header_token):
                logger.warning(
                    "csrf: token mismatch on %s %s", request.method, request.url.path
                )
                return Response(
                    content='{"detail":"CSRF token mismatch."}',
                    status_code=403,
                    media_type="application/json",
                )

        # Call the actual route handler
        response: Response = await call_next(request)

        # Ensure every response carries a fresh or existing CSRF cookie so
        # the browser JS can read it.
        existing = _get_cookie_token(request)
        token = existing or _generate_token()
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=token,
            httponly=False,           # JS must read this
            samesite="strict",
            secure=settings.COOKIE_SECURE,  # False locally, True in prod
            path="/",
        )

        return response


# ---------------------------------------------------------------------------
# FastAPI dependency — for WebSocket / routes that need the token directly
# ---------------------------------------------------------------------------


async def get_csrf_token(
    csrftoken: Optional[str] = Cookie(default=None, alias=CSRF_COOKIE_NAME),
) -> Optional[str]:
    """
    FastAPI cookie dependency — returns the CSRF token string from the cookie.

    Inject in WebSocket route to access the token value:
        token: str = Depends(get_csrf_token)

    Returns None when the cookie is absent (CSRF may be disabled or the client
    hasn't received a cookie yet).
    """
    return csrftoken


# ---------------------------------------------------------------------------
# Standalone validator — for WebSocket message-level CSRF check (Section 4.2)
# ---------------------------------------------------------------------------


def validate_csrf_header(cookie_token: Optional[str], header_token: Optional[str]) -> None:
    """
    Validate a CSRF double-submit pair outside the HTTP middleware layer.

    Called by api/websocket.py when a CAPTURE or command message arrives
    over the WebSocket connection (Section 4.2):

        validate_csrf_header(
            cookie_token=ws_cookie_token,
            header_token=message.get("csrf_token"),
        )

    Raises
    ------
    fastapi.HTTPException (403) if CSRF_ENABLED=True and the tokens do not
    match (or either is absent).

    No-op when CSRF_ENABLED=False.
    """
    if not settings.CSRF_ENABLED:
        return

    if not cookie_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF cookie missing on WebSocket message.",
        )
    if not header_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token missing from WebSocket message payload.",
        )
    if not _tokens_match(cookie_token, header_token):
        logger.warning("csrf: WebSocket CSRF token mismatch")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch on WebSocket message.",
        )
