/**
 * src/types/gantry.types.ts
 *
 * Section 7 — shared TypeScript types for all WebSocket message shapes,
 * gantry state, and UI state. Import from here in every component/context.
 *
 * FIXES applied in this version
 * ──────────────────────────────
 * • Mismatch 10: Added WsMsgCommandAck and included it in the WsMessage union.
 *   The server sends { type: "command_ack", rack_id, command, outcome } after
 *   every command dispatched via WebSocket; the type was previously missing
 *   from the union so TypeScript couldn't handle it.
 *
 * • Mismatch 4: WsMsgCaptureComplete.data now includes cell_row, cell_col,
 *   and capture_timestamp (fields the server always had in its DB schema but
 *   wasn't sending — now fixed in main.py _on_image_message). Also added
 *   image_record_id, trigger_type, and attribution_expired which the server
 *   has always sent but the frontend type omitted.
 *
 * • Mismatch 5 (documentation): WsMsgStatus.data carries heartbeat fields
 *   (status, camera_status, ts, device_id) only — NOT live position or homing
 *   data. Position data (x, y, c, homed_*) arrives via the raw "response"
 *   subtopic as M114 strings parsed by the server's position_monitor. The
 *   frontend must parse those separately; they do not arrive inside a
 *   WsMsgStatus envelope.
 */

// ---------------------------------------------------------------------------
// WebSocket message types (server → browser)
// ---------------------------------------------------------------------------

/** Sent immediately after auth succeeds on the /ws connection */
export interface WsMsgConnected {
  type: 'connected';
  user_id: string;
  role: UserRole;
}

/** Generic error message from server */
export interface WsMsgError {
  type: 'error';
  detail: string;
}

/** Subscription confirmed */
export interface WsMsgSubscribed {
  type: 'subscribed';
  rack_id: string;
}

/** Keepalive pong */
export interface WsMsgPong {
  type: 'pong';
}

/**
 * Pi heartbeat / Last Will message.
 *
 * NOTE (Mismatch 5): This message carries ONLY heartbeat fields (status,
 * camera_status, ts, device_id). It does NOT carry live position (x/y/c)
 * or homing flags. Those arrive on the "response" subtopic as raw M114
 * strings. The server's position_monitor parses them and stores them in
 * gantry_state; the frontend should read position via GET /rack/{id}/position
 * or parse response subtopic messages separately.
 */
export interface WsMsgStatus {
  type: 'status';
  rack_id: string;
  data: {
    /** 'online' from heartbeats, 'offline' from Last Will */
    status?: 'online' | 'offline';
    /** Reason included in Last Will messages */
    reason?: string;
    camera_status?: 'online' | 'offline' | 'unknown';
    /** ISO heartbeat timestamp from the Pi */
    ts?: string;
    device_id?: string;
  };
}

/** Lock acquired — server sends this over WS when a rack lock is taken */
export interface WsMsgLockAcquired {
  type: 'lock_acquired';
  rack_id: string;
  data: {
    lock_type: LockType;
    expires_at: string;
    holder_user_id: string;
    /** Included when lock_type === 'capture' or 'scan' */
    stream_url?: string;
  };
}

/** Lock released — server sends this to the ex-lock-holder */
export interface WsMsgLockReleased {
  type: 'lock_released';
  rack_id: string;
  data: { reason?: string };
}

/**
 * stream_close — sent by the server when the rack lock is released,
 * an emergency stop fires, or the lock-sweep daemon expires a lock.
 *
 * FIX: The server (streaming.py → build_stream_close) sends
 * { type: "stream_close", data: { rack_id } }. The previous frontend
 * code handled 'lock_released' instead of 'stream_close', so the
 * stream URLs were never cleared and CameraPanel kept trying to connect
 * to a dead WebRTC endpoint after every lock release.
 */
export interface WsMsgStreamClose {
  type: 'stream_close';
  rack_id?: string;
  data: { rack_id?: string };
}

