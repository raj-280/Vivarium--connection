"""
pi/config/settings.py

Typed settings reader for pi/config/device.conf.

The config file path is ALWAYS:
    /etc/vivarium/device.conf

This is a permanent system path written ONCE by pi/provisioner.py on first
boot (mode 600, owned by the service user detected by setup.sh). It
survives git pulls and redeployments.

On a dev machine without a real Pi, create it manually:
    sudo mkdir -p /etc/vivarium
    sudo cp pi/device.conf.example /etc/vivarium/device.conf
    sudo chown $USER /etc/vivarium/device.conf

If the file does not exist, all values fall back to _DEFAULTS so the bridge
can start with sensible placeholder values without crashing.

For unit tests, pass conf_path= to the Settings() constructor.
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Config file location ──────────────────────────────────────────────────────
#
# Permanent system path: /etc/vivarium/device.conf
#
# This file is written ONCE by pi/provisioner.py on first boot (mode 600,
# owned by the service user detected by setup.sh) and then only read —
# never overwritten by a code update,
# git pull, or redeploy.  Keeping it in /etc/ means the Pi's identity and
# credentials survive the entire software lifecycle.
#
# DO NOT move this back into the repo/install directory; that would cause
# device.conf to be lost or exposed on every redeploy.

DEVICE_CONF_PATH: Path = Path("/etc/vivarium/device.conf")


# ── Built-in fallback defaults ────────────────────────────────────────────────
#
# Used ONLY when device.conf does not exist yet (before first provisioning, or
# during early local dev). Once the provisioner writes device.conf these are
# ignored — the file's values always take precedence.
#
# Keep in sync with what provisioner._write_device_conf() writes.

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
        # "auto" → SerialHandler.connect() scans /dev/ttyACM* and /dev/ttyUSB*
        # and uses the first Arduino it finds. Pin to a specific port (e.g.
        # /dev/ttyACM0) in device.conf only if you have multiple serial devices.
        "serial_port": "auto",
        "serial_baud": "115200",
        # 10 s: enough headroom for slow firmware responses (M700 rack moves,
        # homing sequences) without hanging forever on a disconnected Arduino.
        "serial_timeout_s": "10",
        "serial_retry_count": "1",
    },
    "capture": {
        "capture_dir": "./captures",
        "batch_upload_enabled": "false",
        # true on a real Pi (/tmp is RAM-backed tmpfs); false on dev machines.
        "tmp_is_tmpfs": "false",
    },
    "streaming": {
        "mediamtx_port": "8554",
        "stream_name": "rack-test",
    },
    "scan": {
        "scan_lock_keepalive_interval_s": "30",
    },
}


class Settings:
    """
    Typed view over /etc/vivarium/device.conf.

    Config file is always read from:
        /etc/vivarium/device.conf   (written once by provisioner.py on first boot)

    Group -> key mapping (Section 2.2):
      [identity]  device_id, cpu_serial
      [server]    server_host, presign_api_key, mqtt_password, rtsp_password
      [mqtt]      broker_host, broker_port, mqtt_use_tls, ca_cert_path
      [serial]    serial_port, serial_baud, serial_timeout_s, serial_retry_count
      [capture]   capture_dir, batch_upload_enabled, tmp_is_tmpfs
      [streaming] mediamtx_port, stream_name
      [scan]      scan_lock_keepalive_interval_s
    """

    def __init__(self, conf_path: Optional[str] = None) -> None:
        # conf_path is accepted only for unit tests that need to inject a
        # custom path. All production code leaves it None and gets the
        # /etc/vivarium/device.conf path automatically via DEVICE_CONF_PATH.
        self.conf_path: str = conf_path if conf_path is not None else str(DEVICE_CONF_PATH)

        self._parser = configparser.ConfigParser()

        # Seed with built-in defaults first so every key has a value even if
        # the file is missing or incomplete.
        self._parser.read_dict(_DEFAULTS)

        if Path(self.conf_path).is_file():
            # Read and normalise line endings before parsing.  device.conf is
            # often committed/transferred from Windows and arrives on the Pi
            # with \r\n endings.  configparser would then store keys as
            # "broker_host\r" instead of "broker_host", silently missing every
            # lookup and falling back to the _DEFAULTS that read_dict() seeded.
            _raw = Path(self.conf_path).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
            self._parser.read_string(_raw)
            self.loaded_from_file = True
            logger.info("Settings loaded from %s", self.conf_path)
        else:
            self.loaded_from_file = False
            logger.warning(
                "device.conf not found at %s — using built-in defaults. "
                "Run pi/provisioner.py to generate it.",
                self.conf_path,
            )

        # ── Identity ──────────────────────────────────────────────────────
        self.device_id: str          = self._get("identity", "device_id")
        self.cpu_serial: str         = self._get("identity", "cpu_serial")

        # ── Server ────────────────────────────────────────────────────────
        self.server_host: str        = self._get("server", "server_host")
        self.presign_api_key: str    = self._get("server", "presign_api_key")
        self.mqtt_password: str      = self._get("server", "mqtt_password")
        self.rtsp_password: str      = self._get("server", "rtsp_password")

        # ── MQTT ──────────────────────────────────────────────────────────
        self.broker_host: str        = self._get("mqtt", "broker_host")
        self.broker_port: int        = self._get_int("mqtt", "broker_port")
        self.mqtt_use_tls: bool      = self._get_bool("mqtt", "mqtt_use_tls")
        self.ca_cert_path: str       = self._get("mqtt", "ca_cert_path")

        # ── Serial ────────────────────────────────────────────────────────
        # serial_port == "auto" means SerialHandler will scan and pick
        # the first /dev/ttyACM* or /dev/ttyUSB* it finds.
        self.serial_port: str        = self._get("serial", "serial_port")
        self.serial_baud: int        = self._get_int("serial", "serial_baud")
        self.serial_timeout_s: float = self._get_float("serial", "serial_timeout_s")
        self.serial_retry_count: int = self._get_int("serial", "serial_retry_count")

        # ── Camera / Capture ──────────────────────────────────────────────
        self.capture_dir: str              = self._get("capture", "capture_dir")
        self.batch_upload_enabled: bool    = self._get_bool("capture", "batch_upload_enabled")
        self.tmp_is_tmpfs: bool            = self._get_bool("capture", "tmp_is_tmpfs")

        # ── Streaming ─────────────────────────────────────────────────────
        self.mediamtx_port: int            = self._get_int("streaming", "mediamtx_port")
        self.stream_name: str              = self._get("streaming", "stream_name")

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
        return self._parser.getboolean(
            section, key, fallback=_DEFAULTS[section][key].lower() == "true"
        )

    def __repr__(self) -> str:
        return (
            f"Settings(conf_path={self.conf_path!r}, "
            f"loaded_from_file={self.loaded_from_file}, "
            f"device_id={self.device_id!r}, "
            f"broker={self.broker_host}:{self.broker_port}, "
            f"serial_port={self.serial_port!r})"
        )


# ── Module-level singleton ────────────────────────────────────────────────────
# Other modules import:  from config.settings import settings
settings = Settings()
