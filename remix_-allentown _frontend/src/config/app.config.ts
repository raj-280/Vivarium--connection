/**
 * src/config/app.config.ts
 *
 * Section 7 — single source of truth for all deployment-time configuration.
 * Every value reads from a VITE_* environment variable with a safe local default.
 * Never import process.env directly in any other file — import from here instead.
 */

const appConfig = {
  /** WebSocket URL for the /ws endpoint (Section 4.2 / 7) */
  wsUrl: import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws',

  /** Base URL for HTTP REST calls (auth/login, /command, /lock, etc.) */
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000',

  /** go2rtc WebRTC stream base path — Section 8 */
  streamBasePath: import.meta.env.VITE_STREAM_BASE_PATH ?? '/camera/api/webrtc',

  /** go2rtc MJPEG fallback path — Section 8 */
  mjpegBasePath: import.meta.env.VITE_MJPEG_BASE_PATH ?? '/camera/mjpeg',

  /** Default rack dimensions (mirrors RACK_ROWS / RACK_COLS server defaults) */
  rackRows: Number(import.meta.env.VITE_RACK_ROWS ?? 12),
  rackCols: Number(import.meta.env.VITE_RACK_COLS ?? 7),

  /** Available jog step sizes in mm (used by ManualJogging controls in LiveControl) */
  jogStepsMm: [1.0, 5.0, 10.0, 50.0],

  /** Default jog step index into jogStepsMm */
  defaultJogStepIndex: 0,

  /**
   * CSRF — must match server/middleware/csrf.py constants exactly.
   * Cookie name: "csrftoken"   Header name: "x-csrf-token"
   */
  csrf: {
    cookieName: 'csrftoken',
    headerName: 'X-CSRF-Token',
  },

  /**
   * Client-side capture spinner hard timeout (ms).
   * If capture_complete never arrives within this window, the spinner clears
   * and shows an error — Section 9 Layer 1.
   */
  captureTimeoutMs: 60_000,

  /**
   * WebSocket reconnect back-off (ms) — doubles each attempt, capped at 30s.
   */
  wsReconnectBaseMs: 1_000,
  wsReconnectMaxMs: 30_000,
} as const;

export default appConfig;
