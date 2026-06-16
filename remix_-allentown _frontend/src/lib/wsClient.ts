/**
 * src/lib/wsClient.ts
 *
 * WebSocket singleton that:
 *   - Connects to VITE_WS_URL with exponential back-off reconnect
 *   - Sends the auth token as the first frame (Section 4.2 browser fallback)
 *   - Reads the CSRF cookie (csrftoken) and injects it as csrf_token field
 *     on every "command" or "CAPTURE" message (Section 7 / 9 Layer 1)
 *   - Exposes send() and subscribe/unsubscribe helpers
 *   - Fires onMessage / onStatusChange callbacks registered by SystemContext
 *
 * Import and call wsClient.init(token) once from SystemContext.
 * Never create a second WebSocket anywhere in the app.
 */

import appConfig from '../config/app.config';
import type { WsMessage, WsSendAuth } from '../types/gantry.types';

type StatusCallback = (status: 'connecting' | 'connected' | 'reconnecting' | 'disconnected') => void;
type MessageCallback = (msg: WsMessage) => void;

// ---------------------------------------------------------------------------
// Cookie helper — reads CSRF token from document.cookie
// ---------------------------------------------------------------------------
function getCsrfToken(): string | null {
  const name = appConfig.csrf.cookieName + '=';
  for (const part of document.cookie.split(';')) {
    const c = part.trimStart();
    if (c.startsWith(name)) return c.slice(name.length);
  }
  return null;
}

// ---------------------------------------------------------------------------
// WsClient class
// ---------------------------------------------------------------------------
class WsClient {
  private ws: WebSocket | null = null;
  private token: string | null = null;
  private reconnectDelay = appConfig.wsReconnectBaseMs;
  private intentionallyClosed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  private onStatusChange: StatusCallback | null = null;
  private onMessage: MessageCallback | null = null;

  // ── Public API ────────────────────────────────────────────────────────────

  /** Call once when the user logs in. */
  init(token: string) {
    this.token = token;
    this.intentionallyClosed = false;
    this._connect();
  }

  /** Disconnect cleanly (logout). */
  close() {
    this.intentionallyClosed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close(1000, 'logout');
    this.ws = null;
  }

  /**
   * Send a JSON message to the server.
   *
   * For "command" and "CAPTURE" message types, the CSRF token is automatically
   * injected as the csrf_token field (Section 7 / Section 9 Layer 1).
   */
  send(msg: Record<string, unknown>) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[wsClient] send() called while not connected — dropped:', msg);
      return;
    }

    const type = msg.type as string;
    let payload = { ...msg };

    // Inject CSRF token on command/CAPTURE messages
    if (type === 'command' || type === 'CAPTURE') {
      const csrf = getCsrfToken();
      if (csrf) payload.csrf_token = csrf;
    }

    this.ws.send(JSON.stringify(payload));
  }

  subscribe(rackId: string) {
    this.send({ type: 'subscribe', rack_id: rackId });
  }

  unsubscribe(rackId: string) {
    this.send({ type: 'unsubscribe', rack_id: rackId });
  }

  ping() {
    this.send({ type: 'ping' });
  }

  setOnStatusChange(cb: StatusCallback) { this.onStatusChange = cb; }
  setOnMessage(cb: MessageCallback)     { this.onMessage = cb; }

  get isOpen() {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  // ── Internal ──────────────────────────────────────────────────────────────

  private _connect() {
    this._setStatus('connecting');

    try {
      this.ws = new WebSocket(appConfig.wsUrl);
    } catch (e) {
      console.error('[wsClient] WebSocket construction failed:', e);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.info('[wsClient] connected to', appConfig.wsUrl);
      this.reconnectDelay = appConfig.wsReconnectBaseMs; // reset back-off

      // Send auth token as the first frame (Section 4.2 browser fallback)
      if (this.token) {
        const authMsg: WsSendAuth = { type: 'auth', token: this.token };
        this.ws!.send(JSON.stringify(authMsg));
      }
    };

    this.ws.onmessage = (event) => {
      let parsed: WsMessage;
      try {
        parsed = JSON.parse(event.data) as WsMessage;
      } catch {
        console.warn('[wsClient] non-JSON message received:', event.data);
        return;
      }

      // Mark as truly connected only after the server echoes "connected"
      if (parsed.type === 'connected') {
        this._setStatus('connected');
      }

      this.onMessage?.(parsed);
    };

    this.ws.onerror = (e) => {
      console.warn('[wsClient] WebSocket error:', e);
    };

    this.ws.onclose = (e) => {
      console.info('[wsClient] closed — code=%d reason=%s', e.code, e.reason);
      if (!this.intentionallyClosed) {
        this._setStatus('reconnecting');
        this._scheduleReconnect();
      } else {
        this._setStatus('disconnected');
      }
    };
  }

  private _scheduleReconnect() {
    if (this.intentionallyClosed) return;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);

    const delay = this.reconnectDelay;
    console.info('[wsClient] reconnecting in %dms…', delay);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectDelay = Math.min(
        this.reconnectDelay * 2,
        appConfig.wsReconnectMaxMs,
      );
      this._connect();
    }, delay);
  }

  private _setStatus(s: Parameters<StatusCallback>[0]) {
    this.onStatusChange?.(s);
  }
}

// Singleton — the entire app shares one WS connection.
const wsClient = new WsClient();
export default wsClient;
