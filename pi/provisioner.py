"""
pi/provisioner.py

First-boot identity flow — Section 5.3.

Responsibilities
────────────────
1. Check if pi/config/device.conf already exists.
   If it does, exit immediately — the bridge can start normally.
   Safe to run on every boot; it is a no-op after successful provisioning.

2. Read the CPU serial from /proc/cpuinfo.
   For local testing (no real Pi hardware), set:
       GANTRY_MOCK_CPU_SERIAL=MOCK000000000001
   Never set this on a real Pi.

3. Read the provisioning secret and optional token from disk or env:
     Secret — GANTRY_PROVISIONING_SECRET_FILE (file path) or
              GANTRY_PROVISIONING_SECRET     (plain env var)
     Token  — GANTRY_PROVISION_TOKEN_FILE    (file path) or
              GANTRY_PROVISION_TOKEN         (plain env var)

4. POST {cpu_serial, provisioning_secret, provision_token?, pi_ip?} to
   {GANTRY_SERVER_URL}/provision (default http://localhost:8000).

5. Write the returned credentials into pi/config/device.conf (mode 0o600).
   This is the SAME file that pi/config/settings.py reads — no path config
   needed on either side.

6. Delete the provisioning_secret/token files from disk after success.

Exit codes
──────────
0 — provisioning complete (or device.conf already existed — no-op).
1 — unrecoverable error (bad secret, network failure, no pool IDs left).
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
# Constants
# ---------------------------------------------------------------------------

# device.conf is written to /etc/vivarium/device.conf — a permanent system
# path that survives git pulls, redeployments, and OS upgrades.  The /etc/
# location is owned root:pi (mode 750 for the directory, 600 for the file)
# so only the bridge/provisioner running as the pi user can read it, and no
# code update can accidentally overwrite or delete it.
#
# pi/config/settings.py reads from the same path.
DEVICE_CONF_PATH: Path = Path("/etc/vivarium/device.conf")

# Server base URL — provisioner runs before device.conf exists, so it reads
# from an env var rather than settings.py.
SERVER_URL: str = os.environ.get("GANTRY_SERVER_URL", "http://localhost:8000").rstrip("/")

# Mock CPU serial for local dev/CI (never set on a real Pi).
MOCK_CPU_SERIAL: Optional[str] = os.environ.get("GANTRY_MOCK_CPU_SERIAL")

# Secret / token — file paths (preferred) or plain env vars (Docker/local).
PROVISIONING_SECRET_FILE: Optional[str] = os.environ.get("GANTRY_PROVISIONING_SECRET_FILE")
PROVISIONING_SECRET_ENV: Optional[str] = os.environ.get("GANTRY_PROVISIONING_SECRET")

PROVISION_TOKEN_FILE: Optional[str] = os.environ.get("GANTRY_PROVISION_TOKEN_FILE")
PROVISION_TOKEN_ENV: Optional[str] = os.environ.get("GANTRY_PROVISION_TOKEN")

# Retry config for the /provision HTTP call
HTTP_TIMEOUT_S: int = int(os.environ.get("GANTRY_PROVISION_TIMEOUT_S", "30"))
HTTP_MAX_RETRIES: int = int(os.environ.get("GANTRY_PROVISION_RETRIES", "5"))
HTTP_RETRY_DELAY_S: float = float(os.environ.get("GANTRY_PROVISION_RETRY_DELAY_S", "10"))

# Single source of truth for the MediaMTX RTSP port.
# Written to BOTH device.conf [streaming] mediamtx_port AND mediamtx.yaml
# rtspAddress — changing this one constant keeps both files in sync.
_MEDIAMTX_DEFAULT_PORT: str = "8554"


# ===========================================================================
# Step 1 — Exit fast if already provisioned
# ===========================================================================

def _already_provisioned() -> bool:
    """Return True if pi/config/device.conf already exists."""
    return DEVICE_CONF_PATH.is_file()


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
    Write the credentials returned by /provision into pi/config/device.conf.

    This is the SAME file that pi/config/settings.py reads — the provisioner
    and settings.py both derive the path from __file__ so they always agree
    without any environment variable or hardcoded system path.

    File permissions: 0o600 (owner read/write only).
    Parent directory (pi/config/) is created if it does not exist.
    """
    device_id: str = data["device_id"]
    conf_path: Path = DEVICE_CONF_PATH

    # Create /etc/vivarium/ if it does not exist yet.
    # Permissions: 750 (root owns it, pi group can read/enter, world cannot).
    # The provisioner runs as root (via sudo / setup.sh), so we set the group
    # to "pi" explicitly so that the bridge service (User=pi) can read the dir.
    conf_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        import shutil, grp
        pi_gid = grp.getgrnam("pi").gr_gid
        shutil.chown(conf_path.parent, user="root", group="pi")
        conf_path.parent.chmod(0o750)
    except (KeyError, PermissionError, AttributeError):
        # On a dev machine or in CI there is no "pi" group — skip chown.
        pass

    parser = configparser.ConfigParser()

    # ── [identity] ─────────────────────────────────────────────────────────
    # cpu_serial is left blank — read from /proc/cpuinfo during provisioning
    # only; not stored here afterwards (Section 2.2).
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
    # broker_host priority: server response → GANTRY_MQTT_BROKER env var
    # (set by setup.sh) → "localhost" last-resort default.
    # setup.sh exports GANTRY_MQTT_BROKER so that even if the server omits
    # broker_host from its /provision response, device.conf gets the correct
    # host that the operator typed in during setup rather than "localhost".
    _broker_host: str = (
        data.get("broker_host")
        or os.environ.get("GANTRY_MQTT_BROKER")
        or "localhost"
    )
    parser["mqtt"] = {
        "broker_host": _broker_host,
        "broker_port": str(data.get("broker_port", 1883)),
        # [PROD ONLY] — flip via Ansible when hardening (Section 9)
        "mqtt_use_tls": "false",
        "ca_cert_path": "",
    }

    # ── [serial] ────────────────────────────────────────────────────────────
    # "auto" tells SerialHandler to scan /dev/ttyACM* and /dev/ttyUSB* and
    # use the first Arduino it finds — no need to know the port in advance.
    # Change to a fixed port (e.g. /dev/ttyACM0) only if you have multiple
    # serial devices and need to pin the Arduino to a specific one.
    parser["serial"] = {
        "serial_port": "auto",
        "serial_baud": "115200",
        # 10 s: enough for slow firmware responses without hanging forever.
        "serial_timeout_s": "10",
        "serial_retry_count": "1",
    }

    # ── [capture] ───────────────────────────────────────────────────────────
    # Pi-local capture directory. S3 upload stays off until both this flag
    # AND S3_ENABLED on the server are true (Section 5.4 / 12).
    parser["capture"] = {
        "capture_dir": f"/var/vivarium/{device_id}/captures",
        "batch_upload_enabled": "false",
        # true on a real Pi (/tmp is RAM-backed tmpfs); false for local dev.
        "tmp_is_tmpfs": "false",
    }

    # -- [streaming] ---------------------------------------------------------
    # mediamtx_port uses _MEDIAMTX_DEFAULT_PORT — the single constant shared
    # with _write_mediamtx_yaml() so both files always carry the same port.
    # Stream path name always matches device_id (Section 2.2 / 5.6).
    parser["streaming"] = {
        "mediamtx_port": _MEDIAMTX_DEFAULT_PORT,
        "stream_name": device_id,
    }

    # ── [scan] ──────────────────────────────────────────────────────────────
    parser["scan"] = {
        "scan_lock_keepalive_interval_s": "30",
    }

    # Write atomically: temp file → chmod 600 → rename (POSIX atomic swap).
    tmp_path = conf_path.with_suffix(".tmp")
    with tmp_path.open("w") as fh:
        parser.write(fh)

    # Set 600 before rename so there is no window where the file is world-readable.
    tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    tmp_path.rename(conf_path)

    # Fix ownership: provisioner runs as root, but vivarium-bridge.service
    # runs as User=pi.  Ensure pi can read device.conf.
    try:
        import shutil as _shutil
        _shutil.chown(conf_path, user="pi", group="pi")
        logger.info("device.conf ownership set to pi:pi")
    except (PermissionError, KeyError, AttributeError):
        logger.debug("Could not chown device.conf to pi:pi (dev machine — skipping)")

    logger.info("device.conf written to %s (mode 600)", conf_path)


