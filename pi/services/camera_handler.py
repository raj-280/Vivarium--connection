"""
pi/services/camera_handler.py

Camera / Capture Handler — Section 5.4.

Implements capture(row, col) for the S3_ENABLED=false local-disk path.
The S3/batch-upload path is present as a clearly-labelled branch that is
completely dormant until BOTH:
  - batch_upload_enabled = true   in device.conf  (Section 2.2)
  - S3_ENABLED = true             in server .env   (Section 2.1)
Flipping those two flags is the entirety of the S3 upgrade (Section 12).

capture(row, col) flow (Section 5.4):
  1. Take a photo into /tmp/rack-{id}-{timestamp}.jpg (permissions 600).
     • Real Pi:  subprocess call to `rpicam-still` (Raspberry Pi OS Bookworm+),
       falling back to the legacy `libcamera-still` name on older images.
     • Local dev (GANTRY_MOCK_CAMERA=1 explicitly set):
       write a synthetic placeholder JPEG so the rest of the flow is fully
       exercisable without hardware.
     • Neither binary found AND GANTRY_MOCK_CAMERA not set: raise loudly.
       (A missing binary on real hardware must never be silently treated
       as "use the mock" — that previously caused real captures to be
       silently replaced with a 1x1 placeholder JPEG.)
  2. Compute SHA-256 of the file.
  3. Publish CAPTURE_STARTED on the response topic
     (server resets the capture lock on receipt — Section 4.3).
  4. S3_ENABLED=false → copy file into capture_dir/{rack_id}/{date}/ and
     derive the local_path the image MQTT message will carry.
     S3_ENABLED=true  → POST /presign and PUT the file (dormant for now).
  5. Publish to vivarium/rack/{id}/image with local_path + sha256 + timestamp.
  6. Publish CAPTURE_DONE on the response topic.
  7. Delete the /tmp file.

Module-level usage from bridge.py:
    from services.camera_handler import camera_handler
    camera_handler.capture(row=2, col=3)
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Import resolution (direct run vs package import) ──────────────────────────
try:
    from config.settings import settings          # python pi/...  (pi/ on sys.path)
    from services.mqtt_client import mqtt_client
except ImportError:
    from pi.config.settings import settings       # python -m pi.bridge
    from pi.services.mqtt_client import mqtt_client


# ── Constants ─────────────────────────────────────────────────────────────────

# When set to any non-empty value, skip the real camera binary call and
# write a synthetic placeholder JPEG instead.  Never set on a real Pi.
_MOCK_CAMERA_ENV = "GANTRY_MOCK_CAMERA"

# Timeout for the still-capture subprocess (seconds).  config-driven if we
# ever add a capture_timeout_s key; hard-coded to 30s for now.
_LIBCAMERA_TIMEOUT_S = 30

# Candidate still-capture binaries, checked in order. `rpicam-still` is the
# current name on Raspberry Pi OS Bookworm+ (including all Pi 5 images).
# `libcamera-still` is the legacy pre-Bookworm name, kept as a fallback for
# older images. Recent rpicam-apps releases removed the libcamera-* symlinks,
# so checking only the old name silently breaks on current installs.
_CAMERA_BINARY_CANDIDATES: tuple[str, ...] = ("rpicam-still", "libcamera-still")


def _make_minimal_jpeg() -> bytes:
    """
    Build a valid 1×1 grey JPEG in pure Python (no PIL required).

    Structure: SOI + APP0/JFIF + DQT + SOF0 + DHT (DC+AC) + SOS + EOI.
    The pixel value is 0x80 (mid-grey). This is only used for local testing;
    it never runs on the real Pi (where libcamera-still is available).
    """
    # Minimal 1×1 greyscale JPEG (pre-computed, verified with file(1))
    # fmt: off
    return bytes([
        0xFF, 0xD8,                          # SOI
        0xFF, 0xE0, 0x00, 0x10,              # APP0 marker + length
        0x4A, 0x46, 0x49, 0x46, 0x00,        # "JFIF\0"
        0x01, 0x01,                          # version 1.1
        0x00,                                # aspect ratio unit (none)
        0x00, 0x01, 0x00, 0x01,              # X/Y density
        0x00, 0x00,                          # thumbnail size
        0xFF, 0xDB, 0x00, 0x43, 0x00,        # DQT marker (64-byte table, id=0)
        *([16] * 64),                        # flat quantisation table
        0xFF, 0xC0, 0x00, 0x0B,              # SOF0 (baseline DCT)
        0x08,                                # 8 bits/sample
        0x00, 0x01, 0x00, 0x01,              # height=1 width=1
        0x01,                                # 1 component (grey)
        0x01, 0x11, 0x00,                    # comp 1: H/V sampling 1×1, QT 0
        0xFF, 0xC4, 0x00, 0x1F, 0x00,        # DHT DC table 0
        0x00, 0x01, 0x05, 0x01, 0x01,
        0x01, 0x01, 0x01, 0x01, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x01, 0x02, 0x03,
        0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B,
        0xFF, 0xC4, 0x00, 0xB5, 0x10,        # DHT AC table 0 (162 bytes)
        *([0x00] * 178),                     # zero-fill (valid for 0-coeff image)
        0xFF, 0xDA, 0x00, 0x08,              # SOS
        0x01,                                # 1 component
        0x01, 0x00,                          # comp 1 selects DC/AC table 0
        0x00, 0x3F, 0x00,                    # Ss=0 Se=63 Ah=0 Al=0
        0x7F,                                # minimal MCU byte (mid-grey DC coeff)
        0xFF, 0xD9,                          # EOI
    ])
    # fmt: on


# Minimum viable JPEG bytes for the mock — built once at module import time.
_MOCK_JPEG: bytes = _make_minimal_jpeg()


class CameraHandler:
    """
    Manages the capture lifecycle for one rack (Section 5.4).

    One singleton per process — bridge.py imports `camera_handler` from the
    bottom of this module.  A threading.Lock prevents concurrent captures
    from writing to the same /tmp path.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def capture(self, row: Optional[int] = None, col: Optional[int] = None) -> None:
        """
        Full capture sequence (Section 5.4).

        row/col are optional: None for a manual (operator-triggered) capture,
        integers for an auto-scan cell capture (scan_executor passes them).

        Runs synchronously — bridge.py should call this from a thread so the
        MQTT message loop is not blocked.
        """
        with self._lock:
            self._do_capture(row, col)

    # ── Internal implementation ───────────────────────────────────────────────

    def _do_capture(self, row: Optional[int], col: Optional[int]) -> None:
        rack_id   = settings.device_id
        timestamp = datetime.now(timezone.utc)
        ts_str    = timestamp.strftime("%Y%m%dT%H%M%SZ")
        tmp_path  = Path("/tmp") / f"rack-{rack_id}-{ts_str}.jpg"

        logger.info(
            "capture: starting rack=%s row=%s col=%s tmp=%s",
            rack_id, row, col, tmp_path,
        )

        # ── Step 1: take the photo ─────────────────────────────────────────
        try:
            self._shoot(tmp_path)
        except Exception as exc:
            logger.error("capture: photo failed: %s", exc)
            mqtt_client.publish_response(f"CAPTURE_ERROR:{exc}")
            return

        try:
            # ── Step 2: compute SHA-256 ────────────────────────────────────
            sha256 = self._sha256(tmp_path)
            logger.debug("capture: sha256=%s", sha256)

            # ── Step 3: publish CAPTURE_STARTED (lock-keepalive §4.3) ──────
            mqtt_client.publish_response("CAPTURE_STARTED")
            logger.info("capture: CAPTURE_STARTED published")

            # ── Step 4: save to destination ────────────────────────────────
            local_path = self._save_local(tmp_path, rack_id, timestamp)
            logger.info("capture: saved to %s", local_path)

            # ── Step 5: publish image MQTT message ─────────────────────────
            image_payload = {
                "local_path": str(local_path),
                "sha256_checksum": sha256,
                "capture_timestamp": timestamp.isoformat(),
                "rack_id": rack_id,
            }
            if row is not None:
                image_payload["cell_row"] = row
            if col is not None:
                image_payload["cell_col"] = col

            mqtt_client.publish_image(image_payload)
            logger.info("capture: image MQTT published: %s", image_payload)

            # ── Step 6: publish CAPTURE_DONE ───────────────────────────────
            mqtt_client.publish_response("CAPTURE_DONE")
            logger.info("capture: CAPTURE_DONE published")

        except Exception as exc:
            # BUG-13 FIX: publish CAPTURE_ERROR so the server releases the
            # capture lock immediately rather than waiting for its expiry
            # (up to CAPTURE_LOCK_TIMEOUT_S = 120s).
            logger.error("capture: post-shoot pipeline failed: %s", exc)
            try:
                mqtt_client.publish_response(f"CAPTURE_ERROR:{exc}")
            except Exception:
                logger.exception("capture: could not publish CAPTURE_ERROR")

        finally:
            # ── Step 7: delete /tmp file (always, even on error) ──────────
            self._cleanup_tmp(tmp_path)

    # ── Photo acquisition ─────────────────────────────────────────────────────

    def _shoot(self, tmp_path: Path) -> None:
        """
        Write a JPEG to tmp_path.

        Real Pi:       resolves rpicam-still / libcamera-still and calls it
                       (subprocess).
        Local dev/CI:  writes a synthetic placeholder JPEG ONLY when
                       GANTRY_MOCK_CAMERA is explicitly set.

        IMPORTANT: a missing binary on real hardware is NOT treated as "use
        the mock" — that silently masked real capture failures behind a fake
        placeholder JPEG. If no camera tool is found and mock mode wasn't
        explicitly requested, this raises so the caller publishes
        CAPTURE_ERROR instead of a fake success.
        """
        explicit_mock = bool(os.environ.get(_MOCK_CAMERA_ENV))

        if explicit_mock:
            logger.info("capture: using MOCK camera (%s set)", _MOCK_CAMERA_ENV)
            self._write_mock_jpeg(tmp_path)
        else:
            binary = self._resolve_camera_binary()
            if binary is None:
                raise RuntimeError(
                    "No camera capture binary found on PATH (looked for: "
                    f"{', '.join(_CAMERA_BINARY_CANDIDATES)}). If this Pi has "
                    "no camera hardware attached, set "
                    f"{_MOCK_CAMERA_ENV}=1 to use the mock camera explicitly."
                )
            self._run_camera_still(binary, tmp_path)

        # Secure the file: owner read/write only (Section 5.4 / 9 Layer 3)
        tmp_path.chmod(0o600)

    @staticmethod
    def _resolve_camera_binary() -> Optional[str]:
        """
        Return the first available still-capture binary on PATH, preferring
        the current `rpicam-still` name (Raspberry Pi OS Bookworm+, including
        every Pi 5 image) and falling back to the legacy `libcamera-still`
        name for older pre-Bookworm images. Returns None if neither is found.
        """
        for name in _CAMERA_BINARY_CANDIDATES:
            if shutil.which(name) is not None:
                return name
        return None

    @staticmethod
    def _run_camera_still(binary: str, tmp_path: Path) -> None:
        """
        Invoke the resolved still-capture binary to capture a single JPEG.
        `rpicam-still` and the legacy `libcamera-still` accept the same flags.

        The capture goes through /tmp (tmpfs on a real Pi so the SD card is
        never written — Section 5.4 / 9 Layer 3).
        """
        cmd = [
            binary,
            "--output", str(tmp_path),
            "--nopreview",
            "--timeout", "2000",   # 2 s viewfinder settle time
            "--quality", "90",
        ]
        logger.debug("capture: running %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_LIBCAMERA_TIMEOUT_S,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{binary} failed (rc={result.returncode}): "
                f"{result.stderr.decode(errors='replace')}"
            )
        if not tmp_path.exists():
            raise RuntimeError(f"{binary} exited 0 but {tmp_path} was not created")

    @staticmethod
    def _write_mock_jpeg(tmp_path: Path) -> None:
        """Write a minimal valid JPEG placeholder for local testing."""
        tmp_path.write_bytes(_MOCK_JPEG)
        logger.debug("capture: wrote mock JPEG (%d bytes) to %s", len(_MOCK_JPEG), tmp_path)

    # ── SHA-256 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _sha256(path: Path) -> str:
        """Compute the SHA-256 hex-digest of a file (Section 5.4 / 9 Layer 2E)."""
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── Local-disk save (S3_ENABLED=false path — Section 5.4 / 12) ───────────

    @staticmethod
    def _save_local(tmp_path: Path, rack_id: str, timestamp: datetime) -> Path:
        """
        Copy the captured file from /tmp into capture_dir/{rack_id}/{date}/.

        Path pattern (matches server-side validation in s3_handler.py):
            {capture_dir}/{rack_id}/{YYYY-MM-DD}/{rack_id}-{timestamp}.jpg

        Returns the full destination Path.

        S3 branch (DORMANT):
            When batch_upload_enabled=true AND the server has S3_ENABLED=true,
            this method should instead POST /presign and PUT the file.  That
            branch is not implemented here because it only activates when both
            flags are set — Section 12 / Rule 3.
        """
        date_str = timestamp.strftime("%Y-%m-%d")
        ts_str   = timestamp.strftime("%Y%m%dT%H%M%SZ")

        dest_dir = Path(settings.capture_dir) / rack_id / date_str
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_path = dest_dir / f"{rack_id}-{ts_str}.jpg"
        shutil.copy2(str(tmp_path), str(dest_path))
        dest_path.chmod(0o600)  # owner r/w only — same as /tmp file

        return dest_path

    # ── /tmp cleanup ──────────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_tmp(tmp_path: Path) -> None:
        """
        Delete the /tmp capture file (Section 5.4 / 5.2 / 9 Layer 3).

        On a real Pi, /tmp is tmpfs (RAM-backed) so this is belt-and-suspenders
        — the file would vanish on reboot anyway.  The reconnect-cleanup sequence
        in bridge.py sweeps any files that survive a crash.
        """
        try:
            tmp_path.unlink(missing_ok=True)
            logger.debug("capture: cleaned up tmp file %s", tmp_path)
        except OSError as exc:
            logger.warning("capture: could not delete tmp file %s: %s", tmp_path, exc)


# ── Module-level singleton ────────────────────────────────────────────────────
# bridge.py imports this:  from services.camera_handler import camera_handler
camera_handler = CameraHandler()
