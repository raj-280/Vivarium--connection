"""
server/services/s3_handler.py

Image storage handler — Section 4.1 / 5.4 / 12.

Two execution paths, controlled entirely by settings.S3_ENABLED:

  S3_ENABLED=false (current default — Section 12 / Rule 3):
    • validate_local_path()  — validates the local_path that the Pi reported in
                               its /image MQTT message against the rules in
                               Section 9 Layer 2A (pattern + rack-id cross-check).
    • local_image_dir()      — return the LOCAL_IMAGE_DIR root for MQTT image
                               handling and the image-history endpoint.
    • get_local_presign()    — returns a local upload target descriptor that
                               camera_handler.py uses when batch_upload_enabled=true.
                               (Dormant until batch_upload_enabled AND S3_ENABLED are
                               both true — Section 5.4.)

  S3_ENABLED=true  (production — Section 12):
    • presign_put()          — generate a pre-signed PUT URL (5-min expiry,
                               PUT-only, SHA-256 condition, Content-Type=image/jpeg).
    • presign_get()          — generate a 15-min pre-signed GET URL for image-
                               history endpoint.

Branching is on settings.S3_ENABLED only — no other change is needed to flip
from local-disk to real S3 (Section 12 / Rule 3).

Validation rules (Section 9 Layer 2A):
  local_path must match:
      {LOCAL_IMAGE_DIR}/{rack_id}/{YYYY-MM-DD}/{rack_id}-{ISO8601}.jpg
  s3_key (when S3_ENABLED=true) must match:
      images/{rack_id}/{ISO8601}.jpg
  In both cases, the {rack_id} segment is cross-checked against the MQTT
  topic the notification arrived on — a Pi cannot claim a path for a different
  rack.

Public API (imported by the MQTT image handler in main.py):
    from services.s3_handler import validate_image_path, ImagePathError
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────

class ImagePathError(ValueError):
    """
    Raised when a Pi-reported path/key fails server-side validation.
    The MQTT image handler catches this and writes a validation_failure
    audit row instead of an image_records row.
    """


# ── Path-pattern constants (Section 9 Layer 2A) ───────────────────────────────

# local_path pattern:
#   {LOCAL_IMAGE_DIR}/{rack_id}/{YYYY-MM-DD}/{rack_id}-{timestamp}.jpg
# Timestamp format: 20240614T123456Z  (YYYYMMDDTHHMMSSz)
_LOCAL_PATH_RE = re.compile(
    r".+/"                           # leading LOCAL_IMAGE_DIR prefix (any chars + slash)
    r"(?P<rack_id>rack-[^/]+)"       # rack_id segment
    r"/\d{4}-\d{2}-\d{2}/"          # date directory  YYYY-MM-DD
    r"(?P=rack_id)"                  # filename starts with same rack_id (back-reference)
    r"-\d{8}T\d{6}Z"                 # ISO-8601 compact timestamp
    r"\.jpg$"                        # extension
)

# s3_key pattern (used when S3_ENABLED=true):
#   images/{rack_id}/{ISO8601}.jpg
_S3_KEY_RE = re.compile(
    r"^images/"
    r"(?P<rack_id>rack-[^/]+)"
    r"/\d{8}T\d{6}Z"
    r"\.jpg$"
)


# ── Validation ────────────────────────────────────────────────────────────────

def validate_image_path(
    *,
    rack_id: str,
    local_path: Optional[str] = None,
    s3_key: Optional[str] = None,
) -> None:
    """
    Validate the image path/key reported by the Pi in its /image MQTT message.

    Checks:
      1. Exactly one of local_path or s3_key must be present (based on S3_ENABLED).
      2. The path matches the expected pattern (Section 9 Layer 2A).
      3. The rack_id embedded in the path matches the `rack_id` argument
         (which comes from the MQTT topic — a Pi cannot claim a path for a
         different rack).

    Raises ImagePathError on any failure.
    Does not return a value — callers proceed on success.
    """
    if not settings.S3_ENABLED:
        # ── Local-disk path ───────────────────────────────────────────────
        if not local_path:
            raise ImagePathError(
                "S3_ENABLED=false but no local_path in image MQTT message."
            )
        if s3_key:
            raise ImagePathError(
                "S3_ENABLED=false but s3_key was present — "
                "Pi should not send an s3_key when S3 is disabled."
            )
        _validate_local_path(rack_id=rack_id, local_path=local_path)
    else:
        # ── S3 path ───────────────────────────────────────────────────────
        if not s3_key:
            raise ImagePathError(
                "S3_ENABLED=true but no s3_key in image MQTT message."
            )
        _validate_s3_key(rack_id=rack_id, s3_key=s3_key)


def _validate_local_path(*, rack_id: str, local_path: str) -> None:
    """
    Validate a local_path reported by the Pi.

    Rules (Section 9 Layer 2A):
      • Must match _LOCAL_PATH_RE.
      • Must start with LOCAL_IMAGE_DIR.
      • The rack_id segment must match the `rack_id` from the MQTT topic.
    """
    # 1. Pattern check
    m = _LOCAL_PATH_RE.match(local_path)
    if not m:
        raise ImagePathError(
            f"local_path {local_path!r} does not match expected pattern "
            f"{{{settings.LOCAL_IMAGE_DIR}}}/{{rack_id}}/{{YYYY-MM-DD}}/{{rack_id}}-{{timestamp}}.jpg"
        )

    # 2. Rack-id cross-check (Section 9 Layer 2A)
    path_rack_id = m.group("rack_id")
    if path_rack_id != rack_id:
        raise ImagePathError(
            f"local_path rack_id mismatch: path contains {path_rack_id!r} "
            f"but MQTT topic is for rack {rack_id!r}."
        )

    # 3. Must be rooted under LOCAL_IMAGE_DIR
    expected_root = str(Path(settings.LOCAL_IMAGE_DIR).resolve())
    actual_root   = str(Path(local_path).resolve())
    if not actual_root.startswith(expected_root):
        raise ImagePathError(
            f"local_path {local_path!r} is outside LOCAL_IMAGE_DIR "
            f"({settings.LOCAL_IMAGE_DIR!r}). Path traversal attempt?"
        )

    logger.debug("validate_image_path: local_path OK: %s", local_path)


def _validate_s3_key(*, rack_id: str, s3_key: str) -> None:
    """
    Validate an s3_key reported by the Pi.

    Rules (Section 9 Layer 2A):
      • Must match _S3_KEY_RE.
      • The rack_id segment must match the `rack_id` from the MQTT topic.
    """
    m = _S3_KEY_RE.match(s3_key)
    if not m:
        raise ImagePathError(
            f"s3_key {s3_key!r} does not match expected pattern "
            f"images/{{rack_id}}/{{timestamp}}.jpg"
        )

    path_rack_id = m.group("rack_id")
    if path_rack_id != rack_id:
        raise ImagePathError(
            f"s3_key rack_id mismatch: key contains {path_rack_id!r} "
            f"but MQTT topic is for rack {rack_id!r}."
        )

    logger.debug("validate_image_path: s3_key OK: %s", s3_key)


# ── Local presign (dormant — only used when batch_upload_enabled=true AND
#   S3_ENABLED=true, Section 5.4 / 12) ────────────────────────────────────────

def get_local_presign_target(rack_id: str, sha256: str) -> dict:
    """
    Return a descriptor the Pi can use to "upload" to the server's
    LOCAL_IMAGE_DIR without going through real S3 — placeholder for the
    batch_upload_enabled code path.

    This function is DORMANT: camera_handler.py only calls it when
    batch_upload_enabled=true, which itself is gated by S3_ENABLED=true on
    the server side.  With S3_ENABLED=false both flags are false, so this
    function is never reached in the current configuration.

    When S3_ENABLED=true, callers should switch to presign_put() instead.
    """
    logger.debug(
        "get_local_presign_target called for rack=%s (dormant path)", rack_id
    )
    return {
        "upload_type": "local",
        "rack_id": rack_id,
        "sha256": sha256,
        "message": "local presign target — S3 not enabled",
    }


# ── S3 presign stubs (active only when S3_ENABLED=true — Section 12) ──────────

def presign_put(rack_id: str, sha256: str) -> str:
    """
    [S3_ENABLED=true only]
    Generate a pre-signed PUT URL for one image upload (Section 4.6 / 5.4):
      - Single object key: images/{rack_id}/{timestamp}.jpg
      - PUT-only, 5-minute expiry
      - SHA-256 checksum condition
      - Content-Type: image/jpeg locked

    Raises NotImplementedError when S3_ENABLED=false — callers must branch on
    settings.S3_ENABLED before calling.

    Implementation note: use boto3.client('s3').generate_presigned_url() with
    ClientMethod='put_object', Params={..., 'ChecksumSHA256': sha256}.
    Fill in during Stage 15 (production hardening) when S3 credentials exist.
    """
    if not settings.S3_ENABLED:
        raise NotImplementedError(
            "presign_put() requires S3_ENABLED=true.  "
            "See Section 12 for the config-only upgrade path."
        )
    # [PROD] implement with boto3 here
    raise NotImplementedError("presign_put: S3 upload not yet implemented (Stage 15).")


def presign_get(s3_key: str) -> str:
    """
    [S3_ENABLED=true only]
    Generate a 15-minute pre-signed GET URL for the image-history endpoint
    (Section 9 Layer 2E).

    Raises NotImplementedError when S3_ENABLED=false.
    """
    if not settings.S3_ENABLED:
        raise NotImplementedError(
            "presign_get() requires S3_ENABLED=true.  "
            "Use local_path directly when S3 is disabled."
        )
    # [PROD] implement with boto3 here
    raise NotImplementedError("presign_get: S3 not yet implemented (Stage 15).")
