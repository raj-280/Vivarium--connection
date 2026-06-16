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
import secrets
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


# ---------------------------------------------------------------------------
# Static token validation
# ---------------------------------------------------------------------------

def is_valid_admin_token(token: str) -> bool:
    """
    Return True if *token* matches settings.ADMIN_TOKEN.

    Uses a constant-time comparison to prevent timing attacks.
    """
    return secrets.compare_digest(token, settings.ADMIN_TOKEN)


def is_valid_pi_api_key(key: str) -> bool:
    """
    Return True if *key* matches settings.PI_API_KEY.

    PI_API_KEY is only accepted on /rack/{id}/presign — middleware/auth.py
    enforces that restriction; this helper only checks the value.
    """
    return secrets.compare_digest(key, settings.PI_API_KEY)


# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------

_VALID_ROLES = {"viewer", "operator", "admin"}


def is_admin(role: str) -> bool:
    """Admins bypass user_rack_assignments and have full write access."""
    return role == "admin"


def is_operator(role: str) -> bool:
    """Operators can send commands to racks they are assigned to."""
    return role == "operator"


def is_viewer(role: str) -> bool:
    """Viewers can only read state; they never receive capture_complete messages."""
    return role == "viewer"


def is_valid_role(role: str) -> bool:
    """Return True if *role* is one of the three known roles."""
    return role in _VALID_ROLES


# ---------------------------------------------------------------------------
# Rack-assignment check  (Section 3.3 / Section 4.2)
# ---------------------------------------------------------------------------

def user_has_rack_access(user_id: str, role: str, rack_id: str, db) -> bool:
    """
    Return True if the user may command / lock the given rack.

    Rules (Section 3.3 / Section 4.2):
    - admin  → always True (bypasses user_rack_assignments)
    - viewer → always False (read-only; they use a separate read endpoint)
    - operator → True only if a UserRackAssignment row exists for
                 (user_id, rack_id)

    *db* is a SQLAlchemy session.  The caller is responsible for managing its
    lifetime (use the FastAPI db dependency or db_session context manager).
    """
    if is_admin(role):
        return True
    if is_viewer(role):
        return False

    # Operator path — check user_rack_assignments
    from db.models import UserRackAssignment  # local import to avoid circular

    row = (
        db.query(UserRackAssignment)
        .filter_by(user_id=user_id, rack_id=rack_id)
        .first()
    )
    return row is not None


# ---------------------------------------------------------------------------
# Credential-type mismatch guard  (Section 4.2 / Section 9 Layer 2A)
# ---------------------------------------------------------------------------

def looks_like_mqtt_credential(value: str) -> bool:
    """
    Heuristic: an MQTT password is a 256-bit random hex string (64 hex chars)
    generated during provisioning (Section 4.6).  The PI_API_KEY has the same
    format, but it is the *same* static key for all Pis (stored in settings),
    whereas per-Pi MQTT passwords are stored only in device.conf.

    This function returns True if *value* is exactly 64 lowercase hex chars
    AND it does NOT equal the configured PI_API_KEY.  That combination is
    almost certainly a Pi's MQTT password being incorrectly presented to a
    browser endpoint, and middleware/auth.py uses this to emit a 401 with an
    explicit error rather than a generic "invalid credential".
    """
    if len(value) != 64:
        return False
    if not all(c in "0123456789abcdef" for c in value):
        return False
    # If it matches PI_API_KEY exactly it is a valid presign credential, not an MQTT one.
    return not secrets.compare_digest(value, settings.PI_API_KEY)
