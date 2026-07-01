"""
server/core/security.py

Section 4.2 / Section 9 Layer 2A — Auth helpers used by middleware/auth.py.

Responsibilities
----------------
1. JWT creation (create_access_token) and validation (decode_token).
   - Algorithm : HS256
   - Secret    : settings.JWT_SECRET_KEY
   - Lifetime  : settings.JWT_EXPIRE_MINUTES
   - Payload   : {"sub": user_id, "role": role, "exp": ...}

2. Password hashing (hash_password / verify_password) via passlib/bcrypt
   for the `users` table (Section 3.2).

3. ADMIN_TOKEN and PI_API_KEY validation helpers so callers never compare
   raw secrets inline.

4. Role + rack-assignment helpers:
   - is_admin(role)        → admins bypass user_rack_assignments
   - is_viewer(role)       → viewers are read-only regardless of assignment
   - user_has_rack_access(user_id, rack_id, db) → True if the user has an
     entry in user_rack_assignments OR is an admin.

All configuration is read from settings, never from os.environ directly.
"""

from __future__ import annotations

import logging
import bcrypt as _bcrypt_lib
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from config.settings import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
# passlib 1.7.4 is incompatible with bcrypt >= 4.x (missing __about__).
# We call the bcrypt library directly so the rest of the codebase is unchanged.


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain* suitable for storing in users.password_hash."""
    salt = _bcrypt_lib.gensalt()
    return _bcrypt_lib.hashpw(plain.encode(), salt).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the stored bcrypt *hashed* value."""
    return _bcrypt_lib.checkpw(plain.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

_ALGORITHM = "HS256"

# Claim keys — centralised here so middleware always uses the same strings.
_CLAIM_SUB  = "sub"   # user_id
_CLAIM_ROLE = "role"
_CLAIM_EXP  = "exp"


def create_access_token(user_id: str, role: str) -> str:
    """
    Create a signed JWT for a browser user.

    The token is valid for settings.JWT_EXPIRE_MINUTES minutes.
    Payload contains "sub" (user_id) and "role".
    """
    expire = datetime.now(tz=timezone.utc) + timedelta(
        minutes=settings.JWT_EXPIRE_MINUTES
    )
    payload = {
        _CLAIM_SUB:  user_id,
        _CLAIM_ROLE: role,
        _CLAIM_EXP:  expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=_ALGORITHM)


class TokenPayload:
    """Parsed JWT payload — raised exceptions are caught by middleware."""

    def __init__(self, user_id: str, role: str) -> None:
        self.user_id = user_id
        self.role    = role

    def __repr__(self) -> str:
        return f"TokenPayload(user_id={self.user_id!r}, role={self.role!r})"


def decode_token(token: str) -> TokenPayload:
    """
    Decode and validate a JWT.

    Raises
    ------
    jose.JWTError
        If the token is malformed, expired, or the signature is wrong.
    ValueError
        If mandatory claims ("sub" / "role") are absent.
    """
    payload = jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[_ALGORITHM],
    )
    user_id: Optional[str] = payload.get(_CLAIM_SUB)
    role:    Optional[str] = payload.get(_CLAIM_ROLE)
    if not user_id or not role:
        raise ValueError("JWT is missing required claims (sub / role)")
    return TokenPayload(user_id=user_id, role=role)



