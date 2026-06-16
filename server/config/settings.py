"""
server/config/settings.py

Single source of truth for every configuration key listed in Section 2.1 of
the implementation plan.  Every other module imports `settings` from here;
nothing reads os.environ directly.

Local-dev defaults are baked in so the server starts with no .env file at all.
Production values are supplied by writing a real .env (or injecting environment
variables in the container).  See server/.env.example for the full key list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # MQTT  (Section 2.1 — MQTT group)
    # -------------------------------------------------------------------------
    MQTT_BROKER: str = Field(default="localhost", description="Mosquitto broker hostname or IP")
    MQTT_PORT: int = Field(default=1883, description="1883 locally, 8883 in production (TLS)")
    MQTT_USERNAME: str = Field(default="", description="Blank → anonymous (local dev only)")
    MQTT_PASSWORD: str = Field(default="", description="Blank → anonymous (local dev only)")
    # [PROD ONLY] ↓
    MQTT_USE_TLS: bool = Field(default=False, description="[PROD ONLY] Enable TLS on MQTT connection")
    MQTT_TLS_CA_PATH: str = Field(default="", description="[PROD ONLY] Path to CA cert for MQTT TLS")

    # -------------------------------------------------------------------------
    # Server  (Section 2.1 — Server group)
    # -------------------------------------------------------------------------
    BACKEND_HOST: str = Field(default="127.0.0.1")
    BACKEND_PORT: int = Field(default=8000)
    CORS_ALLOWED_ORIGINS: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:3000"],
        description="Origins the browser is allowed to connect from",
    )

    # -------------------------------------------------------------------------
    # Auth  (Section 2.1 — Auth group)
    # -------------------------------------------------------------------------
    ADMIN_TOKEN: str = Field(
        default="dev-admin-token-change-in-production",
        description="Static bearer token for browser admin sessions (local dev only)",
    )
    PI_API_KEY: str = Field(
        default="dev-pi-api-key-change-in-production",
        description="Static API key accepted on /rack/{id}/presign (Pi identity)",
    )
    JWT_SECRET_KEY: str = Field(
        default="dev-jwt-secret-key-change-in-production-min-32-chars",
        description="HMAC key for signing JWTs; must be at least 32 chars",
    )
    JWT_EXPIRE_MINUTES: int = Field(default=60, description="JWT lifetime in minutes")
    # Can be True locally — cookie will be non-Secure but CSRF protection still works.
    CSRF_ENABLED: bool = Field(default=False, description="Enable CSRF double-submit cookie validation")
    # [PROD ONLY] ↓
    COOKIE_SECURE: bool = Field(
        default=False,
        description="[PROD ONLY] Mark auth cookies as Secure (requires HTTPS)",
    )

    # -------------------------------------------------------------------------
    # Provisioning  (Section 2.1 — Provisioning group)
    # -------------------------------------------------------------------------
    PROVISIONING_SECRET: str = Field(
        default="dev-provisioning-secret",
        description="Shared secret baked into SD card images; validated on POST /provision",
    )
    PROVISION_TOKEN_TTL_HOURS: int = Field(
        default=24,
        description="How long a pre-assigned provision token stays valid",
    )

    # -------------------------------------------------------------------------
    # Database  (Section 2.1 — Database group)
    # -------------------------------------------------------------------------
    DATABASE_URL: str = Field(
        default="sqlite:///./vivarium.db",
        description=(
            "SQLite locally.  Swap to postgresql://user:pass@host:5432/vivarium "
            "for production — no other code change needed."
        ),
    )

    # -------------------------------------------------------------------------
    # Cache / Rate-limit  (Section 2.1 — Cache group)
    # -------------------------------------------------------------------------
    CACHE_BACKEND: Literal["sqlite", "redis"] = Field(
        default="sqlite",
        description=(
            "sqlite → pending_commands / capture_attribution tables act as the cache. "
            "redis → Redis key/TTL semantics (requires REDIS_URL)."
        ),
    )
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Only used when CACHE_BACKEND=redis",
    )
    # slowapi limit strings — format "<count>/<period>" e.g. "60/minute"
    RATE_LIMIT_PER_USER_COMMANDS: str = Field(
        default="60/minute",
        description="Per-user rate limit applied to all command / lock endpoints",
    )
    RATE_LIMIT_PRESIGN: str = Field(
        default="2/minute",
        description="Stricter per-Pi rate limit on /rack/{id}/presign (Section 9 Layer 2A)",
    )

    # -------------------------------------------------------------------------
    # S3  (Section 2.1 — S3 group)
    # S3_ENABLED=false → local-disk path used throughout.
    # Flipping to true requires no code changes (Section 12).
    # -------------------------------------------------------------------------
    S3_ENABLED: bool = Field(
        default=False,
        description="When false, images go to LOCAL_IMAGE_DIR instead of S3",
    )
    AWS_ACCESS_KEY_ID: str = Field(default="")
    AWS_SECRET_ACCESS_KEY: str = Field(default="")
    AWS_REGION: str = Field(default="us-east-1")
    S3_BUCKET: str = Field(default="")
    S3_ENDPOINT_URL: str = Field(
        default="",
        description="Leave blank for real AWS; set to MinIO URL for local S3 testing",
    )
    LOCAL_IMAGE_DIR: str = Field(
        default="./images",
        description="Root directory for local image storage when S3_ENABLED=false",
    )

    # -------------------------------------------------------------------------
    # Timeouts  (Section 2.1 — Timeouts group, all in seconds)
    # -------------------------------------------------------------------------
    COMMAND_TIMEOUT_S: int = Field(
        default=10,
        description="Seconds before a sent command with no ACK triggers L1 retry",
    )
    MOTION_TIMEOUT_S: int = Field(
        default=30,
        description="Seconds before a motion lock auto-releases (L2 escalation if no M114)",
    )
    HOMING_TIMEOUT_S: int = Field(
        default=60,
        description="Seconds allowed for a G28 homing sequence to complete",
    )
    CAPTURE_LOCK_TIMEOUT_S: int = Field(
        default=120,
        description="Capture lock lifetime; reset on CAPTURE_STARTED keepalive",
    )
    MANUAL_VS_SCAN_RESUME_WINDOW_S: int = Field(
        default=300,
        description=(
            "Seconds the operator has to choose resume/restart after a manual "
            "command interrupts an auto-scan (Section 4.8)"
        ),
    )

    # -------------------------------------------------------------------------
    # Rack geometry defaults  (Section 2.1 — Rack geometry group)
    # Applied when a new rack row is created; each rack can override in its DB row.
    # -------------------------------------------------------------------------
    RACK_ROWS: int = Field(default=12, description="Default grid rows (12 × 7 = 84 cells)")
    RACK_COLS: int = Field(default=7, description="Default grid columns")
    X0_OFFSET_MM: float = Field(default=0.0)
    PITCH_X_MM: float = Field(default=50.0)
    Y0_OFFSET_MM: float = Field(default=0.0)
    PITCH_Y_MM: float = Field(default=50.0)
    POSITION_TOLERANCE_X_MM: float = Field(
        default=3.0,
        description="Max allowable X position error before L2 re-home (Section 4.4)",
    )
    POSITION_TOLERANCE_Y_MM: float = Field(
        default=2.0,
        description="Max allowable Y position error before L2 re-home (Section 4.4)",
    )

    # -------------------------------------------------------------------------
    # Streaming  (Section 2.1 — Streaming group)
    # -------------------------------------------------------------------------
    GO2RTC_INTERNAL_URL: str = Field(
        default="http://localhost:1984",
        description="go2rtc API base URL on the server (localhost-only, never exposed)",
    )
    GO2RTC_PROXY_PATH: str = Field(
        default="/camera",
        description="Nginx reverse-proxy prefix for go2rtc (WebRTC/MJPEG endpoints)",
    )
    STREAM_URL_TTL_S: int = Field(
        default=3600,
        description="How long a stream URL issued over /ws remains valid",
    )

    # -------------------------------------------------------------------------
    # Scan engine  (Section 2.1 — Scan engine group)
    # -------------------------------------------------------------------------
    SCAN_POSTPONE_MINUTES: int = Field(
        default=15,
        description="Minutes to delay a scheduled scan when the Pi is offline or locked",
    )
    SCAN_STAGGER_GROUP_SIZE: int = Field(
        default=5,
        description="Number of racks per stagger batch when many share a scan window",
    )
    SCAN_STAGGER_DELAY_MINUTES: int = Field(
        default=2,
        description="Offset between stagger batches (minutes)",
    )

    # -------------------------------------------------------------------------
    # Production toggles  (Section 2.1 — Production toggles group)
    # All default False/empty locally.  Flipping these is the entirety of the
    # production hardening pass (Stage 15 of Section 13).
    # -------------------------------------------------------------------------
    # [PROD ONLY] ↓
    TLS_ENABLED: bool = Field(default=False, description="[PROD ONLY] Require HTTPS/WSS")
    HSTS_ENABLED: bool = Field(default=False, description="[PROD ONLY] Add HSTS header")
    CSP_ENABLED: bool = Field(default=False, description="[PROD ONLY] Add Content-Security-Policy header")
    WIREGUARD_REQUIRED: bool = Field(default=False, description="[PROD ONLY] Reject requests not from WireGuard VPN")
    ANSIBLE_INVENTORY_PATH: str = Field(
        default="",
        description="[PROD ONLY] Path to Ansible inventory for ACL rollout and cert rotation",
    )


# ---------------------------------------------------------------------------
# Module-level singleton — every other module does:
#   from config.settings import settings
# ---------------------------------------------------------------------------
settings = Settings()
