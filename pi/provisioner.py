"""
pi/provisioner.py

First-boot identity flow — Section 5.3.

Responsibilities
────────────────
1. Check if /etc/gantry/device.conf (or GANTRY_DEVICE_CONF override) exists.
   If it does, exit immediately (milliseconds) — the bridge starts normally.
   This makes the vivarium-provisioner.service safe to run on every boot; it is
   a no-op after the first successful provisioning.

2. Read the CPU serial from /proc/cpuinfo.
   For local testing (no real Pi hardware), set the environment variable:
       GANTRY_MOCK_CPU_SERIAL=MOCK000000000001
   and any string is accepted instead.  Never set this on a real Pi.

3. Read the provisioning secret and (optional) provision token from disk/env.
   Baked into the SD card image at image-build time.  After provisioning they
   are deleted from disk so they can never be read again.

   Look-up order (either mechanism is fine):
     a. File:  GANTRY_PROVISIONING_SECRET_FILE (path) → read content, strip.
     b. Env:   GANTRY_PROVISIONING_SECRET (value, for Docker/local dev).

   Token (optional, pre-assigned flow):
     a. File:  GANTRY_PROVISION_TOKEN_FILE (path) → read content, strip.
     b. Env:   GANTRY_PROVISION_TOKEN (value).

4. POST {cpu_serial, provisioning_secret, provision_token?, pi_ip?} to
   {GANTRY_SERVER_URL}/provision (default http://localhost:8000).

5. Write the returned credentials into device.conf at mode 0o600.
   Path is GANTRY_DEVICE_CONF env var (default /etc/gantry/device.conf).
   Parent directory is created if it doesn't exist.

6. Delete the provisioning_secret file and provision_token file (if they
   were read from a file path) so they cannot be read after provisioning.
   Env-var-supplied secrets are gone as soon as the process exits.

Exit codes
──────────
0 — provisioning complete (or device.conf already existed — no-op).
1 — unrecoverable error (bad secret, network failure, no pool IDs left, etc.)
    Details are logged; the systemd unit will log them to the journal.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("GANTRY_LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("provisioner")

# ---------------------------------------------------------------------------
# Constants driven by environment variables — no hardcoded values (Section 2)
# ---------------------------------------------------------------------------

# Where to write device.conf — mirrors pi/config/settings.py resolution order
DEVICE_CONF_PATH: str = os.environ.get("GANTRY_DEVICE_CONF", "/etc/gantry/device.conf")

# Server base URL — provisioner runs before the Pi has device.conf, so it
# reads from the environment, not from settings.py (which requires device.conf).
SERVER_URL: str = os.environ.get("GANTRY_SERVER_URL", "http://localhost:8000").rstrip("/")

# Mock CPU serial for local dev/CI (never set on a real Pi — see §5.3 note).
MOCK_CPU_SERIAL: Optional[str] = os.environ.get("GANTRY_MOCK_CPU_SERIAL")

# Secret / token — file paths (preferred, mirrors SD-card image convention) or
# plain env vars (convenient for Docker/local tests).
PROVISIONING_SECRET_FILE: Optional[str] = os.environ.get("GANTRY_PROVISIONING_SECRET_FILE")
PROVISIONING_SECRET_ENV: Optional[str] = os.environ.get("GANTRY_PROVISIONING_SECRET")

PROVISION_TOKEN_FILE: Optional[str] = os.environ.get("GANTRY_PROVISION_TOKEN_FILE")
PROVISION_TOKEN_ENV: Optional[str] = os.environ.get("GANTRY_PROVISION_TOKEN")

# Retry config for the /provision HTTP call (transient network issues on first boot)
HTTP_TIMEOUT_S: int = int(os.environ.get("GANTRY_PROVISION_TIMEOUT_S", "30"))
HTTP_MAX_RETRIES: int = int(os.environ.get("GANTRY_PROVISION_RETRIES", "5"))
HTTP_RETRY_DELAY_S: float = float(os.environ.get("GANTRY_PROVISION_RETRY_DELAY_S", "10"))


# ===========================================================================
# Step 1 — Exit fast if already provisioned
# ===========================================================================

def _already_provisioned() -> bool:
    """Return True if device.conf exists at the configured path."""
    return Path(DEVICE_CONF_PATH).is_file()


# ===========================================================================
# Step 2 — CPU serial
# ===========================================================================

def _read_cpu_serial() -> str:
    """
    Read the CPU serial from /proc/cpuinfo (Raspberry Pi only).
    Falls back to GANTRY_MOCK_CPU_SERIAL for local development / CI.

    Raises RuntimeError if neither source works.
    """
    if MOCK_CPU_SERIAL:
        logger.warning(
            "Using mock CPU serial '%s' — do NOT set GANTRY_MOCK_CPU_SERIAL on a real Pi.",
            MOCK_CPU_SERIAL,
        )
        return MOCK_CPU_SERIAL

    cpuinfo_path = Path("/proc/cpuinfo")
    if not cpuinfo_path.is_file():
        raise RuntimeError(
            "/proc/cpuinfo not found and GANTRY_MOCK_CPU_SERIAL is not set. "
            "Set GANTRY_MOCK_CPU_SERIAL for local testing."
        )

    for line in cpuinfo_path.read_text().splitlines():
        if line.lower().startswith("serial"):
            # Format:  Serial          : 0000000012345678
            parts = line.split(":", 1)
            if len(parts) == 2:
                serial = parts[1].strip()
                if serial:
                    logger.info("CPU serial: %s", serial)
                    return serial

    raise RuntimeError(
        "Could not find 'Serial' line in /proc/cpuinfo. "
        "If running off real Pi hardware, check that /proc/cpuinfo is readable."
    )


# ===========================================================================
# Step 3 — Provisioning secret and token
# ===========================================================================

def _read_from_file_or_env(
    file_env_var_value: Optional[str],
    plain_env_value: Optional[str],
    label: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Read a secret from a file path (preferred) or a plain env var.

    Returns (value, file_path_if_from_file):
      - value         — the secret string, or None if not found.
      - file_path     — the path the secret was read from (so we can delete it
                        after provisioning), or None if it came from an env var.
    """
    if file_env_var_value:
        p = Path(file_env_var_value)
        if p.is_file():
            value = p.read_text().strip()
            logger.info("Loaded %s from file %s", label, p)
            return value, str(p)
        logger.warning(
            "%s file path '%s' does not exist; checking env var fallback.",
            label,
            file_env_var_value,
        )

    if plain_env_value:
        logger.info("Loaded %s from environment variable.", label)
        return plain_env_value, None

    return None, None


