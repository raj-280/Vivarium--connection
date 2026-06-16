"""
pi/config/settings.py

Single source of truth for every configuration key listed in Section 2.2 of
the implementation plan.  Every other module imports `settings` from here;
nothing reads the config file or os.environ directly.

The config file is INI-style (configparser), matching /etc/gantry/device.conf,
mode 600, as described in Section 2.2 / 5.3.

── Local testing override ───────────────────────────────────────────────────
Section 2.2 specifies the file lives at /etc/gantry/device.conf — writing
there normally requires root.  For local development and the socat-based
Stage 7 test, set the environment variable:

    GANTRY_DEVICE_CONF=/path/to/your/device.conf

and settings.py will read from that path instead.  No code change is needed
to go back to the real path — just unset the env var (or don't set it) on
the actual Pi.

If neither the override env var nor the real file exists, settings.py falls
back to a small set of local-dev defaults so bridge.py can still start (e.g.
to run provisioner.py for the first time, per Section 5.3).
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional


# ── Path resolution ────────────────────────────────────────────────────────

DEFAULT_DEVICE_CONF_PATH = "/etc/gantry/device.conf"
ENV_OVERRIDE_VAR = "GANTRY_DEVICE_CONF"


def _resolve_conf_path() -> str:
    """
    Resolution order:
      1. GANTRY_DEVICE_CONF env var (local testing — no root required)
      2. /etc/gantry/device.conf (real Pi deployment)
    """
    override = os.environ.get(ENV_OVERRIDE_VAR)
    if override:
        return override
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