/**
 * capture_complete — routed only to the operator who holds the lock.
 * Viewers never receive this (Section 4.3).
 *
 * FIX (Mismatch 4): Added cell_row, cell_col, capture_timestamp (which the
 * server now populates from the Pi image payload). Also added image_record_id,
 * trigger_type, and attribution_expired which the server has always sent.
 */
export interface WsMsgCaptureComplete {
  type: 'capture_complete';
  rack_id: string;
  data: {
    s3_key?: string;
    local_path?: string;
    sha256?: string;
    /** DB row id — present after successful image_records insert */
    image_record_id?: number;
    /** 'manual' | 'auto_scan' */
    trigger_type?: string;
    /** True if the operator attribution TTL expired before the image arrived */
    attribution_expired?: boolean;
    /** Grid cell coordinates — present for auto_scan captures */
    cell_row?: number;
    cell_col?: number;
    /** ISO timestamp of when the photo was taken on the Pi */
    capture_timestamp?: string;
  };
}

/**
 * scan_cell_complete — broadcast to all rack subscribers (Section 4.3).
 *
 * FIX (Mismatch 1): The Pi now sends cell_row/cell_col (not row/col),
 * so these field names now match what actually arrives.
 */
export interface WsMsgScanCellComplete {
  type: 'scan_cell_complete';
  rack_id: string;
  data: {
    cell_row: number;   // was: "row" on the Pi — now fixed to cell_row
    cell_col: number;   // was: "col" on the Pi — now fixed to cell_col
    cells_completed: number;
    cells_total: number;
    cells_failed?: number;
    cell_ok?: boolean;
    scan_session_id?: number;
    ts?: string;
  };
}

/** Scan lifecycle update */
export interface WsMsgScanStatus {
  type: 'scan_status';
  rack_id: string;
  data: {
    status: ScanState;
    last_completed_row?: number;
    last_completed_col?: number;
    abort_reason?: string;
    cells_completed?: number;
    cells_failed?: number;
    cells_total?: number;
    duration_s?: number;
  };
}

/**
 * stream_url — sent alongside lock_acquired for capture/scan locks.
 * CameraPanel opens the <video> element with this URL (Section 7 / 8).
 */
export interface WsMsgStreamUrl {
  type: 'stream_url';
  rack_id: string;
  data: {
    url: string;           // e.g. "/camera/api/webrtc?src=rack-047"
    mjpeg_url?: string;    // fallback
  };
}

/**
 * Escalation ladder alerts — maintenance_required, re-homing, etc. (Section 10)
 *
 * FIX (Mismatch 2): The server previously sent flat top-level fields
 * (severity=, detail=). It now sends the correct envelope with data.level
 * and data.message as defined here. No frontend type change needed —
 * this type was already correct; the server was the bug.
 */
export interface WsMsgAlert {
  type: 'alert';
  rack_id?: string;
  data: {
    /** Severity level — matches CSS/UI indicator colour */
    level: 'info' | 'warning' | 'error';
    code: string;          // e.g. "re_homing", "maintenance_required", "stale_homing"
    message: string;
  };
}

/**
 * Scan resume/restart prompt after a manual-command conflict (Section 4.8).
 *
 * FIX (Mismatch 3): expires_at is now an ISO 8601 timestamp string (not a
 * raw seconds integer). The server was previously sending timeout_s (number);
 * it now computes and sends expires_at so this field is directly usable as
 * new Date(expires_at) in the frontend without arithmetic.
 */
export interface WsMsgScanResumePrompt {
  type: 'scan_resume_prompt';
  rack_id: string;
  data: {
    scan_session_id?: number;
    last_completed_row: number;
    last_completed_col: number;
    grid_rows?: number;
    grid_cols?: number;
    /** ISO 8601 — after this timestamp the window closes and scan restarts */
    expires_at: string;
    message?: string;
  };
}

/**
 * command_ack — sent by the server after every command dispatched via
 * WebSocket (Section 4.2).
 *
 * FIX (Mismatch 10): This message type was sent by the server but was
 * completely absent from the WsMessage union, making it impossible to
 * handle in TypeScript without an unsafe cast. Now included.
 */
