/**
 * src/context/SystemContext.tsx
 *
 * Section 7 — extended SystemContext.
 *
 * Additions over the original stub:
 *   wsStatus          — WS connection state (connecting/connected/reconnecting/disconnected)
 *   gantryPosition    — live X/Y/C + homed flags from M114 responses
 *   piOnline          — true while Pi heartbeat is within the last ~60s
 *   mqttConnected     — last value from /health or connected message
 *   gridCells         — per-cell capture state for GantryGrid
 *   streamUrl         — active go2rtc WebRTC URL (non-null while lock held)
 *   userRole          — role of the authenticated user
 *   activeRackId      — rack the operator is currently working on
 *   auth              — JWT token + userId + role (null when logged out)
 *   alerts            — escalation-ladder alerts to show in the UI
 *   sendCommand()     — sends a command message over the WS with CSRF
 *   login()           — POST /auth/login, store JWT, open WS
 *   logout()          — clear auth, close WS
 *   subscribeRack()   — send a subscribe message for a rack
 *
 * The original SystemNode / activeSystem / setActiveSystem are kept so that
 * FleetManager and AppLayout continue to work without modification.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react';

import appConfig from '../config/app.config';
import wsClient from '../lib/wsClient';
import type {
  AuthState,
  GantryPosition,
  GridCell,
  RackLayout,
  ScanState,
  UserRole,
  WsMessage,
  WsStatus,
  WsMsgAlert,
} from '../types/gantry.types';

// ---------------------------------------------------------------------------
// Legacy SystemNode — kept for FleetManager / AppLayout compatibility
// ---------------------------------------------------------------------------

export type SystemNode = {
  id: string;
  name: string;
  status: string;
  ip: string;
  parentPath: string;
  activeJob?: string;
};

// ---------------------------------------------------------------------------
// Alert entry for the UI
// ---------------------------------------------------------------------------
export interface AlertEntry {
  id: string;
  rackId?: string;
  level: 'info' | 'warning' | 'error';
  code: string;
  message: string;
  ts: Date;
}

// ---------------------------------------------------------------------------
// Context shape
// ---------------------------------------------------------------------------
interface SystemContextType {
  // ── Legacy ──────────────────────────────────────────────────────────────
  activeSystem: SystemNode | null;
  setActiveSystem: (system: SystemNode | null) => void;

  // ── Auth ────────────────────────────────────────────────────────────────
  auth: AuthState | null;
  userRole: UserRole | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;

  // ── WebSocket / connectivity ─────────────────────────────────────────────
  wsStatus: WsStatus;
  mqttConnected: boolean;
  piOnline: boolean;

  // ── Rack context ─────────────────────────────────────────────────
  activeRackId: string | null;
  subscribeRack: (rackId: string) => Promise<void>;

  // ── Gantry state ─────────────────────────────────────────────────
  gantryPosition: GantryPosition;
  scanState: ScanState;
  gridCells: GridCell[];
  rackLayout: RackLayout | null;

  // ── Camera stream ─────────────────────────────────────────────────────────
  streamUrl: string | null;

  // ── Alerts ───────────────────────────────────────────────────────────────
  alerts: AlertEntry[];
  dismissAlert: (id: string) => void;

  // ── Command API ──────────────────────────────────────────────────────────
  /**
   * Send any whitelisted gantry command over the WebSocket.
   * CSRF token is injected automatically by wsClient.
   * Viewer-role connections are blocked server-side — no client guard needed.
   */
  sendCommand: (command: string, rackId?: string) => void;
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------
const defaultPosition: GantryPosition = {
  x: null, y: null, c: null,
  homed_x: false, homed_y: false, homed_c: false,
};

