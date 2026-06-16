"""
server/services/command_handler.py

Command whitelist, validation, and routing (Section 4.2 / Section 9 Layer 2A).

Whitelist
─────────
G28, M700, M701, M702, M703, M704, M710, M711, M114
!           emergency stop — QoS 2, bypasses lock/queue entirely
CAPTURE     intercepted by Pi bridge, not forwarded to Arduino
SCAN_START  routed to scan_engine (Stage 12)
SCAN_STOP   routed to scan_engine (Stage 12)

Rules enforced before MQTT is touched (Section 9 Layer 2A)
──────────────────────────────────────────────────────────
1. Command token not in ALLOWED_COMMANDS → CommandValidationError (→ HTTP 400)
2. M700/M701-M704: R param must be 0..grid_rows-1; C param must be 0..grid_cols-1
3. Empty command string → CommandValidationError

Routing table
─────────────
!            → CommandRoute.EMERGENCY  → queue_manager.submit_emergency()
CAPTURE      → CommandRoute.CAMERA     → camera_handler (Stage 10)
SCAN_START/  → CommandRoute.SCAN_ENGINE→ scan_engine (Stage 12)
SCAN_STOP
everything   → CommandRoute.MQTT_PUBLISH→ queue_manager.submit()
else

validate_command(raw, rack) is a pure function (no DB access, no side effects).
handle_command(rack_id, raw, user_id, db) is the full flow entry point.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ── Whitelist ─────────────────────────────────────────────────────────────────

ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "G28",
    "M700", "M701", "M702", "M703", "M704",
    "M705", "M706", "M707",   # Rack layout queries (relayed by Pi to Arduino)
    "M710", "M711",
    "M114",
    "M799",                    # Machine limits query
    "!",
    "CAPTURE",
    "SCAN_START",
    "SCAN_STOP",
})

# Subset that carries grid-position parameters and requires range validation
GRID_COMMANDS: frozenset[str] = frozenset({"M700", "M701", "M702", "M703", "M704"})

# Commands intercepted by the Pi bridge — never forwarded to the Arduino over serial
PI_ONLY_COMMANDS: frozenset[str] = frozenset({"CAPTURE", "SCAN_START", "SCAN_STOP"})


# ── Enumerations ──────────────────────────────────────────────────────────────

class CommandRoute(str, Enum):
    """Where a validated command should be dispatched."""
    MQTT_PUBLISH = "mqtt_publish"   # Normal motion / query command
    EMERGENCY    = "emergency"      # ! → emergency topic QoS 2
    CAMERA       = "camera"         # CAPTURE → camera_handler (Stage 10)
    SCAN_ENGINE  = "scan_engine"    # SCAN_START/STOP → scan_engine (Stage 12)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ParsedCommand:
    raw: str                            # Original string, e.g. "M700 R3 C5"
    cmd: str                            # Normalised token, e.g. "M700"
    params: dict[str, int] = field(default_factory=dict)  # e.g. {"R": 3, "C": 5}
    route: CommandRoute = CommandRoute.MQTT_PUBLISH


@dataclass
class CommandResult:
    """
    Returned by handle_command().
    outcome values: "published" | "queued" | "emergency" | "routed" | "error:…"
    detail is non-empty when outcome == "routed" (contains "camera" or "scan_engine").
    """
    outcome: str
    parsed: Optional[ParsedCommand] = None
    detail: str = ""


# ── Exceptions ────────────────────────────────────────────────────────────────

class CommandValidationError(ValueError):
    """
    Raised when a command fails whitelist or parameter validation.
    API routes catch this and return HTTP 400 with the message as the body.
    MQTT is never touched when this is raised (Section 9 Layer 2A).
    """
    pass


# ── Parsing ───────────────────────────────────────────────────────────────────

# Matches parameter tokens like "R3", "C12", "F-5"
_PARAM_RE = re.compile(r"([A-Za-z])(-?\d+)")


def parse_command(raw: str) -> ParsedCommand:
    """
    Tokenise a raw command string into a ParsedCommand.

    Examples:
        "G28"            → ParsedCommand(cmd="G28", params={})
        "M700 R3 C5"     → ParsedCommand(cmd="M700", params={"R":3,"C":5})
        "m700 r3 c5"     → same (case-insensitive normalisation)
        "!"              → ParsedCommand(cmd="!", route=EMERGENCY)

    Raises CommandValidationError on empty input.
    """
    tokens = raw.strip().split()
    if not tokens:
        raise CommandValidationError("Empty command string.")

    cmd = tokens[0].upper()
    params: dict[str, int] = {}
    for token in tokens[1:]:
        match = _PARAM_RE.fullmatch(token)
        if match:
            params[match.group(1).upper()] = int(match.group(2))

    return ParsedCommand(
        raw=raw.strip(),
        cmd=cmd,
        params=params,
        route=_route_for(cmd),
    )


def _route_for(cmd: str) -> CommandRoute:
    if cmd == "!":
        return CommandRoute.EMERGENCY
    if cmd == "CAPTURE":
        return CommandRoute.CAMERA
    if cmd in ("SCAN_START", "SCAN_STOP"):
        return CommandRoute.SCAN_ENGINE
    return CommandRoute.MQTT_PUBLISH


# ── Validation (pure function — no DB, no side effects) ───────────────────────

def validate_command(raw: str, rack) -> ParsedCommand:
    """
    Validate a raw command string against the whitelist and the rack's
    grid geometry.  Returns a ParsedCommand on success.

    `rack` must have attributes: .id (str), .grid_rows (int), .grid_cols (int).
    It may be an ORM Rack instance or any duck-typed object with those fields.

    Raises CommandValidationError if:
      • The command token is not in ALLOWED_COMMANDS.
      • M700/M701-M704 has an R param ≥ rack.grid_rows or < 0.
      • M700/M701-M704 has a C param ≥ rack.grid_cols or < 0.

    No MQTT is touched — this function is a pure validation gate.
    """
    parsed = parse_command(raw)

    # ── 1. Whitelist ─────────────────────────────────────────────────────────
    if parsed.cmd not in ALLOWED_COMMANDS:
        raise CommandValidationError(
            f"'{parsed.cmd}' is not a recognised command. "
            f"Allowed commands: {sorted(ALLOWED_COMMANDS)}"
        )

    # ── 2. Grid parameter validation ─────────────────────────────────────────
    if parsed.cmd in GRID_COMMANDS:
        if "R" in parsed.params:
            r = parsed.params["R"]
            if not (0 <= r < rack.grid_rows):
                raise CommandValidationError(
                    f"Row parameter R={r} is out of range for rack '{rack.id}' "
                    f"(grid_rows={rack.grid_rows}, valid: 0–{rack.grid_rows - 1})."
                )
        if "C" in parsed.params:
            c = parsed.params["C"]
            if not (0 <= c < rack.grid_cols):
                raise CommandValidationError(
                    f"Column parameter C={c} is out of range for rack '{rack.id}' "
                    f"(grid_cols={rack.grid_cols}, valid: 0–{rack.grid_cols - 1})."
                )

    logger.debug(
        "validate_command OK: rack=%s cmd=%s params=%s route=%s",
        rack.id, parsed.cmd, parsed.params, parsed.route.value,
    )
    return parsed


# ── Full flow (validation → routing → dispatch) ───────────────────────────────

def handle_command(
    rack_id: str,
    raw_command: str,
    user_id: Optional[str],
    db,
) -> CommandResult:
    """
    Full command-handling flow:

    1. Load rack from DB (needed for grid geometry check).
    2. validate_command() — raises CommandValidationError on failure.
    3. Route by parsed.route:
         EMERGENCY    → queue_manager.submit_emergency()
         CAMERA       → return CommandResult(outcome="routed", detail="camera")
         SCAN_ENGINE  → return CommandResult(outcome="routed", detail="scan_engine")
         MQTT_PUBLISH → queue_manager.submit() (handles lock / queue / publish)

    Returns CommandResult; never raises (errors go into outcome="error:...").
    API routes should catch CommandValidationError and return HTTP 400.
    """
    from db.models import Rack
    from core.queue_manager import queue_manager

    # Load rack for grid validation and existence check
    rack = db.query(Rack).filter_by(id=rack_id).first()
    if rack is None:
        return CommandResult(
            outcome="error:rack_not_found",
            detail=f"Rack '{rack_id}' does not exist.",
        )

    # validate_command raises CommandValidationError — let it propagate to the caller
    parsed = validate_command(raw_command, rack)

    logger.info(
        "handle_command: rack=%s user=%s cmd=%s route=%s",
        rack_id, user_id, parsed.cmd, parsed.route.value,
    )

    # Dispatch by route
    if parsed.route == CommandRoute.EMERGENCY:
        outcome = queue_manager.submit_emergency(rack_id)
        return CommandResult(outcome=outcome, parsed=parsed)

    if parsed.route == CommandRoute.CAMERA:
        from core.queue_manager import queue_manager
        from services.cache import cache
        from config.settings import settings as cfg

        # Acquire a capture lock, record attribution, then publish via queue_manager.
        # queue_manager.submit() handles lock acquisition internally; we set the
        # attribution BEFORE submit() so _on_image_message can read it immediately.
        cache.set_capture_attribution(
            rack_id=rack_id,
            operator_id=user_id or "",
            ttl_s=cfg.CAPTURE_LOCK_TIMEOUT_S,
        )
        outcome = queue_manager.submit(rack_id, "CAPTURE", user_id)
        return CommandResult(outcome=outcome, parsed=parsed, detail="camera")

    if parsed.route == CommandRoute.SCAN_ENGINE:
        from services.scan_engine import scan_engine
        cmd_token = parsed.cmd.upper()
        if cmd_token == "SCAN_START":
            outcome = scan_engine.trigger_manual_scan(rack_id, user_id or "", db)
            return CommandResult(outcome=outcome, parsed=parsed, detail="scan_engine")
        elif cmd_token == "SCAN_STOP":
            scan_engine.send_scan_stop(rack_id)
            return CommandResult(outcome="published", parsed=parsed, detail="scan_engine")
        # Fallback for any other scan commands
        return CommandResult(outcome="routed", parsed=parsed, detail="scan_engine")

    # MQTT_PUBLISH — route through queue_manager (handles lock + queue + publish)
    # submit() manages its own db_session internally for lock acquisition.
    outcome = queue_manager.submit(rack_id, raw_command, user_id)
    return CommandResult(outcome=outcome, parsed=parsed)
