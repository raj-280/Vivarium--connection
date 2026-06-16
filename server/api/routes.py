"""
server/api/routes.py

FastAPI HTTP routes — Section 4.2 / Section 9 Layer 2A.

Endpoints implemented in Stage 5
──────────────────────────────────
GET  /health                    — liveness check; no auth
POST /auth/login                — issue JWT from username/password
POST /rack/{rack_id}/command    — validate + route command; requires rack-operator
POST /rack/{rack_id}/lock       — acquire a motion/capture lock; requires rack-operator
DELETE /rack/{rack_id}/lock     — release lock; requires rack-operator (must be holder)
POST /rack/{rack_id}/presign    — [STUB] image pre-sign; requires Pi API key
POST /provision                 — [STUB] Pi provisioning; requires PROVISIONING_SECRET
GET  /devices                   — admin list of device_pool rows (pending + assigned)

Stubs
─────
/presign and /provision return 501 with a clear message so the server boots and
the route is registered; full logic lands in Stage 8 (provisioning) and Stage 10
(capture) respectively — no code changes to the route file will be needed then,
only the service functions they call.

Rate-limit
──────────
/rack/{rack_id}/command  uses limit_commands  (per-user, config-driven)
/rack/{rack_id}/presign  uses limit_presign   (2/min per Pi credential)
Both decorators are applied here; the Limiter is mounted in main.py.
"""


import logging
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config.settings import settings
from core.locking import LockResult, LockType, acquire_lock, release_lock
from core.security import verify_password
from core.security import create_access_token
from db.database import db_session, get_db
from db.models import Rack, User
from middleware.auth import (
    CurrentUser,
    require_admin,
    require_browser_user,
    require_pi_api_key,
    require_rack_operator,
)
from middleware.rate_limit import limit_commands, limit_presign
from services.command_handler import CommandValidationError, handle_command
from services.provisioning import ProvisionRequest, ProvisionResult, provision_device
from services.streaming import broadcast_stream_url, broadcast_stream_close

logger = logging.getLogger(__name__)

router = APIRouter()


# ===========================================================================
# Request / Response schemas
# ===========================================================================

class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: str

class CommandRequest(BaseModel):
    command: str

class CommandResponse(BaseModel):
    outcome: str
    detail: Optional[str] = None

class LockRequest(BaseModel):
    lock_type: str = "motion"   # motion | capture | scan

class LockResponse(BaseModel):
    result: str
    rack_id: str
    lock_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    holder_user_id: Optional[str] = None

class PresignResponse(BaseModel):
    message: str

class ProvisionResponse(BaseModel):
    """Full credential response returned to the Pi after successful provisioning."""
    device_id: str
    mqtt_username: str
    mqtt_password: str
    presign_api_key: str
    rtsp_password: str
    server_host: str
    broker_host: str
    broker_port: int


# ===========================================================================
# GET /health
# ===========================================================================