def _load_provisioning_secret() -> tuple[str, Optional[str]]:
    """
    Return (provisioning_secret, file_path_or_None).
    Raises RuntimeError if not configured at all.
    """
    value, path = _read_from_file_or_env(
        PROVISIONING_SECRET_FILE,
        PROVISIONING_SECRET_ENV,
        "provisioning_secret",
    )
    if not value:
        raise RuntimeError(
            "Provisioning secret not found. "
            "Set GANTRY_PROVISIONING_SECRET_FILE (path to file on disk) "
            "or GANTRY_PROVISIONING_SECRET (plain env var for local dev)."
        )
    return value, path


def _load_provision_token() -> tuple[Optional[str], Optional[str]]:
    """
    Return (token_or_None, file_path_or_None).
    A missing token is valid — auto-assign flow will run.
    """
    return _read_from_file_or_env(
        PROVISION_TOKEN_FILE,
        PROVISION_TOKEN_ENV,
        "provision_token",
    )


# ===========================================================================
# Step 3b — Pi IP (best-effort; not strictly required)
# ===========================================================================

def _get_pi_ip() -> Optional[str]:
    """
    Best-effort: resolve the Pi's outbound IP by connecting a UDP socket to the
    server address.  Never actually sends a packet; just reads the local address
    the OS would use.  Returns None on any error rather than failing provisioning.
    """
    try:
        server_host = SERVER_URL.split("://", 1)[-1].split(":")[0]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((server_host, 80))
            return s.getsockname()[0]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not determine Pi IP: %s", exc)
        return None


# ===========================================================================
# Step 4 — POST /provision
# ===========================================================================

