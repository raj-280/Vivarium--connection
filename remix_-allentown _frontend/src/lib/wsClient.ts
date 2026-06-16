/**
 * src/lib/wsClient.ts
 */

import appConfig from '../config/app.config';
import type { WsMessage, WsSendAuth } from '../types/gantry.types';

type StatusCallback = (status: 'connecting' | 'connected' | 'reconnecting' | 'disconnected') => void;
type MessageCallback = (msg: WsMessage) => void;

function getCsrfToken(): string | null {
  const name = appConfig.csrf.cookieName + '=';
  for (const part of document.cookie.split(';')) {
    const c = part.trimStart();
    if (c.startsWith(name)) return c.slice(name.length);
  }
  return null;
}

class WsClient {
  private ws: WebSocket | null = null;
  private token: string | null = null;
  private reconnectDelay = appConfig.wsReconnectBaseMs;
  private intentionallyClosed = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingSubscribe: string | null = null;  // ← ADDED

  private onStatusChange: StatusCallback | null = null;
  private onMessage: MessageCallback | null = null;

  init(token: string) {
    this.token = token;
    this.intentionallyClosed = false;
    this._connect();
  }

  close() {
    this.intentionallyClosed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close(1000, 'logout');
    this.ws = null;
  }

  send(msg: Record<string, unknown>) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('[wsClient] send() called while not connected — dropped:', msg);
      return;
    }
    const type = msg.type as string;
    let payload = { ...msg };
    if (type === 'command' || type === 'CAPTURE') {
      const csrf = getCsrfToken();
      if (csrf) payload.csrf_token = csrf;
    }
    this.ws.send(JSON.stringify(payload));
  }

  subscribe(rackId: string) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({ type: 'subscribe', rack_id: rackId });  // ← CHANGED
    } else {
      this.pendingSubscribe = rackId;  // ← ADDED: queue it
    }
  }

  unsubscribe(rackId: string) {
    this.send({ type: 'unsubscribe', rack_id: rackId });
  }

  ping() {
    this.send({ type: 'ping' });
  }

  setOnStatusChange(cb: StatusCallback) { this.onStatusChange = cb; }
  setOnMessage(cb: MessageCallback) { this.onMessage = cb; }

  get isOpen() {
    return this.ws?.readyState === WebSocket.OPEN;
  }

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
      this.reconnectDelay = appConfig.wsReconnectBaseMs;
      // ← CHANGED: added readyState check
      if (this.token && this.ws?.readyState === WebSocket.OPEN) {
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

      if (parsed.type === 'connected') {
        this._setStatus('connected');
        // ← ADDED: flush pending subscribe
        if (this.pendingSubscribe) {
          this.send({ type: 'subscribe', rack_id: this.pendingSubscribe });
          this.pendingSubscribe = null;
        }
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

const wsClient = new WsClient();
export default wsClient;