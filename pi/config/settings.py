"""
pi/config/settings.py

"""

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Path resolution ────────────────────────────────────────────────────────

DEFAULT_DEVICE_CONF_PATH = "/etc/gantry/device.conf"
ENV_OVERRIDE_VAR = "GANTRY_DEVICE_CONF"


def _resolve_conf_path() -> str:
    """
    Resolution order:
      1. GANTRY_DEVICE_CONF env var  (local dev — set this to any path)
      2. /etc/gantry/device.conf     (real Pi deployment, provisioned by setup.sh)
      3. device.conf next to this settings.py file  (development fallback —
         works automatically when running from the repo root without any .env
         or system-level install, e.g. `python -m pi.bridge` in the repo dir)

    If GANTRY_DEVICE_CONF is set but points at a file that doesn't exist
    (e.g. a stale value left in the shell/IDE env from a previous Pi-style
    test), we don't hard-fail here — we log a warning and fall through to
    steps 2/3 instead of returning a path that will never resolve. This used
    to be what bit local/dev runs: an override pointed at a missing
    /etc/gantry/device.conf-style path and silently shadowed the perfectly
    good pi/config/device.conf sitting right next to this file.
    """
    local_conf = Path(__file__).parent / "device.conf"

    override = os.environ.get(ENV_OVERRIDE_VAR)
    if override:
        if Path(override).is_file():
            logger.info("Using device.conf from %s=%s", ENV_OVERRIDE_VAR, override)
            return override
        logger.warning(
            "%s=%s is set but no file exists at that path. "
            "Falling back to default resolution instead of failing outright.",
            ENV_OVERRIDE_VAR,
            override,
        )

    if Path(DEFAULT_DEVICE_CONF_PATH).is_file():
        logger.info("Using device.conf from %s", DEFAULT_DEVICE_CONF_PATH)
        return DEFAULT_DEVICE_CONF_PATH

    # Development fallback: look for device.conf in the same directory as
    # this settings.py file (i.e. pi/config/device.conf).
    if local_conf.is_file():
        logger.info("Using device.conf from local dev fallback %s", local_conf)
        return str(local_conf)

    # No file found anywhere — return the default path anyway;
    # Settings.__init__ will detect loaded_from_file=False, log a clear
    # warning naming every path that was tried, and fall back to _DEFAULTS.
    logger.warning(
        "No device.conf found. Tried: %s, %s, %s. Using built-in defaults.",
        override or "(GANTRY_DEVICE_CONF not set)",
        DEFAULT_DEVICE_CONF_PATH,
        local_conf,
    )
    return DEFAULT_DEVICE_CONF_PATH


# ── Local-dev fallback defaults ──────────────────────────────────────────────
# Used only if the config file doesn't exist at all (e.g. before provisioning,
# or during early bridge.py development). Mirrors the groups in Section 2.2.

_DEFAULTS: dict[str, dict[str, str]] = {
    "identity": {
        "device_id": "rack-test",
        "cpu_serial": "",
    },
    "server": {
        "server_host": "http://localhost:8000",
        "presign_api_key": "",
        "mqtt_password": "",
        "rtsp_password": "",
    },
    "mqtt": {
        "broker_host": "localhost",
        "broker_port": "1883",
        "mqtt_use_tls": "false",
        "ca_cert_path": "",
    },
    "serial": {
        "serial_port": "/dev/ttyACM0",
        "serial_baud": "115200",
        "serial_timeout_s": "5",
        "serial_retry_count": "1",
    },
    "capture": {
        "capture_dir": "./captures",
        "batch_upload_enabled": "false",
        "tmp_is_tmpfs": "false",
    },
    "streaming": {
        "go2rtc_port": "8554",
        "go2rtc_stream_name": "rack-test",
    },
    "scan": {
        "scan_lock_keepalive_interval_s": "30",
    },
}