def _post_provision(
    cpu_serial: str,
    provisioning_secret: str,
    provision_token: Optional[str],
    pi_ip: Optional[str],
) -> dict:
    """
    POST to {SERVER_URL}/provision and return the parsed JSON response.

    Retries up to HTTP_MAX_RETRIES times with HTTP_RETRY_DELAY_S delay between
    attempts to handle transient network issues on first boot (e.g. the server
    might still be starting).

    Raises RuntimeError on a definitive failure (4xx) or after exhausting retries.
    """
    url = f"{SERVER_URL}/provision"
    payload: dict = {
        "cpu_serial": cpu_serial,
        "provisioning_secret": provisioning_secret,
    }
    if provision_token:
        payload["provision_token"] = provision_token
    if pi_ip:
        payload["pi_ip"] = pi_ip

    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            logger.info("POST %s (attempt %d/%d)", url, attempt, HTTP_MAX_RETRIES)
            response = requests.post(
                url,
                json=payload,
                timeout=HTTP_TIMEOUT_S,
                headers={"Content-Type": "application/json"},
            )

            if response.status_code == 200:
                data = response.json()
                logger.info(
                    "Provision success: device_id=%s",
                    data.get("device_id", "<unknown>"),
                )
                return data

            # 4xx — definitive failures, no point retrying
            if 400 <= response.status_code < 500:
                try:
                    detail = response.json().get("detail", response.text)
                except Exception:  # noqa: BLE001
                    detail = response.text
                raise RuntimeError(
                    f"Provisioning rejected by server (HTTP {response.status_code}): {detail}"
                )

            # 5xx — possibly transient; retry
            logger.warning(
                "Server returned HTTP %d on attempt %d: %s",
                response.status_code,
                attempt,
                response.text[:200],
            )

        except requests.RequestException as exc:
            logger.warning("Network error on attempt %d: %s", attempt, exc)

        if attempt < HTTP_MAX_RETRIES:
            logger.info("Retrying in %.0f seconds…", HTTP_RETRY_DELAY_S)
            time.sleep(HTTP_RETRY_DELAY_S)

    raise RuntimeError(
        f"Provisioning failed after {HTTP_MAX_RETRIES} attempts. "
        "Check server logs and network connectivity."
    )


# ===========================================================================
# Step 5 — Write device.conf at 600 permissions
# ===========================================================================

def _write_device_conf(data: dict) -> None:
    """
    Write the credentials returned by /provision into device.conf.

    The file follows the INI sections defined in Section 2.2 exactly:
      [identity], [server], [mqtt], [serial], [capture], [streaming], [scan]

    Static defaults (serial, capture, streaming, scan) are written from the
    built-in fallback values in pi/config/settings.py so the bridge can start
    immediately after provisioning without any further hand-editing.

    File permissions: 0o600 (owner read/write only).
    Parent directory is created with 0o700 if it doesn't exist.
    """
    device_id: str = data["device_id"]
    conf_path = Path(DEVICE_CONF_PATH)

    # Create parent directory if missing (e.g. /etc/gantry/)
    conf_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    parser = configparser.ConfigParser()

    # ── [identity] ─────────────────────────────────────────────────────────
    # cpu_serial is intentionally LEFT BLANK per Section 2.2:
    # "cpu_serial (read-only, not stored after provisioning)"
    parser["identity"] = {
        "device_id": device_id,
        "cpu_serial": "",
    }

    # ── [server] ────────────────────────────────────────────────────────────
    parser["server"] = {
        "server_host": data.get("server_host", SERVER_URL),
        "presign_api_key": data["presign_api_key"],
        "mqtt_password": data["mqtt_password"],
        "rtsp_password": data["rtsp_password"],
    }

    # ── [mqtt] ──────────────────────────────────────────────────────────────
    parser["mqtt"] = {
        "broker_host": data.get("broker_host", "localhost"),
        "broker_port": str(data.get("broker_port", 1883)),
        # [PROD ONLY] — inert locally; flip via Ansible when hardening (§9)
        "mqtt_use_tls": "false",
        "ca_cert_path": "",
    }

    # ── [serial] ────────────────────────────────────────────────────────────
    # Defaults from Section 2.2; operator edits these in place if hardware
    # differs (e.g. different port on a custom carrier board).
    parser["serial"] = {
        "serial_port": "/dev/ttyACM0",
        "serial_baud": "115200",
        "serial_timeout_s": "5",
        "serial_retry_count": "1",
    }

    # ── [capture] ───────────────────────────────────────────────────────────
    # S3 upload stays dormant until BOTH batch_upload_enabled=true here AND
    # S3_ENABLED=true on the server (Section 5.4 / 12).
    parser["capture"] = {
        "capture_dir": f"/var/vivarium/{device_id}/captures",
        "batch_upload_enabled": "false",
        # true on a real Pi (/tmp is RAM-backed tmpfs); false by default so
        # the provisioner doesn't assume it's running on the actual hardware.
        "tmp_is_tmpfs": "false",
    }

    # ── [streaming] ─────────────────────────────────────────────────────────
    # Stream name always matches device_id (Section 2.2 / 5.6).
    parser["streaming"] = {
        "go2rtc_port": "8554",
        "go2rtc_stream_name": device_id,
    }

    # ── [scan] ──────────────────────────────────────────────────────────────
    parser["scan"] = {
        "scan_lock_keepalive_interval_s": "30",
    }

    # Write atomically: write to a temp file then rename (POSIX atomic).
    tmp_path = conf_path.with_suffix(".tmp")
    with tmp_path.open("w") as fh:
        parser.write(fh)

    # Set 600 before rename so there's no window where the file is world-readable
    tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    tmp_path.rename(conf_path)

    logger.info("device.conf written to %s (mode 600)", conf_path)


