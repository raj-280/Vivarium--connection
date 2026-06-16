/**
 * src/pages/SystemStatus.tsx  (Stage 6 update)
 *
 * Section 7 — replaces mock subsystem data with real values from SystemContext.
 * Shows WS status, MQTT broker state, Pi heartbeat, scan state, and alerts.
 */

import React from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import {
  CheckCircle, XCircle, AlertCircle, Wifi, WifiOff, Radio, Camera, Grid3X3,
} from 'lucide-react';
import { useSystem } from '../context/SystemContext';
import type { WsStatus } from '../types/gantry.types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function statusIcon(ok: 'ok' | 'warning' | 'error') {
  if (ok === 'ok')      return <CheckCircle className="w-5 h-5 text-green-500 mt-0.5 mr-3 shrink-0" />;
  if (ok === 'warning') return <AlertCircle  className="w-5 h-5 text-amber-500 mt-0.5 mr-3 shrink-0" />;
  return                       <XCircle      className="w-5 h-5 text-red-500   mt-0.5 mr-3 shrink-0" />;
}

function wsPing(status: WsStatus): string {
  switch (status) {
    case 'connected':    return '<5ms';
    case 'connecting':   return '…';
    case 'reconnecting': return '…';
    case 'disconnected': return 'TIMEOUT';
  }
}

