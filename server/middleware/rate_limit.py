"""
server/middleware/rate_limit.py

Section 4.2 / Section 9 Layer 2A — slowapi-based rate limiting.

Two tiers (driven entirely by settings, not hardcoded):
  1. Default per-user command limit  — applied to all command / lock endpoints.
     Limit string: "{RATE_LIMIT_PER_USER_COMMANDS}/minute"
     Key          : user_id extracted from the JWT/ADMIN_TOKEN (via _key_user)

  2. Presign limit (stricter)         — 2/min per Pi credential on /presign.
     Limit string: "{RATE_LIMIT_PRESIGN}/minute"   (default "2/minute")
     Key          : X-API-Key header value (the PI_API_KEY credential)

Two new settings keys are consumed here.  They are declared in
config/settings.py as part of the Cache / Rate-limit group:
  RATE_LIMIT_PER_USER_COMMANDS  (default "60/minute")
  RATE_LIMIT_PRESIGN            (default "2/minute")

Public surface
--------------
  limiter          — slowapi Limiter singleton; mounted on the FastAPI app in
                     main.py via  app.state.limiter = limiter  +
                     app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

  limit_commands   — decorator applied to all command / lock route functions.
  limit_presign    — decorator applied to the /rack/{id}/presign route.

Usage in api/routes.py:
    from middleware.rate_limit import limiter, limit_commands, limit_presign

    @router.post("/rack/{rack_id}/command")
    @limit_commands
    async def command_endpoint(request: Request, ...):
        ...

    @router.post("/rack/{rack_id}/presign")
    @limit_presign
    async def presign_endpoint(request: Request, ...):
        ...
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key functions
# ---------------------------------------------------------------------------


def _key_user(request: Request) -> str:
    """
    Rate-limit key for browser users: user_id from the JWT / ADMIN_TOKEN.

    Falls back to the client IP if the token cannot be parsed (the auth
    middleware will reject the request before the route handler runs, so
    this fallback is only reached by the rate-limit layer itself during
    the pre-handler check).
    """
    from core.security import decode_token, is_valid_admin_token

    auth: Optional[str] = request.headers.get("authorization", "")
    scheme, _, token = (auth or "").partition(" ")
    token = token.strip()

    if token:
        # ADMIN_TOKEN path
        if is_valid_admin_token(token):
            return "_admin_token_user"
        # JWT path
        try:
            payload = decode_token(token)
            return payload.user_id
        except Exception:
            pass

    # Fall back to IP (still provides some protection)
    return request.client.host if request.client else "unknown"


def _key_pi_api_key(request: Request) -> str:
    """
    Rate-limit key for the /presign endpoint: X-API-Key header value.

    Using the full key value as the bucket key means each Pi credential
    gets its own 2/min counter.  Falls back to IP if the header is absent
    (middleware will reject the request anyway).
    """
    key = request.headers.get("x-api-key", "")
    if key:
        # Truncate for safety (don't put full secret in logs via slowapi internals)
        return f"pi:{key[:8]}..."
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Limiter singleton
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=_key_user, default_limits=[])
"""
Module-level slowapi Limiter.

Mount in main.py:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from middleware.rate_limit import limiter

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
"""


# ---------------------------------------------------------------------------
# Rate-limit exception handler  (register in main.py)
# ---------------------------------------------------------------------------


def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """
    Custom 429 handler — returns JSON instead of slowapi's default plain-text.
    Includes a Retry-After header as required by Section 9 Layer 2A.
    """
    retry_after = getattr(exc, "retry_after", 60)
    logger.warning(
        "rate_limit: 429 for key=%s path=%s",
        _key_user(request) if "presign" not in str(request.url) else _key_pi_api_key(request),
        request.url.path,
    )
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded. Please wait before retrying.",
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


# ---------------------------------------------------------------------------
# Decorators — apply to individual route functions
# ---------------------------------------------------------------------------

def limit_commands(route_func):
    """
    Apply the per-user command rate limit (settings.RATE_LIMIT_PER_USER_COMMANDS).

    The limiter uses _key_user so each authenticated user gets their own bucket.
    Unauthenticated requests fall back to IP (and will be rejected by auth middleware
    before the route handler runs).
    """
    limit_str = settings.RATE_LIMIT_PER_USER_COMMANDS
    return limiter.limit(limit_str, key_func=_key_user)(route_func)


def limit_presign(route_func):
    """
    Apply the stricter presign rate limit (settings.RATE_LIMIT_PRESIGN, default 2/minute)
    keyed by the Pi's X-API-Key credential.
    """
    limit_str = settings.RATE_LIMIT_PRESIGN
    return limiter.limit(limit_str, key_func=_key_pi_api_key)(route_func)