# ===========================================================================
# Step 5b -- Write mediamtx.yaml from template
# ===========================================================================

def _write_mediamtx_yaml(
    device_id: str,
    rtsp_password: str,
    mediamtx_port: str = _MEDIAMTX_DEFAULT_PORT,
) -> None:
    """
    Generate pi/mediamtx/mediamtx.yaml by substituting placeholders in
    pi/mediamtx/mediamtx.example.yaml.

    Placeholders substituted:
        {{DEVICE_ID}}      -- this Pi's device_id
        {{RTSP_PASSWORD}}  -- issued by POST /provision
        {{MEDIAMTX_PORT}}  -- RTSP listen port (default _MEDIAMTX_DEFAULT_PORT)

    The output file is written mode 600 (contains rtsp_password).
    """
    template_path = Path(__file__).parent / "mediamtx" / "mediamtx.example.yaml"
    output_path   = Path(__file__).parent / "mediamtx" / "mediamtx.yaml"

    if not template_path.is_file():
        logger.warning(
            "mediamtx.example.yaml not found at %s -- skipping mediamtx.yaml generation.",
            template_path,
        )
        return

    content = template_path.read_text(encoding="utf-8")
    content = content.replace("{{DEVICE_ID}}",     device_id)
    content = content.replace("{{RTSP_PASSWORD}}", rtsp_password)
    content = content.replace("{{MEDIAMTX_PORT}}", mediamtx_port)

    tmp_path = output_path.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600 -- contains RTSP password
    tmp_path.rename(output_path)

    # Fix ownership: provisioner runs as root (via setup.sh sudo) but
    # vivarium-camera.service runs as User=pi.  Without chown the file is
    # root:root 600 and mediamtx gets "permission denied" on open().
    try:
        import shutil as _shutil
        _shutil.chown(output_path, user="pi", group="pi")
        logger.info("mediamtx.yaml ownership set to pi:pi")
    except (PermissionError, KeyError, AttributeError):
        # Dev machine / CI — no "pi" user; skip gracefully.
        logger.debug("Could not chown mediamtx.yaml to pi:pi (dev machine — skipping)")

    logger.info("mediamtx.yaml written to %s (mode 600)", output_path)


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
    logger.info("Config will be written to: %s", DEVICE_CONF_PATH)

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

    # ── Step 5: write device.conf (mode 600) to /etc/vivarium/device.conf ──
    try:
        _write_device_conf(result)
    except OSError as exc:
        logger.error(
            "Failed to write device.conf to %s: %s",
            DEVICE_CONF_PATH,
            exc,
        )
        sys.exit(1)

    # -- Step 5b: write mediamtx.yaml from template --------------------------
    # BUG-15 FIX: rtsp_password must be present; an empty password would leave
    # the RTSP stream unauthenticated. KeyError propagates to the outer
    # except RuntimeError block and exits with code 1.
    try:
        rtsp_password = result["rtsp_password"]
        if not rtsp_password:
            raise RuntimeError(
                "Server returned an empty rtsp_password — cannot configure "
                "MediaMTX without a password. Check server provisioning logic."
            )
    except KeyError:
        raise RuntimeError(
            "Server response missing 'rtsp_password' field — cannot configure "
            "MediaMTX. Check server provisioning endpoint."
        )

    # Pass mediamtx_port explicitly from the shared constant — do not rely
    # on the default parameter so that a future change to _MEDIAMTX_DEFAULT_PORT
    # is immediately visible at the call site.
    _write_mediamtx_yaml(
        device_id=result.get("device_id", "rack-unknown"),
        rtsp_password=rtsp_password,
        mediamtx_port=_MEDIAMTX_DEFAULT_PORT,
    )

    # -- Step 6: delete secret files from disk --------------------------------
    _delete_secret_files(secret_file_path, token_file_path)

    logger.info(
        "Provisioning complete.  Device ID: %s  "
        "Secret files deleted from disk.",
        result.get("device_id"),
    )


if __name__ == "__main__":
    main()