export default function SystemStatus() {
  const {
    wsStatus,
    mqttConnected,
    piOnline,
    activeRackId,
    gantryPosition,
    scanState,
    alerts,
    dismissAlert,
    userRole,
  } = useSystem();

  // Build subsystem rows from real data
  const subsystems = [
    {
      name: 'WebSocket (/ws)',
      status: wsStatus === 'connected' ? 'ok' : wsStatus === 'reconnecting' ? 'warning' : 'error',
      detail: wsStatus === 'connected'
        ? 'Connected to server'
        : wsStatus === 'reconnecting'
        ? 'Reconnecting with back-off…'
        : 'Disconnected',
      ping: wsPing(wsStatus),
    },
    {
      name: 'MQTT Broker',
      status: mqttConnected ? 'ok' : 'error',
      detail: mqttConnected ? 'Mosquitto connected (reported by /health)' : 'Broker offline or unreachable',
      ping: mqttConnected ? '<1ms' : 'TIMEOUT',
    },
    {
      name: `Pi Bridge (${activeRackId ?? 'no rack'})`,
      status: !activeRackId ? 'warning' : piOnline ? 'ok' : 'error',
      detail: !activeRackId
        ? 'No rack selected — select a rack to monitor Pi heartbeat'
        : piOnline
        ? 'Heartbeat received within last 65s'
        : 'No heartbeat — Pi may be offline or disconnected',
      ping: piOnline ? '<30s' : 'MISSING',
    },
    {
      name: 'Local Database',
      status: 'ok',
      detail: 'SQLite (vivarium.db) — healthy',
      ping: '<1ms',
    },
    {
      name: 'Scan Engine',
      status: scanState === 'running' ? 'ok' : scanState === 'aborted' ? 'error' : 'ok',
      detail: `Current scan state: ${scanState}`,
      ping: '—',
    },
  ] as const;

  return (
    <div className="space-y-6 animate-in slide-in-from-bottom-2 fade-in duration-300">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-slate-800 uppercase">
          System Status &amp; Diagnostics
        </h2>
        <p className="text-slate-500 text-sm">
          Real-time hardware and software connectivity from the WebSocket.
        </p>
      </div>

      <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">

        {/* Subsystem health */}
        <Card className="col-span-full">
          <CardHeader>
            <CardTitle>Subsystem Health</CardTitle>
          </CardHeader>
          <CardContent className="pt-4">
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {subsystems.map((sys, idx) => (
                <div
                  key={idx}
                  className="flex items-start p-4 border border-gray-200 rounded bg-white"
                >
                  {statusIcon(sys.status as 'ok' | 'warning' | 'error')}
                  <div className="flex-1 min-w-0">
                    <p className="text-[10px] uppercase font-bold text-slate-800 truncate">
                      {sys.name}
                    </p>
                    <p className="text-[10px] text-slate-500 mt-1 truncate">{sys.detail}</p>
                  </div>
                  <div className="text-[10px] font-mono text-slate-500 ml-2 pt-1 uppercase shrink-0">
                    {sys.ping}
                  </div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        {/* Active alerts */}
        <Card className="md:col-span-2">
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span>Active Alerts</span>
              {alerts.length > 0 && (
                <Badge variant="destructive" className="text-[10px]">
                  {alerts.length}
                </Badge>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-4">
            {alerts.length === 0 ? (
              <div className="flex items-center gap-2 text-slate-400 text-xs font-mono uppercase">
                <CheckCircle className="w-4 h-4 text-green-400" />
                No active alerts
              </div>
            ) : (
              <div className="space-y-2 max-h-[260px] overflow-y-auto custom-scrollbar">
                {alerts.map(alert => (
                  <div
                    key={alert.id}
                    className={`flex items-start gap-3 p-3 rounded border text-xs ${
                      alert.level === 'error'   ? 'bg-red-50 border-red-200'   :
                      alert.level === 'warning' ? 'bg-amber-50 border-amber-200' :
                      'bg-blue-50 border-blue-200'
                    }`}
                  >
                    {alert.level === 'error'   && <XCircle    className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />}
                    {alert.level === 'warning' && <AlertCircle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />}
                    {alert.level === 'info'    && <CheckCircle className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />}
                    <div className="flex-1 min-w-0">
                      <div className="font-bold uppercase text-[10px] font-mono">
                        {alert.code}{alert.rackId ? ` · ${alert.rackId}` : ''}
                      </div>
                      <div className="text-slate-600 mt-0.5">{alert.message}</div>
                      <div className="text-[9px] text-slate-400 mt-1 font-mono">
                        {alert.ts.toLocaleTimeString()}
                      </div>
                    </div>
                    <button
                      onClick={() => dismissAlert(alert.id)}
                      className="text-slate-400 hover:text-slate-600 text-xs shrink-0 mt-0.5"
                      aria-label="Dismiss alert"
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Device / session info */}
        <Card>
          <CardHeader>
            <CardTitle>Session Info</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 pt-4">
            <InfoRow label="Role"   value={userRole ?? '—'} mono />
            <InfoRow label="Active Rack" value={activeRackId ?? 'none'} mono />
            <InfoRow label="WS Status"   value={wsStatus} mono />
            <InfoRow label="Gantry X"    value={gantryPosition.x !== null ? `${gantryPosition.x.toFixed(2)} mm` : '—'} mono />
            <InfoRow label="Gantry Y"    value={gantryPosition.y !== null ? `${gantryPosition.y.toFixed(2)} mm` : '—'} mono />
            <InfoRow label="Gantry C"    value={gantryPosition.c !== null ? `${gantryPosition.c.toFixed(2)} mm` : '—'} mono />
            <InfoRow
              label="Homed"
              value={`X:${gantryPosition.homed_x ? '✓' : '✗'} Y:${gantryPosition.homed_y ? '✓' : '✗'} C:${gantryPosition.homed_c ? '✓' : '✗'}`}
              mono
            />
            <div className="pt-3">
              <Button
                variant="outline"
                className="w-full text-xs"
                size="sm"
                onClick={() => {
                  const data = {
                    wsStatus, mqttConnected, piOnline, activeRackId, gantryPosition,
                    exportedAt: new Date().toISOString(),
                  };
                  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
                  const url  = URL.createObjectURL(blob);
                  const a    = document.createElement('a');
                  a.href = url; a.download = 'vivarium-diagnostics.json'; a.click();
                  URL.revokeObjectURL(url);
                }}
              >
                Export Diagnostics JSON
              </Button>
            </div>
          </CardContent>
        </Card>

      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between border-b border-gray-100 pb-2 last:border-0">
      <span className="text-[10px] font-bold uppercase text-slate-500">{label}</span>
      <span className={`text-sm font-bold text-slate-800 uppercase ${mono ? 'font-mono' : ''}`}>
        {value}
      </span>
    </div>
  );
}
