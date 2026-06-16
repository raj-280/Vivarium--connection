"""
server/middleware/auth.py

Section 4.2 / Section 9 Layer 2A — Per-request credential routing and role
enforcement for FastAPI routes.

Credential spaces (mutually exclusive per endpoint)
-----------------------------------------------------
A. Browser credentials (all routes except /rack/{id}/presign):
     1. Bearer JWT    — Authorization: Bearer <jwt>
     2. ADMIN_TOKEN   — Authorization: Bearer <admin_token>
        (identical header; resolved by trying JWT decode first)

B. Pi credential (/rack/{id}/presign ONLY):
     PI_API_KEY       — X-API-Key: <key>

Mismatched types are explicitly rejected:
  - MQTT password presented to /presign  → 401 (MQTT credential not accepted here)
  - PI_API_KEY presented to any browser route → 401

Role enforcement (Section 4.2):
  - viewer  → GET-only; blocked from any command / lock / capture / presign endpoint
  - operator → commands allowed only for racks in user_rack_assignments
  - admin    → full access; bypasses user_rack_assignments

Public
------
  require_browser_user   — dependency for all routes that need a logged-in
                           browser user (viewer / operator / admin).
  require_operator        — dependency for command / lock endpoints; also
                           checks rack assignment via user_has_rack_access.
  require_admin           — dependency for admin-only routes.
  require_pi_api_key      — dependency for /rack/{id}/presign.
  CurrentUser             — typed dataclass returned by browser dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Path, status
from jose import JWTError
from sqlalchemy.orm import Session

from config.settings import settings
from core.security import (
    decode_token,
    is_valid_admin_token,
    is_valid_pi_api_key,
    looks_like_mqtt_credential,
    user_has_rack_access,
)
from db.database import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------


@dataclass
class CurrentUser:
    """
    Resolved identity for a browser caller.  Returned by require_browser_user,
    require_operator, and require_admin.
    """

    user_id: str
    role: str
    # True when authenticated with ADMIN_TOKEN rather than a full JWT — the
    # user_id is the synthetic sentinel "_admin_token_user" in that case.
    is_admin_token: bool = False


# Sentinel user_id used when the static ADMIN_TOKEN is presented (no DB row
# is required; the token is validated against the config key only).
_ADMIN_TOKEN_SENTINEL = "_admin_token_user"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """
    Parse the value from an 'Authorization: Bearer <token>' header.
    Returns None if the header is absent or not in Bearer scheme.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _resolve_browser_identity(authorization: Optional[str]) -> CurrentUser:
    """
    Try to resolve a browser identity from the Authorization header.

    Resolution order:
    1. If value == ADMIN_TOKEN  → admin (static token)
    2. Else try JWT decode      → role from token payload
    3. Else check for MQTT-style credential (64 hex) → 401 explicit error
    4. Else 401 generic

    Raises HTTPException on failure.
    """
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Supply 'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check static ADMIN_TOKEN first (fast-path for dev)
    if is_valid_admin_token(token):
        return CurrentUser(
            user_id=_ADMIN_TOKEN_SENTINEL,
            role="admin",
            is_admin_token=True,
        )

    # MQTT-credential heuristic — give a precise error to help debugging
    if looks_like_mqtt_credential(token):
        logger.warning("auth: MQTT credential presented to a browser endpoint — rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "MQTT credentials are not accepted on this endpoint. "
                "Use a JWT obtained from /auth/login."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # JWT path
    try:
        payload = decode_token(token)
    except JWTError as exc:
        logger.debug("auth: JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except ValueError as exc:
        logger.debug("auth: JWT missing claims: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return CurrentUser(user_id=payload.user_id, role=payload.role)


# ---------------------------------------------------------------------------
# Public FastAPI dependencies — browser caller
# ---------------------------------------------------------------------------


async def require_browser_user(
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:
    """
    Dependency: any authenticated browser user (viewer / operator / admin).

    Inject with:
        current_user: CurrentUser = Depends(require_browser_user)

    Raises 401 if no valid credential is present.
    """
    return _resolve_browser_identity(authorization)


async def require_operator(
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:
    """
    Dependency: operator or admin role (viewers are rejected with 403).

    Does NOT check rack assignment — callers that need per-rack enforcement
    should use require_rack_operator instead.

    Raises 401 for missing/invalid credentials; 403 for viewer role.
    """
    user = _resolve_browser_identity(authorization)
    if user.role == "viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewer role cannot perform this action.",
        )
    return user


async def require_admin(
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:
    """
    Dependency: admin role only.

    Raises 401 for missing/invalid credentials; 403 for non-admin roles.
    """
    user = _resolve_browser_identity(authorization)
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return user


# ---------------------------------------------------------------------------
# Public FastAPI dependency — rack-scoped operator
# ---------------------------------------------------------------------------


async def require_rack_operator(
    rack_id: str = Path(...),
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """
    Dependency: operator or admin role, AND the user must be assigned to the
    requested rack (via user_rack_assignments — Section 3.3).

    Admins always pass the rack check.
    Viewers are always rejected with 403.
    Operators without an assignment for the requested rack are rejected with 403.

    Usage (in api/routes.py):
        @router.post("/rack/{rack_id}/command")
        async def command(user: CurrentUser = Depends(require_rack_operator)):
            ...
    """
    user = _resolve_browser_identity(authorization)

    if user.role == "viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewer role cannot send commands.",
        )

    # ADMIN_TOKEN path — no DB row; always granted
    if user.is_admin_token:
        return user

    if not user_has_rack_access(user.user_id, user.role, rack_id, db):
        logger.warning(
            "auth: user=%s role=%s denied access to rack=%s (no assignment)",
            user.user_id,
            user.role,
            rack_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You are not assigned to rack {rack_id!r}.",
        )

    return user


# ---------------------------------------------------------------------------
# Public FastAPI dependency — Pi credential (presign endpoint ONLY)
# ---------------------------------------------------------------------------


async def require_pi_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
) -> str:
    """
    Dependency: validates the PI_API_KEY on /rack/{id}/presign.

    Rules (Section 4.2 / Section 9 Layer 2A):
    - Accepts X-API-Key header carrying settings.PI_API_KEY.
    - Explicitly rejects JWT / ADMIN_TOKEN presented as Bearer on this endpoint
      (mismatched credential type → 401 with an explicit message).
    - Explicitly rejects MQTT-style 64-hex credentials (→ 401 with message).
    - Missing key → 401.
    - Wrong key → 401.

    Returns the validated key string (the rack-specific credential issued at
    provisioning time).  Callers can use the key string as an identifier in
    audit log entries.
    """
    # Detect Bearer token on a Pi-only endpoint
    bearer_token = _extract_bearer(authorization)
    if bearer_token:
        logger.warning(
            "auth: Bearer token presented to /presign (Pi-only endpoint) — rejected"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "This endpoint only accepts X-API-Key authentication. "
                "Browser JWTs and ADMIN_TOKEN are not valid here."
            ),
        )

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-API-Key header required.",
        )

    # Detect MQTT credential heuristic
    if looks_like_mqtt_credential(x_api_key):
        logger.warning("auth: MQTT credential presented to /presign — rejected")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "MQTT credentials are not accepted on /presign. "
                "Use the presign_api_key issued during provisioning."
            ),
        )

    if not is_valid_pi_api_key(x_api_key):
        logger.warning("auth: invalid PI_API_KEY presented to /presign")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    return x_api_key
