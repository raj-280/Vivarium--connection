"""
server/db/models.py

SQLAlchemy ORM models for all 12 tables defined in Section 3 of the
implementation plan.  Column names, types, nullability, FK constraints, and the
UNIQUE constraint on image_records.s3_key are implemented exactly as specified
so the schema can be replayed against PostgreSQL with only a DATABASE_URL change.

Table order matters for FK resolution at create_all() time:
  users → racks → scan_sessions → image_records
  device_pool → provision_tokens
  (remaining tables have FK to racks or are standalone)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    PrimaryKeyConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ===========================================================================
# 3.2  users
# Must come before racks (racks.lock_holder_user_id → users.id)
# ===========================================================================
class User(Base):
    """
    Section 3.2 — authenticated human accounts.
    Roles: viewer / operator / admin
    """
    __tablename__ = "users"

    id = Column(String, primary_key=True, nullable=False)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)          # bcrypt via passlib
    role = Column(String, nullable=False)                   # viewer / operator / admin
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

    # Relationships
    rack_assignments = relationship("UserRackAssignment", back_populates="user", cascade="all, delete-orphan")
    locked_racks = relationship("Rack", back_populates="lock_holder", foreign_keys="Rack.lock_holder_user_id")
    triggered_images = relationship("ImageRecord", back_populates="triggered_by", foreign_keys="ImageRecord.triggered_by_operator")


# ===========================================================================
# 3.1  racks
# References users (lock_holder_user_id); referenced by nearly every table.
# ===========================================================================
class Rack(Base):
    """
    Section 3.1 — master record per physical rack: identity, geometry,
    live position/homing state, lock state, scan state, connectivity.
    """
    __tablename__ = "racks"

    id = Column(String, primary_key=True, nullable=False)               # e.g. rack-047
    display_name = Column(String, nullable=False)
    location = Column(String, nullable=True)
    pi_ip = Column(String, nullable=True)                               # Used by go2rtc pull list
    mqtt_username = Column(String, nullable=True)                       # Per-Pi MQTT identity
    mqtt_password_ref = Column(String, nullable=True)                   # MQTT credential, not raw in prod
    rtsp_password_ref = Column(String, nullable=True)                   # Handle, not raw secret
    presign_api_key_ref = Column(String, nullable=True)                 # Handle, not raw secret
    cpu_serial = Column(String, unique=True, nullable=True)             # Unique hardware ID for the Pi

    # Grid geometry — defaults from .env, overridable per rack
    grid_rows = Column(Integer, nullable=False)
    grid_cols = Column(Integer, nullable=False)
    x0_offset_mm = Column(Float, nullable=False, default=0.0)
    pitch_x_mm = Column(Float, nullable=False, default=50.0)
    y0_offset_mm = Column(Float, nullable=False, default=0.0)
    pitch_y_mm = Column(Float, nullable=False, default=50.0)
    position_tolerance_x_mm = Column(Float, nullable=False, default=3.0)
    position_tolerance_y_mm = Column(Float, nullable=False, default=2.0)

    # Connectivity status
    mqtt_status = Column(String, nullable=False, default="offline")     # online / offline
    camera_status = Column(String, nullable=False, default="unknown")   # online / offline / unknown

    # Live position — from most recent M114
    last_position_x = Column(Float, nullable=True)
    last_position_y = Column(Float, nullable=True)
    last_position_c = Column(Float, nullable=True)

    # Homing flags — from most recent M114 homed: X=?/Y=?/C=?
    homed_x = Column(Boolean, nullable=False, default=False)
    homed_y = Column(Boolean, nullable=False, default=False)
    homed_c = Column(Boolean, nullable=False, default=False)
    last_homed_at = Column(DateTime, nullable=True)                     # Guards 12h stale-homing check

    # Lock state (Section 4.3)
    lock_holder_user_id = Column(String, ForeignKey("users.id"), nullable=True)
    lock_type = Column(String, nullable=True)                           # motion / capture / scan
    lock_acquired_at = Column(DateTime, nullable=True)
    lock_expires_at = Column(DateTime, nullable=True)

    # Scan state (Section 4.7 / 4.8)
    scan_state = Column(String, nullable=False, default="idle")         # idle / running / paused / complete / aborted

    # Escalation state (Section 4.5)
    maintenance_required = Column(Boolean, nullable=False, default=False)

    # Machine limits (from M799 / LAYOUT_CONFIG — max travel per axis in mm)
    # Null until the first LAYOUT_CONFIG or M799 response is received from the Pi.
    limit_x_mm = Column(Float, nullable=True)
    limit_y_mm = Column(Float, nullable=True)
    limit_c_mm = Column(Float, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    lock_holder = relationship("User", back_populates="locked_racks", foreign_keys=[lock_holder_user_id])
    user_assignments = relationship("UserRackAssignment", back_populates="rack", cascade="all, delete-orphan")
    scan_sessions = relationship("ScanSession", back_populates="rack", cascade="all, delete-orphan")
    image_records = relationship("ImageRecord", back_populates="rack", cascade="all, delete-orphan")
    scan_schedule = relationship("ScanSchedule", back_populates="rack", uselist=False, cascade="all, delete-orphan")
    certificates = relationship("Certificate", back_populates="rack", cascade="all, delete-orphan")
    pending_command = relationship("PendingCommand", back_populates="rack", uselist=False, cascade="all, delete-orphan")
    capture_attribution = relationship("CaptureAttribution", back_populates="rack", uselist=False, cascade="all, delete-orphan")


# ===========================================================================
# 3.3  user_rack_assignments
# Composite PK on (user_id, rack_id).
# ===========================================================================
class UserRackAssignment(Base):
    """
    Section 3.3 — junction table: which racks an operator may command/lock/view.
    Admins bypass this table entirely; viewers are read-only regardless of assignment.
    """
    __tablename__ = "user_rack_assignments"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "rack_id"),
    )

    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    rack_id = Column(String, ForeignKey("racks.id"), nullable=False)

    # Relationships
    user = relationship("User", back_populates="rack_assignments")
    rack = relationship("Rack", back_populates="user_assignments")


# ===========================================================================
# 3.6  scan_sessions
# Referenced by image_records; must be defined before image_records.
# ===========================================================================
class ScanSession(Base):
    """
    Section 3.6 — per-scan lifecycle: status, cell counts, resume point, abort reason.
    """
    __tablename__ = "scan_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rack_id = Column(String, ForeignKey("racks.id"), nullable=False)
    status = Column(String, nullable=False)                             # running / paused / complete / aborted
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    cells_total = Column(Integer, nullable=False, default=0)
    cells_completed = Column(Integer, nullable=False, default=0)
    cells_failed = Column(Integer, nullable=False, default=0)
    last_completed_row = Column(Integer, nullable=True)                 # Resume point
    last_completed_col = Column(Integer, nullable=True)
    abort_reason = Column(String, nullable=True)                        # e.g. emergency_stop

    # Relationships
    rack = relationship("Rack", back_populates="scan_sessions")
    image_records = relationship("ImageRecord", back_populates="scan_session")


# ===========================================================================
# 3.4  image_records
# UNIQUE on s3_key enforced at DB level to deduplicate MQTT image notifications.
# ===========================================================================
class ImageRecord(Base):
    """
    Section 3.4 — one row per captured image.
    UNIQUE constraint on s3_key rejects duplicate MQTT image notifications at the
    DB level on both SQLite (raises IntegrityError) and PostgreSQL (raises 23505).
    """
    __tablename__ = "image_records"
    __table_args__ = (
        UniqueConstraint("s3_key", name="uq_image_records_s3_key"),
        # Duplicate-notification guard for the local-disk path (S3_ENABLED=false).
        # SQLite allows multiple NULLs in a UNIQUE column so this does not
        # interfere with rows where local_path is NULL (i.e. S3 rows).
        UniqueConstraint("local_path", name="uq_image_records_local_path"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    rack_id = Column(String, ForeignKey("racks.id"), nullable=False)

    # Storage path — exactly one of these is populated depending on S3_ENABLED
    s3_key = Column(String, nullable=True)                              # UNIQUE — see __table_args__
    local_path = Column(String, nullable=True)                         # Populated when S3_ENABLED=false

    sha256_checksum = Column(String, nullable=False)                   # Computed on Pi, verified on server
    triggered_by_operator = Column(String, ForeignKey("users.id"), nullable=True)
    trigger_type = Column(String, nullable=False)                       # manual / auto_scan
    scan_session_id = Column(Integer, ForeignKey("scan_sessions.id"), nullable=True)
    cell_row = Column(Integer, nullable=True)                           # Only for auto_scan images
    cell_col = Column(Integer, nullable=True)
    capture_timestamp = Column(DateTime, nullable=False)               # From Pi filename timestamp
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    rack = relationship("Rack", back_populates="image_records")
    triggered_by = relationship("User", back_populates="triggered_images", foreign_keys=[triggered_by_operator])
    scan_session = relationship("ScanSession", back_populates="image_records")


# ===========================================================================
# 3.5  audit_log
# Append-only; covers every security-relevant event.
# ===========================================================================
class AuditLog(Base):
    """
    Section 3.5 — append-only event log.
    event_type values: capture_triggered, presign_issued, upload_confirmed,
    image_notification_received, duplicate_image_notification, validation_failure,
    command_published, command_ack_missing, position_error, re_home_triggered,
    maintenance_flagged, stream_opened, stream_closed, provisioning_event, etc.
    """
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String, nullable=False)
    rack_id = Column(String, nullable=True)                             # Not a FK — rack may not exist yet
    user_id = Column(String, nullable=True)
    pi_credential_ref = Column(String, nullable=True)
    details = Column(Text, nullable=True)                              # JSON string — free-form context
    outcome = Column(String, nullable=False)                           # success / failure / flagged
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# ===========================================================================
# 3.7  scan_schedule
# PK is rack_id (one schedule per rack).
# ===========================================================================
class ScanSchedule(Base):
    """
    Section 3.7 — per-rack auto-scan schedule.
    APScheduler checks next_scan_at every minute.
    enabled is auto-set to False when racks.maintenance_required is True.
    """
    __tablename__ = "scan_schedule"

    rack_id = Column(String, ForeignKey("racks.id"), primary_key=True, nullable=False)
    interval_hours = Column(Float, nullable=False, default=24.0)
    next_scan_at = Column(DateTime, nullable=False)
    last_scan_started_at = Column(DateTime, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)

    # Relationships
    rack = relationship("Rack", back_populates="scan_schedule")





# ===========================================================================
# 3.10  certificates
# [PROD ONLY] — table created from day one but unused locally.
# ===========================================================================
class Certificate(Base):
    """
    Section 3.10 — TLS certificate metadata per rack.
    Populated by the Ansible cert-rotation job in production.
    Table exists from day one so the schema migrates cleanly; rows only appear
    once the production hardening pass (Stage 15) is in progress.
    """
    __tablename__ = "certificates"

    # Composite PK on (rack_id, cert_serial) — a rack can have a history of certs.
    rack_id = Column(String, ForeignKey("racks.id"), primary_key=True, nullable=False)
    cert_serial = Column(String, primary_key=True, nullable=False)
    issued_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    last_rotated_at = Column(DateTime, nullable=True)

    # Relationships
    rack = relationship("Rack", back_populates="certificates")


# ===========================================================================
# 3.11  pending_commands
# Used only when CACHE_BACKEND=sqlite.
# One outstanding command per rack; semantics mirror Redis pending_cmd:{rack_id}.
# ===========================================================================
class PendingCommand(Base):
    """
    Section 3.11 — SQLite cache-backend substitute for Redis pending_cmd:{rack_id}.
    Background task (Section 4.5) polls this every 2s; identical escalation semantics
    to the Redis key described in the architecture doc.
    """
    __tablename__ = "pending_commands"

    rack_id = Column(String, ForeignKey("racks.id"), primary_key=True, nullable=False)
    command = Column(String, nullable=False)
    operator_id = Column(String, nullable=True)
    published_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    retry_count = Column(Integer, nullable=False, default=0)
    timeout_at = Column(DateTime, nullable=False)

    # Relationships
    rack = relationship("Rack", back_populates="pending_command")


# ===========================================================================
# 3.12  capture_attribution
# Used only when CACHE_BACKEND=sqlite.
# Mirrors Redis key {rack_id → operator_id, expire_at: now+120s}.
# ===========================================================================
class CaptureAttribution(Base):
    """
    Section 3.12 — SQLite cache-backend substitute for the Redis capture-attribution
    TTL key.  Consumed (deleted) when the matching image notification arrives.
    If expired before the image arrives, image_records.triggered_by_operator is
    written as null and a validation_failure audit entry is written.
    """
    __tablename__ = "capture_attribution"

    rack_id = Column(String, ForeignKey("racks.id"), primary_key=True, nullable=False)
    operator_id = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)

    # Relationships
    rack = relationship("Rack", back_populates="capture_attribution")