# ===========================================================================
# Step 6 — Delete secrets from disk
# ===========================================================================

def _delete_secret_files(*paths: Optional[str]) -> None:
    """
    Securely delete provisioning secret/token files from disk after provisioning
    succeeds, so they cannot be read again (Section 5.3).

    Uses os.remove; on Linux with a kernel ≥ 3.1 the VFS page-cache entry is
    dropped immediately.  For stronger guarantees in production, use shred or
    set up an encrypted volume — that is a [PROD ONLY] hardening step (§9 Layer 3).
    """
    for path_str in paths:
        if path_str is None:
            continue
        p = Path(path_str)
        try:
            p.unlink(missing_ok=True)
            logger.info("Deleted secret file: %s", p)
        except OSError as exc:
            # Non-fatal — log a warning but don't abort; the credentials are
            # already written to device.conf and the server has rotated them.
            logger.warning("Could not delete secret file %s: %s", p, exc)


# ===========================================================================
# Main entry point
# ===========================================================================

def main() -> None:
    """Run the full first-boot provisioning flow (Section 5.3)."""

    # ── Step 1: exit immediately if already provisioned ────────────────────
    if _already_provisioned():
        logger.info(
            "device.conf already exists at %s — provisioning not needed, exiting.",
            DEVICE_CONF_PATH,
        )
        sys.exit(0)

    logger.info(
        "No device.conf found at %s — starting first-boot provisioning.",
        DEVICE_CONF_PATH,
    )

    # ── Step 2: read CPU serial ────────────────────────────────────────────
    try:
        cpu_serial = _read_cpu_serial()
    except RuntimeError as exc:
        logger.error("Cannot read CPU serial: %s", exc)
        sys.exit(1)

    # ── Step 3: load provisioning secret and optional token ────────────────
    try:
        provisioning_secret, secret_file_path = _load_provisioning_secret()
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    provision_token, token_file_path = _load_provision_token()
    if provision_token:
        logger.info("Pre-assigned token found — will request specific device_id.")
    else:
        logger.info("No provision token — will use auto-assign from device pool.")

    # ── Step 3b: best-effort Pi IP ─────────────────────────────────────────
    pi_ip = _get_pi_ip()
    if pi_ip:
        logger.info("Pi outbound IP: %s", pi_ip)

    # ── Step 4: POST /provision ────────────────────────────────────────────
    try:
        result = _post_provision(cpu_serial, provisioning_secret, provision_token, pi_ip)
    except RuntimeError as exc:
        logger.error("Provisioning failed: %s", exc)
        sys.exit(1)

    # ── Step 5: write device.conf (mode 600) ──────────────────────────────
    try:
        _write_device_conf(result)
    except OSError as exc:
        logger.error(
            "Failed to write device.conf to %s: %s\n"
            "If running locally, set GANTRY_DEVICE_CONF to a writable path "
            "(e.g. GANTRY_DEVICE_CONF=/tmp/device.conf).",
            DEVICE_CONF_PATH,
            exc,
        )
        sys.exit(1)

    # ── Step 6: delete secret files from disk ─────────────────────────────
    _delete_secret_files(secret_file_path, token_file_path)

    logger.info(
        "Provisioning complete.  Device ID: %s  "
        "Secret files deleted from disk.",
        result.get("device_id"),
    )


if __name__ == "__main__":
    main()