@router.get("/health", tags=["system"])
async def health():
    """
    Liveness probe — no authentication required.
    Returns 200 with MQTT + DB connectivity flags so the frontend
    ConnectionBar (Section 7) can show meaningful status.
    """
    from services.mqtt_client import mqtt_client

    return {
        "status": "ok",
        "mqtt_connected": mqtt_client.is_connected,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# ===========================================================================
# POST /auth/login
# ===========================================================================

@router.post("/auth/login", response_model=LoginResponse, tags=["auth"])
async def login(body: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate a browser user with username + bcrypt password.
    Returns a JWT that the frontend stores and sends as 'Authorization: Bearer <token>'.
    """
    user: Optional[User] = (
        db.query(User).filter(User.username == body.username).first()
    )
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
        )

    # Record login timestamp
    user.last_login_at = datetime.utcnow()
    db.commit()

    token = create_access_token(user_id=user.id, role=user.role)
    logger.info("Login: user=%s role=%s", user.id, user.role)

    return LoginResponse(
        access_token=token,
        role=user.role,
        user_id=user.id,
    )


# ===========================================================================
# POST /rack/{rack_id}/command
# ===========================================================================

@router.post(
    "/rack/{rack_id}/command",
    response_model=CommandResponse,
    tags=["rack"],
)
@limit_commands
async def send_command(
    request: Request,                                   # required by slowapi
    rack_id: str = Path(...),
    body: Annotated[CommandRequest, Body()] = ...,
    current_user: CurrentUser = Depends(require_rack_operator),
    db: Session = Depends(get_db),
):
    """
    Validate and dispatch a gantry command (Section 4.2 / 4.3).

    - Whitelist check + M700/M701-704 grid-range validation run before MQTT.
    - Emergency stop (!) bypasses the lock queue immediately (Section 4.3).
    - All other commands go through the queue_manager which handles locking,
      queuing, and pending-command tracking.
    """
    try:
        result = handle_command(
            rack_id=rack_id,
            raw_command=body.command,
            user_id=current_user.user_id,
            db=db,
        )
    except CommandValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if result.outcome.startswith("error:"):
        code = (
            status.HTTP_404_NOT_FOUND
            if "rack_not_found" in result.outcome
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=result.detail or result.outcome)

    # Release lock after emergency stop (Item 6 server-side).
    # queue_manager.submit_emergency already cleared the queue; now release the lock
    # so the rack is not left in a permanently locked state.
    if result.outcome == "emergency":
        with db_session() as db_rel:
            release_lock(rack_id=rack_id, db=db_rel)
        try:
            broadcast_stream_close(rack_id, current_user.user_id)
        except Exception:
            logger.exception(
                "broadcast_stream_close failed after e-stop rack=%s", rack_id
            )

    return CommandResponse(outcome=result.outcome, detail=result.detail or None)


# ===========================================================================
# POST /rack/{rack_id}/lock   — acquire
# DELETE /rack/{rack_id}/lock — release
# ===========================================================================

@router.post(
    "/rack/{rack_id}/lock",
    response_model=LockResponse,
    tags=["rack"],
)
async def acquire_lock(
    rack_id: str = Path(...),
    body: LockRequest = ...,
    current_user: CurrentUser = Depends(require_rack_operator),
    db: Session = Depends(get_db),
):
    """
    Explicitly acquire a rack lock (Section 4.3).

    The lock is also acquired implicitly by /command — this endpoint is for
    the frontend to request a lock before opening a camera stream (Section 8).
    """
    try:
        lock_type = LockType(body.lock_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid lock_type '{body.lock_type}'. Use: motion | capture | scan",
        )

    result = acquire_lock(
        rack_id=rack_id,
        user_id=current_user.user_id,
        lock_type=lock_type,
        db=db,
    )

    if result == LockResult.RACK_NOT_FOUND:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rack '{rack_id}' not found.",
        )

    if result == LockResult.ALREADY_LOCKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Rack '{rack_id}' is already locked.",
        )

    # Fetch the updated rack row to populate the response
    from db.models import Rack as RackModel
    rack = db.query(RackModel).filter_by(id=rack_id).first()

    # ── Stream URL (Section 8) ────────────────────────────────────────────
    # Send the go2rtc stream URL to the operator's WebSocket alongside the
    # lock confirmation, so CameraPanel.tsx can open the <video> element.
    # This is fire-and-forget — a WebSocket error here must not fail the
    # HTTP lock response.
    if result == LockResult.ACQUIRED:
        try:
            broadcast_stream_url(rack_id, current_user.user_id)
        except Exception:
            logger.exception(
                "broadcast_stream_url failed for rack=%s user=%s "
                "(lock still granted)", rack_id, current_user.user_id
            )

    return LockResponse(
        result=result.value,
        rack_id=rack_id,
        lock_type=rack.lock_type if rack else None,
        expires_at=rack.lock_expires_at if rack else None,
        holder_user_id=rack.lock_holder_user_id if rack else None,
    )


@router.delete(
    "/rack/{rack_id}/lock",
    response_model=LockResponse,
    tags=["rack"],
)
async def release_lock_endpoint(
    rack_id: str = Path(...),
    current_user: CurrentUser = Depends(require_rack_operator),
    db: Session = Depends(get_db),
):
    """
    Release the caller's lock on a rack (Section 4.3).

    Only the lock holder (or an admin) may release the lock.
    """
    from db.models import Rack as RackModel

    rack: Optional[RackModel] = db.query(RackModel).filter_by(id=rack_id).first()
    if rack is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rack '{rack_id}' not found.",
        )

    # Admins may force-release; operators may only release their own lock
    if (
        current_user.role != "admin"
        and rack.lock_holder_user_id != current_user.user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not hold the lock on this rack.",
        )

    released = release_lock(rack_id=rack_id, db=db)

    # ── Stream close (Section 8) ──────────────────────────────────────────
    # Send the stream_close signal so the browser tears down the <video>
    # element and frees the WebRTC PeerConnection.
    if released:
        try:
            broadcast_stream_close(rack_id, current_user.user_id)
        except Exception:
            logger.exception(
                "broadcast_stream_close failed for rack=%s user=%s",
                rack_id, current_user.user_id
            )

    return LockResponse(
        result="released" if released else "not_locked",
        rack_id=rack_id,
    )


# ===========================================================================
# POST /rack/{rack_id}/presign  [STUB — Stage 10]
# ===========================================================================

@router.post(
    "/rack/{rack_id}/presign",
    tags=["capture"],
)
@limit_presign
async def presign(
    request: Request,                                   # required by slowapi
    rack_id: str = Path(...),
    api_key: str = Depends(require_pi_api_key),
):
    """
    [STUB] Generate a pre-signed PUT URL for image upload (Section 4.6 / 5.4).

    Full implementation in Stage 10 (capture flow):
      - Validates the rack_id against the credential.
      - When S3_ENABLED=false → returns a local upload target.
      - When S3_ENABLED=true  → returns a real S3 pre-signed PUT URL.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Presign endpoint not yet implemented. Coming in Stage 10 (capture flow).",
    )


# ===========================================================================
# POST /provision  (Stage 8 — fully implemented)
# ===========================================================================

@router.post("/provision", response_model=ProvisionResponse, tags=["provisioning"])
async def provision(
    body: ProvisionRequest,
    db: Session = Depends(get_db),
):
    """
    First-boot Pi provisioning (Section 4.6 / 5.3).

    No authentication header required — the ``provisioning_secret`` in the
    request body acts as the shared secret that gates this endpoint.

    Flow:
      1. Validates ``provisioning_secret`` (401 if wrong).
      2. Validates optional ``provision_token`` against provision_tokens (3.9).
      3. If ``cpu_serial`` already assigned, returns existing credentials
         (idempotent — safe to reflash and retry).
      4. Assigns a device_id (token pre-assigned or next from pool).
      5. Generates mqtt_password, presign_api_key, rtsp_password (256-bit each).
      6. Upserts the racks row; marks token used; writes audit row.
    """
    result: ProvisionResult = provision_device(body, db)
    return ProvisionResponse(
        device_id=result.device_id,
        mqtt_username=result.mqtt_username,
        mqtt_password=result.mqtt_password,
        presign_api_key=result.presign_api_key,
        rtsp_password=result.rtsp_password,
        server_host=result.server_host,
        broker_host=result.broker_host,
        broker_port=result.broker_port,
    )



# POST /admin/users  — admin only — create a new user
# ===========================================================================

class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "operator"   # viewer | operator | admin

class CreateUserResponse(BaseModel):
    user_id: str
    username: str
    role: str

@router.post(
    "/admin/users",
    response_model=CreateUserResponse,
    tags=["admin"],
    status_code=201,
)
async def create_user(
    body: CreateUserRequest,
    current_user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Create a new user account (admin, operator, or viewer).
    Requires admin authentication.
    """
    import uuid
    from core.security import hash_password

    valid_roles = {"viewer", "operator", "admin"}
    if body.role not in valid_roles:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role '{body.role}'. Must be one of: {', '.join(sorted(valid_roles))}",
        )

    # Check if username already exists
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{body.username}' is already taken.",
        )

    user_id = f"{body.role}-{str(uuid.uuid4())[:8]}"
    new_user = User(
        id=user_id,
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(new_user)
    db.commit()

    logger.info("Admin %s created user username=%s role=%s", current_user.user_id, body.username, body.role)

    return CreateUserResponse(user_id=user_id, username=body.username, role=body.role)


# ===========================================================================
# POST /setup  — NO AUTH REQUIRED — first-run admin creation only
# ===========================================================================

class SetupRequest(BaseModel):
    username: str
    password: str

@router.post(
    "/setup",
    response_model=CreateUserResponse,
    tags=["setup"],
    status_code=201,
)
async def first_time_setup(body: SetupRequest, db: Session = Depends(get_db)):
    """
    Create the very first admin account with no authentication required.
    LOCKS ITSELF permanently once any admin user exists in the database.
    Safe to leave enabled — subsequent calls return 409 Conflict.
    """
    import uuid
    from core.security import hash_password

    # Lock: refuse if any admin already exists
    existing_admin = db.query(User).filter(User.role == "admin").first()
    if existing_admin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup already complete — an admin account already exists. Please log in.",
        )

    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{body.username}' is already taken.",
        )

    user_id = f"admin-{str(uuid.uuid4())[:8]}"
    new_admin = User(
        id=user_id,
        username=body.username,
        password_hash=hash_password(body.password),
        role="admin",
    )
    db.add(new_admin)
    db.commit()

    logger.info("First-time setup: admin user created username=%s", body.username)
    return CreateUserResponse(user_id=user_id, username=body.username, role="admin")


# ===========================================================================
# GET /rack/{rack_id}/position  — last known gantry position (no MQTT)
# ===========================================================================

@router.get(
    "/rack/{rack_id}/position",
    tags=["rack"],
)
async def get_position(
    rack_id: str = Path(...),
    current_user: CurrentUser = Depends(require_rack_operator),
):
    """
    Return the last known gantry position for a rack from the in-memory state.

    REST equivalent of the sample code's GET /position endpoint.
    The WebSocket stream also delivers position updates in real-time via M114
    response messages, but this endpoint is useful for initial page load or
    polling clients that do not maintain a WebSocket connection.

    Returns:
        x, y, c   — position in mm (null if never received)
        homed     — per-axis homed flags
        last_homed_at — ISO timestamp of last successful homing (null if never)
    """
    from core.state import gantry_state
    state = gantry_state.get(rack_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rack '{rack_id}' not found or has no state yet.",
        )
    return {
        "rack_id": rack_id,
        "x": state.last_position_x,
        "y": state.last_position_y,
        "c": state.last_position_c,
        "homed": {
            "x": state.homed_x,
            "y": state.homed_y,
            "c": state.homed_c,
        },
        "last_homed_at": (
            state.last_homed_at.isoformat() + "Z"
            if state.last_homed_at else None
        ),
        "mqtt_status": state.mqtt_status,
    }


# ===========================================================================
# GET /rack/{rack_id}/layout  — rack grid geometry (reads state/DB, no MQTT)
# ===========================================================================

@router.get(
    "/rack/{rack_id}/layout",
    tags=["rack"],
)
async def get_rack_layout(
    rack_id: str = Path(...),
    current_user: CurrentUser = Depends(require_rack_operator),
    db: Session = Depends(get_db),
    live: bool = False,  # ?live=true triggers M705/M706/M707 queries via MQTT
):
    """
    Return the rack grid layout (rows, columns, pitch, origin offset, limits).

    By default returns the DB-stored values (seeded during provisioning or
    updated from Pi LAYOUT_CONFIG messages on every reconnect).

    With ?live=true, publishes M705/M706/M707 to the Pi and waits up to 5s
    for the Arduino to respond with current values. Falls back to DB values
    on timeout. This is the MQTT equivalent of the sample code's /rack-layout
    endpoint which queries the Arduino directly over serial.

    The frontend uses this to:
      - Render the GantryGrid with correct dimensions
      - Compute which grid cell the gantry is currently over (position → cell)
      - Validate M700 row/col inputs against live limits
    """
    from core.state import gantry_state as _gs

    rack = db.query(Rack).filter_by(id=rack_id).first()
    if rack is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rack '{rack_id}' not found.",
        )

    if live:
        # Publish M705/M706/M707 and wait for Pi to relay Arduino responses
        import re as _re
        from services.layout_cache import layout_cache
        from services.mqtt_client import mqtt_client as _mc

        layout_cache.clear(rack_id)
        try:
            _mc.publish_command(rack_id, "M705")
            _mc.publish_command(rack_id, "M706")
            _mc.publish_command(rack_id, "M707")
        except Exception:
            logger.warning(
                "get_rack_layout: failed to publish M705/M706/M707 for rack=%s", rack_id
            )

        rows_line   = layout_cache.wait_for(rack_id, "M705", timeout_s=5.0)
        pitch_line  = layout_cache.wait_for(rack_id, "M706", timeout_s=5.0)
        offset_line = layout_cache.wait_for(rack_id, "M707", timeout_s=5.0)

        if rows_line and pitch_line and offset_line:
            rm = _re.search(r'ROWS=(\d+)\s+COLS=(\d+)', rows_line)
            pm = _re.search(r'Pitch\s+X=([-\d.]+)\s+Y=([-\d.]+)', pitch_line)
            om = _re.search(r'Offsets\s+X0=([-\d.]+)\s+Y0=([-\d.]+)', offset_line)
            if rm and pm and om:
                return {
                    "rack_id": rack_id,
                    "source": "live",
                    "rows": int(rm.group(1)),
                    "columns": int(rm.group(2)),
                    "pitch_x": float(pm.group(1)),
                    "pitch_y": float(pm.group(2)),
                    "offset_x": float(om.group(1)),
                    "offset_y": float(om.group(2)),
                    "limit_x_mm": rack.limit_x_mm,
                    "limit_y_mm": rack.limit_y_mm,
                    "limit_c_mm": rack.limit_c_mm,
                }

        logger.warning(
            "get_rack_layout: live query timed out for rack=%s — falling back to state/DB", rack_id
        )

    # Prefer in-memory state (most recently updated by LAYOUT_CONFIG on Pi reconnect)
    state = _gs.get(rack_id)
    if state and state.pitch_x_mm is not None:
        return {
            "rack_id": rack_id,
            "source": "state",
            "rows": state.grid_rows,
            "columns": state.grid_cols,
            "pitch_x": state.pitch_x_mm,
            "pitch_y": state.pitch_y_mm,
            "offset_x": state.x0_offset_mm,
            "offset_y": state.y0_offset_mm,
            "limit_x_mm": state.limit_x_mm,
            "limit_y_mm": state.limit_y_mm,
            "limit_c_mm": state.limit_c_mm,
        }

    # Fallback: DB row values (provisioned or last-known)
    return {
        "rack_id": rack_id,
        "source": "db",
        "rows": rack.grid_rows,
        "columns": rack.grid_cols,
        "pitch_x": rack.pitch_x_mm,
        "pitch_y": rack.pitch_y_mm,
        "offset_x": rack.x0_offset_mm,
        "offset_y": rack.y0_offset_mm,
        "limit_x_mm": rack.limit_x_mm,
        "limit_y_mm": rack.limit_y_mm,
        "limit_c_mm": rack.limit_c_mm,
    }


# ===========================================================================
# POST /rack/{rack_id}/query-limits  — publish M799 and return DB limits
# ===========================================================================

@router.post(
    "/rack/{rack_id}/query-limits",
    tags=["rack"],
)
async def query_machine_limits(
    rack_id: str = Path(...),
    current_user: CurrentUser = Depends(require_rack_operator),
    db: Session = Depends(get_db),
):
    """
    Publish M799 to the Pi and return the Arduino's machine limits (X/Y/C max travel).

    Server-side equivalent of serial_manager.query_limits() from the sample code.
    The response arrives asynchronously via MQTT and is stored in gantry_state + DB.
    This endpoint triggers the query and immediately returns the current DB values;
    the caller should re-fetch /layout after a short delay to see the updated limits.
    """
    from core.state import gantry_state
    from services.mqtt_client import mqtt_client as _mc

    rack = db.query(Rack).filter_by(id=rack_id).first()
    if rack is None:
        raise HTTPException(status_code=404, detail=f"Rack '{rack_id}' not found.")

    # Publish M799 to trigger an async LIMITS response from the Pi
    try:
        _mc.publish_command(rack_id, "M799")
    except Exception:
        logger.warning("query_machine_limits: failed to publish M799 for rack=%s", rack_id)

    return {
        "rack_id": rack_id,
        "limit_x_mm": rack.limit_x_mm,
        "limit_y_mm": rack.limit_y_mm,
        "limit_c_mm": rack.limit_c_mm,
        "note": "M799 published — limits will update via MQTT within seconds if Pi is online.",
    }
# ===========================================================================
# GET /racks  — list provisioned racks (admin or operator)
# ===========================================================================

class RackSummary(BaseModel):
    id: str
    display_name: Optional[str] = None
    location: Optional[str] = None
    mqtt_status: Optional[str] = None
    camera_status: Optional[str] = None

@router.get("/racks", response_model=list[RackSummary], tags=["rack"])
async def list_racks(
    current_user: CurrentUser = Depends(require_browser_user),
    db: Session = Depends(get_db),
):
    """Return all provisioned racks so the frontend rack-picker can populate."""
    rows = db.query(Rack).all()
    return [
        RackSummary(
            id=r.id,
            display_name=getattr(r, "display_name", None),
            location=getattr(r, "location", None),
            mqtt_status=getattr(r, "mqtt_status", None),
            camera_status=getattr(r, "camera_status", None),
        )
        for r in rows
    ]