export interface WsMsgCommandAck {
  type: 'command_ack';
  rack_id: string;
  command: string;
  /** e.g. "published", "queued", "emergency", "error:..." */
  outcome: string;
}

/**
 * response — raw MQTT "response" subtopic relay.
 *
 * FIX: The server forwards all MQTT response-subtopic payloads to the
 * browser as { type: "response", data: <string|object> }. This message
 * type was handled in SystemContext's switch statement but was missing
 * from the WsMessage union, causing TypeScript to narrow `msg` to
 * `never` inside `case 'response':` and flag `msg.data` as a red error.
 *
 * data is `string` for raw M114 position lines and plain ACK strings
 * (e.g. "COMMAND_ACK:M700"), and a structured object for anything the
 * server JSON-encodes before forwarding.
 */
export interface WsMsgResponse {
  type: 'response';
  rack_id?: string;
  data: string | Record<string, unknown> | null;
}

/** Union of all server→browser message shapes */
export type WsMessage =
  | WsMsgConnected
  | WsMsgError
  | WsMsgSubscribed
  | WsMsgPong
  | WsMsgStatus
  | WsMsgLockAcquired
  | WsMsgLockReleased
  | WsMsgCaptureComplete
  | WsMsgScanCellComplete
  | WsMsgScanStatus
  | WsMsgStreamUrl
  | WsMsgAlert
  | WsMsgScanResumePrompt
  | WsMsgCommandAck
  | WsMsgResponse     // FIX: added — resolves 'never' error in SystemContext case 'response'
  | WsMsgStreamClose; // FIX: added — server sends stream_close on lock release / e-stop / sweep

// ---------------------------------------------------------------------------
// Browser → server WebSocket message shapes
// ---------------------------------------------------------------------------

export interface WsSendAuth {
  type: 'auth';
  token: string;
}

export interface WsSendPing {
  type: 'ping';
}

export interface WsSendSubscribe {
  type: 'subscribe';
  rack_id: string;
}

export interface WsSendUnsubscribe {
  type: 'unsubscribe';
  rack_id: string;
}

// ---------------------------------------------------------------------------
// Domain enums / value types
// ---------------------------------------------------------------------------

export type UserRole = 'viewer' | 'operator' | 'admin';

export type LockType = 'motion' | 'capture' | 'scan';

export type ScanState = 'idle' | 'running' | 'paused' | 'complete' | 'aborted';

/** WebSocket connection status shown by ConnectionBar */
export type WsStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected';

/** Live gantry position — mirrors racks table columns */
export interface GantryPosition {
  x: number | null;
  y: number | null;
  c: number | null;
  homed_x: boolean;
  homed_y: boolean;
  homed_c: boolean;
}

/** One cell in the GantryGrid component */
export interface GridCell {
  row: number;
  col: number;
  /** undefined = not yet scanned, true = captured ok, false = failed */
  captured?: boolean;
  /** Whether this cell is currently active (gantry is positioned here) */
  active?: boolean;
}

/** Auth state stored in context after a successful /auth/login */
export interface AuthState {
  token: string;
  userId: string;
  role: UserRole;
}

/** Shape of the /health endpoint response */
export interface HealthResponse {
  status: string;
  mqtt_connected: boolean;
  timestamp: string;
}

/**
 * Rack grid layout — mirrors GET /rack/{rack_id}/layout response.
 */
export interface RackLayout {
  rack_id: string;
  /** 'live' = from Arduino via M705-707 MQTT query, 'state' = from gantry_state, 'db' = from DB */
  source: 'live' | 'state' | 'db';
  rows: number;
  columns: number;
  pitch_x: number;  // mm between columns
  pitch_y: number;  // mm between rows
  offset_x: number; // X origin offset mm
  offset_y: number; // Y origin offset mm
  limit_x_mm?: number | null;
  limit_y_mm?: number | null;
  limit_c_mm?: number | null;
}
