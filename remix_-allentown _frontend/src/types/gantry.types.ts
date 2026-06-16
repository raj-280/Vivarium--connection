/**
 * src/types/gantry.types.ts
 *
 * Section 7 — shared TypeScript types for all WebSocket message shapes,
 * gantry state, and UI state. Import from here in every component/context.
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
 * Periodic position update — relayed from M114 Arduino response.
 * data mirrors the fields the Pi publishes on vivarium/rack/{id}/response.
 */
export interface WsMsgStatus {
  type: 'status';
  rack_id: string;
  data: {
    status?: 'online' | 'offline';
    reason?: string;
    x?: number;
    y?: number;
    c?: number;
    homed_x?: boolean;
    homed_y?: boolean;
    homed_c?: boolean;
    scan_state?: ScanState;
    camera_status?: 'online' | 'offline' | 'unknown';
    /** heartbeat tick from the Pi */
    ts?: string;
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
 * capture_complete — routed only to the operator who holds the lock.
 * Viewers never receive this (Section 4.3).
 */
export interface WsMsgCaptureComplete {
  type: 'capture_complete';
  rack_id: string;
  data: {
    s3_key?: string;
    local_path?: string;
    sha256?: string;
    cell_row?: number;
    cell_col?: number;
    capture_timestamp?: string;
  };
}

/**
 * scan_cell_complete — broadcast to all rack subscribers (Section 4.3).
 */
export interface WsMsgScanCellComplete {
  type: 'scan_cell_complete';
  rack_id: string;
  data: {
    cell_row: number;
    cell_col: number;
    cells_completed: number;
    cells_total: number;
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
 */
export interface WsMsgAlert {
  type: 'alert';
  rack_id?: string;
  data: {
    level: 'info' | 'warning' | 'error';
    code: string;          // e.g. "re_homing", "maintenance_required"
    message: string;
  };
}

/** Scan resume/restart prompt after a manual-command conflict (Section 4.8) */
export interface WsMsgScanResumePrompt {
  type: 'scan_resume_prompt';
  rack_id: string;
  data: {
    last_completed_row: number;
    last_completed_col: number;
    expires_at: string;    // ISO — after this the scan restarts from beginning
  };
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
  | WsMsgScanResumePrompt;

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
 * Populated in SystemContext.subscribeRack() and used by GantryGrid to:
 *   - Render the grid with the correct row/column count
 *   - Compute the active cell from the gantry's X/Y position
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