const SystemContext = createContext<SystemContextType | undefined>(undefined);

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------
export const SystemProvider = ({ children }: { children: React.ReactNode }) => {
  // Legacy
  const [activeSystem, setActiveSystem] = useState<SystemNode | null>(null);

  // Auth
  const [auth, setAuth] = useState<AuthState | null>(() => {
    try {
      const raw = sessionStorage.getItem('vivarium_auth');
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  });

  // Connectivity
  const [wsStatus, setWsStatus]         = useState<WsStatus>('disconnected');
  const [mqttConnected, setMqttConnected] = useState(false);
  const [piOnline, setPiOnline]         = useState(false);

  // Rack
  const [activeRackId, setActiveRackId] = useState<string | null>(null);

  // Gantry state
  const [gantryPosition, setGantryPosition] = useState<GantryPosition>(defaultPosition);
  const [scanState, setScanState]           = useState<ScanState>('idle');
  const [gridCells, setGridCells]           = useState<GridCell[]>([]);

  // Camera
  const [streamUrl, setStreamUrl] = useState<string | null>(null);

  // Alerts
  const [alerts, setAlerts] = useState<AlertEntry[]>([]);

  // Rack layout (live from /rack/{id}/layout, refreshed on subscribeRack)
  const [rackLayout, setRackLayout] = useState<RackLayout | null>(null);

  const piHeartbeatTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── Grid initialiser ────────────────────────────────────────────────────
  useEffect(() => {
    const cells: GridCell[] = [];
    for (let r = 0; r < appConfig.rackRows; r++) {
      for (let c = 0; c < appConfig.rackCols; c++) {
        cells.push({ row: r, col: c });
      }
    }
    setGridCells(cells);
  }, []);

  // ── Pi heartbeat watchdog — mark offline if >65s since last status ──────
  const resetPiHeartbeat = useCallback(() => {
    setPiOnline(true);
    if (piHeartbeatTimer.current) clearTimeout(piHeartbeatTimer.current);
    piHeartbeatTimer.current = setTimeout(() => setPiOnline(false), 65_000);
  }, []);

  // ── /health poll — runs every 30s to keep mqttConnected fresh ───────────
  useEffect(() => {
    const poll = async () => {
      try {
        const res = await fetch(`${appConfig.apiBaseUrl}/health`);
        if (res.ok) {
          const data = await res.json();
          setMqttConnected(data.mqtt_connected ?? false);
        }
      } catch { /* server offline */ }
    };
    poll();
    const id = setInterval(poll, 30_000);
    return () => clearInterval(id);
  }, []);

  // ── WS message handler ──────────────────────────────────────────────────
  const handleWsMessage = useCallback((msg: WsMessage) => {
    switch (msg.type) {
      case 'connected':
        // role & user_id confirmed by server
        break;

      case 'status': {
        const d = msg.data;
        if (d.status === 'online' || d.ts !== undefined) {
          resetPiHeartbeat();
        }
        if (d.status === 'offline') {
          setPiOnline(false);
        }
        if (d.x !== undefined || d.y !== undefined || d.c !== undefined) {
          setGantryPosition(prev => ({
            x: d.x ?? prev.x,
            y: d.y ?? prev.y,
            c: d.c ?? prev.c,
            homed_x: d.homed_x ?? prev.homed_x,
            homed_y: d.homed_y ?? prev.homed_y,
            homed_c: d.homed_c ?? prev.homed_c,
          }));
        }
        if (d.scan_state) setScanState(d.scan_state);
        break;
      }

      case 'stream_url':
        setStreamUrl(msg.data.url);
        break;

      case 'lock_released':
        setStreamUrl(null);
        break;

      case 'scan_status':
        setScanState(msg.data.status);
        break;

      case 'scan_cell_complete': {
        const { cell_row, cell_col } = msg.data;
        setGridCells(prev =>
          prev.map(cell =>
            cell.row === cell_row && cell.col === cell_col
              ? { ...cell, captured: true }
              : cell,
          ),
        );
        break;
      }

      case 'capture_complete': {
        const { cell_row, cell_col } = msg.data;
        if (cell_row !== undefined && cell_col !== undefined) {
          setGridCells(prev =>
            prev.map(cell =>
              cell.row === cell_row && cell.col === cell_col
                ? { ...cell, captured: true }
                : cell,
            ),
          );
        }
        break;
      }

      case 'alert': {
        const alertMsg = msg as unknown as WsMsgAlert;
        const entry: AlertEntry = {
          id: crypto.randomUUID(),
          rackId: alertMsg.rack_id,
          level: alertMsg.data.level,
          code: alertMsg.data.code,
          message: alertMsg.data.message,
          ts: new Date(),
        };
        setAlerts(prev => [entry, ...prev].slice(0, 50));
        break;
      }

      case 'response': {
        // The server relays all MQTT "response" subtopic messages to WS.
        // M114 lines arrive here: "M114 X:12.50 Y:24.00 C:0.00 homed:X=Y Y=Y C=N"
        // Other messages (COMMAND_ACK:M700, BRIDGE_RECONNECTED, etc.) are skipped.
        const raw = typeof msg.data === 'string' ? msg.data : JSON.stringify(msg.data ?? '');
        const m114Match = raw.match(
          /X:([-\d.]+)\s+Y:([-\d.]+)\s+C:([-\d.]+).*homed:X=([YN])\s*Y=([YN])\s*C=([YN])/i
        );
        if (m114Match) {
          setGantryPosition({
            x: parseFloat(m114Match[1]),
            y: parseFloat(m114Match[2]),
            c: parseFloat(m114Match[3]),
            homed_x: m114Match[4].toUpperCase() === 'Y',
            homed_y: m114Match[5].toUpperCase() === 'Y',
            homed_c: m114Match[6].toUpperCase() === 'Y',
          });
        }
        break;
      }

      default:
        break;
    }
  }, [resetPiHeartbeat]);

  // ── Wire wsClient callbacks once ─────────────────────────────────────────
  useEffect(() => {
    wsClient.setOnStatusChange(setWsStatus);
    wsClient.setOnMessage(handleWsMessage);
  }, [handleWsMessage]);

  // ── Auto-connect when auth is available ──────────────────────────────────
  useEffect(() => {
    if (auth?.token) {
      wsClient.init(auth.token);
    } else {
      wsClient.close();
    }
    return () => {/* keep alive across re-renders */};
  }, [auth?.token]);

  // ── Auth ─────────────────────────────────────────────────────────────────
  const login = useCallback(async (username: string, password: string) => {
    const res = await fetch(`${appConfig.apiBaseUrl}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail ?? 'Login failed');
    }
    const data = await res.json();
    const authState: AuthState = {
      token: data.access_token,
      userId: data.user_id,
      role: data.role,
    };
    sessionStorage.setItem('vivarium_auth', JSON.stringify(authState));
    setAuth(authState);
  }, []);

  const logout = useCallback(() => {
    sessionStorage.removeItem('vivarium_auth');
    setAuth(null);
    setWsStatus('disconnected');
    setPiOnline(false);
    setStreamUrl(null);
  }, []);

  // ── Rack subscription ──────────────────────────────────────────────────
  const subscribeRack = useCallback(async (rackId: string) => {
    setActiveRackId(rackId);
    if (wsClient.isOpen) wsClient.subscribe(rackId);

    // Fetch live rack layout from the server (DB-stored, refreshed by LAYOUT_CONFIG)
    if (!auth?.token) return;
    try {
      const res = await fetch(`${appConfig.apiBaseUrl}/rack/${rackId}/layout`, {
        headers: { Authorization: `Bearer ${auth.token}` },
      });
      if (res.ok) {
        const layout: RackLayout = await res.json();
        setRackLayout(layout);
        // Rebuild grid cells with correct dimensions from the server
        const cells: GridCell[] = [];
        for (let r = 0; r < layout.rows; r++) {
          for (let c = 0; c < layout.columns; c++) {
            cells.push({ row: r, col: c });
          }
        }
        setGridCells(cells);
      }
    } catch {
      // Non-fatal: keep default grid from appConfig.rackRows/rackCols
    }
  }, [auth?.token]);

  // ── sendCommand ───────────────────────────────────────────────────────────
  const sendCommand = useCallback((command: string, rackId?: string) => {
    const target = rackId ?? activeRackId;
    if (!target) {
      console.warn('[SystemContext] sendCommand — no rack selected');
      return;
    }
    wsClient.send({ type: 'command', rack_id: target, command });
  }, [activeRackId]);

  // ── Alerts ────────────────────────────────────────────────────────────────
  const dismissAlert = useCallback((id: string) => {
    setAlerts(prev => prev.filter(a => a.id !== id));
  }, []);

  // ── Context value ─────────────────────────────────────────────────────────
  const value: SystemContextType = {
    // Legacy
    activeSystem,
    setActiveSystem,
    // Auth
    auth,
    userRole: auth?.role ?? null,
    login,
    logout,
    // Connectivity
    wsStatus,
    mqttConnected,
    piOnline,
    // Rack
    activeRackId,
    subscribeRack,
    // Gantry
    gantryPosition,
    scanState,
    gridCells,
    rackLayout,
    // Camera
    streamUrl,
    // Alerts
    alerts,
    dismissAlert,
    // Commands
    sendCommand,
  };

  return (
    <SystemContext.Provider value={value}>
      {children}
    </SystemContext.Provider>
  );
};

export const useSystem = () => {
  const context = useContext(SystemContext);
  if (context === undefined) {
    throw new Error('useSystem must be used within a SystemProvider');
  }
  return context;
};