class Settings:
    """
    Typed view over device.conf (Section 2.2).

    Group → key mapping mirrors the table in Section 2.2 exactly:
      [identity]  device_id, cpu_serial
      [server]    server_host, presign_api_key, mqtt_password, rtsp_password
      [mqtt]      broker_host, broker_port, mqtt_use_tls, ca_cert_path
      [serial]    serial_port, serial_baud, serial_timeout_s, serial_retry_count
      [capture]   capture_dir, batch_upload_enabled, tmp_is_tmpfs
      [streaming] go2rtc_port, go2rtc_stream_name
      [scan]      scan_lock_keepalive_interval_s
    """

    def __init__(self, conf_path: Optional[str] = None) -> None:
        self.conf_path = conf_path or _resolve_conf_path()
        self._parser = configparser.ConfigParser()

        # Seed with defaults first so missing keys/sections still resolve.
        self._parser.read_dict(_DEFAULTS)

        if Path(self.conf_path).is_file():
            self._parser.read(self.conf_path)
            self.loaded_from_file = True
        else:
            self.loaded_from_file = False

        # ── Identity ──────────────────────────────────────────────────────
        self.device_id: str = self._get("identity", "device_id")
        self.cpu_serial: str = self._get("identity", "cpu_serial")

        # ── Server ────────────────────────────────────────────────────────
        self.server_host: str = self._get("server", "server_host")
        self.presign_api_key: str = self._get("server", "presign_api_key")
        self.mqtt_password: str = self._get("server", "mqtt_password")
        self.rtsp_password: str = self._get("server", "rtsp_password")

        # ── MQTT ──────────────────────────────────────────────────────────
        self.broker_host: str = self._get("mqtt", "broker_host")
        self.broker_port: int = self._get_int("mqtt", "broker_port")
        self.mqtt_use_tls: bool = self._get_bool("mqtt", "mqtt_use_tls")
        self.ca_cert_path: str = self._get("mqtt", "ca_cert_path")

        # ── Serial ────────────────────────────────────────────────────────
        self.serial_port: str = self._get("serial", "serial_port")
        self.serial_baud: int = self._get_int("serial", "serial_baud")
        self.serial_timeout_s: float = self._get_float("serial", "serial_timeout_s")
        self.serial_retry_count: int = self._get_int("serial", "serial_retry_count")

        # ── Camera / Capture ──────────────────────────────────────────────
        self.capture_dir: str = self._get("capture", "capture_dir")
        self.batch_upload_enabled: bool = self._get_bool("capture", "batch_upload_enabled")
        self.tmp_is_tmpfs: bool = self._get_bool("capture", "tmp_is_tmpfs")

        # ── Streaming ─────────────────────────────────────────────────────
        self.go2rtc_port: int = self._get_int("streaming", "go2rtc_port")
        self.go2rtc_stream_name: str = self._get("streaming", "go2rtc_stream_name")

        # ── Scan engine ───────────────────────────────────────────────────
        self.scan_lock_keepalive_interval_s: float = self._get_float(
            "scan", "scan_lock_keepalive_interval_s"
        )

    # ── Internal getters ──────────────────────────────────────────────────

    def _get(self, section: str, key: str) -> str:
        return self._parser.get(section, key, fallback=_DEFAULTS.get(section, {}).get(key, ""))

    def _get_int(self, section: str, key: str) -> int:
        return self._parser.getint(section, key, fallback=int(_DEFAULTS[section][key]))

    def _get_float(self, section: str, key: str) -> float:
        return self._parser.getfloat(section, key, fallback=float(_DEFAULTS[section][key]))

    def _get_bool(self, section: str, key: str) -> bool:
        return self._parser.getboolean(section, key, fallback=_DEFAULTS[section][key].lower() == "true")

    def __repr__(self) -> str:
        return (
            f"Settings(conf_path={self.conf_path!r}, "
            f"loaded_from_file={self.loaded_from_file}, "
            f"device_id={self.device_id!r}, "
            f"broker={self.broker_host}:{self.broker_port}, "
            f"serial_port={self.serial_port!r})"
        )


# ── Module-level singleton ───────────────────────────────────────────────────
# Other modules: `from config.settings import settings`
settings = Settings()
