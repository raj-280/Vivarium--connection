/**
 * src/pages/Settings.tsx  (Stage 6 update)
 *
 * Section 7 — adds WS/rack configuration display (read-only, shows VITE_* values)
 * and retains the existing Security + Danger Zone sections.
 */

import React from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Label } from '../components/ui/Label';
import { Input } from '../components/ui/Input';
import { Button } from '../components/ui/Button';
import { Badge } from '../components/ui/Badge';
import { useSystem } from '../context/SystemContext';
import appConfig from '../config/app.config';

export default function Settings() {
  const { wsStatus, mqttConnected, piOnline, activeRackId, userRole, logout } = useSystem();

  return (
    <div className="space-y-6 animate-in slide-in-from-bottom-2 fade-in duration-300">
      <div>
        <h2 className="text-2xl font-bold tracking-tight text-slate-800 uppercase">
          Settings &amp; Security
        </h2>
        <p className="text-slate-500 text-sm">Device configuration and local administration.</p>
      </div>

      <div className="grid gap-6 md:grid-cols-2">

        {/* ── WebSocket / Server Connection ───────────────────────────── */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span>Server Connection</span>
              <Badge
                variant={wsStatus === 'connected' ? 'success' : 'warning'}
                className="text-[10px]"
              >
                {wsStatus.toUpperCase()}
              </Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 pt-4">
            <div className="space-y-1">
              <Label>WebSocket URL</Label>
              <Input
                id="settings-ws-url"
                readOnly
                value={appConfig.wsUrl}
                className="bg-gray-50 text-slate-600 font-mono text-xs"
              />
              <p className="text-[10px] text-slate-400 uppercase font-mono">
                Set via VITE_WS_URL in .env
              </p>
            </div>
            <div className="space-y-1">
              <Label>API Base URL</Label>
              <Input
                id="settings-api-url"
                readOnly
                value={appConfig.apiBaseUrl}
                className="bg-gray-50 text-slate-600 font-mono text-xs"
              />
              <p className="text-[10px] text-slate-400 uppercase font-mono">
                Set via VITE_API_BASE_URL in .env
              </p>
            </div>
            <div className="space-y-1">
              <Label>Stream Base Path</Label>
              <Input
                id="settings-stream-path"
                readOnly
                value={appConfig.streamBasePath}
                className="bg-gray-50 text-slate-600 font-mono text-xs"
              />
            </div>
            <div className="grid grid-cols-2 gap-3 pt-2 border-t border-gray-100">
              <StatusItem label="MQTT Broker" ok={mqttConnected} />
              <StatusItem label={`Pi (${activeRackId ?? 'none'})`} ok={piOnline} />
            </div>
          </CardContent>
        </Card>

        {/* ── Rack Geometry ───────────────────────────────────────────── */}
        <Card>
          <CardHeader>
            <CardTitle>Rack Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 pt-4">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label>Grid Rows</Label>
                <Input
                  id="settings-rack-rows"
                  readOnly
                  value={appConfig.rackRows}
                  className="bg-gray-50 font-mono text-xs"
                />
                <p className="text-[10px] text-slate-400 uppercase font-mono">VITE_RACK_ROWS</p>
              </div>
              <div className="space-y-1">
                <Label>Grid Cols</Label>
                <Input
                  id="settings-rack-cols"
                  readOnly
                  value={appConfig.rackCols}
                  className="bg-gray-50 font-mono text-xs"
                />
                <p className="text-[10px] text-slate-400 uppercase font-mono">VITE_RACK_COLS</p>
              </div>
            </div>
            <div className="space-y-1">
              <Label>Jog Steps (mm)</Label>
              <div className="flex gap-2 flex-wrap">
                {appConfig.jogStepsMm.map(s => (
                  <span
                    key={s}
                    className="px-2 py-1 bg-gray-100 rounded text-xs font-mono text-slate-700"
                  >
                    {s}
                  </span>
                ))}
              </div>
            </div>
            <div className="space-y-1 pt-2 border-t border-gray-100">
              <Label>Active Rack</Label>
              <Input
                readOnly
                value={activeRackId ?? 'None selected'}
                className="bg-gray-50 font-mono text-xs"
              />
            </div>
          </CardContent>
        </Card>

        {/* ── Security &amp; Access ───────────────────────────────────── */}
        <Card>
          <CardHeader>
            <CardTitle>Security &amp; Access</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 pt-4">
            <div className="space-y-1">
              <Label>Current Role</Label>
              <div className="flex items-center gap-2">
                <Badge
                  variant={
                    userRole === 'admin' ? 'destructive' :
                    userRole === 'operator' ? 'success' : 'secondary'
                  }
                  className="uppercase text-xs"
                >
                  {userRole ?? 'not logged in'}
                </Badge>
              </div>
            </div>

            <div className="pt-3 border-t border-gray-200">
              <h4 className="text-[10px] font-bold text-slate-500 mb-3 uppercase">
                Role Permissions
              </h4>
              <div className="space-y-2 text-xs text-slate-600">
                <p className="flex items-center gap-2">
                  <span className={userRole !== 'viewer' ? 'text-green-500' : 'text-slate-300'}>●</span>
                  Send motion &amp; capture commands (Operator / Admin)
                </p>
                <p className="flex items-center gap-2">
                  <span className={userRole === 'admin' ? 'text-green-500' : 'text-slate-300'}>●</span>
                  Admin panel &amp; device management (Admin only)
                </p>
                <p className="flex items-center gap-2">
                  <span className="text-green-500">●</span>
                  View live feed &amp; status (All roles)
                </p>
              </div>
            </div>

            <div className="pt-3 border-t border-gray-200">
              <h4 className="text-[10px] font-bold text-slate-500 mb-2 uppercase">
                CSRF Protection
              </h4>
              <p className="text-[10px] text-slate-500 font-mono">
                Cookie: <code>{appConfig.csrf.cookieName}</code><br />
                Header: <code>{appConfig.csrf.headerName}</code><br />
                Injected automatically on all CAPTURE/command messages.
              </p>
            </div>
          </CardContent>
        </Card>

        {/* ── Danger Zone ────────────────────────────────────────────── */}
        <Card className="border-red-500/30">
          <CardHeader>
            <CardTitle className="text-red-500">Danger Zone</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 pt-4">
            <p className="text-sm text-slate-500">
              These actions are irreversible or require admin credentials.
            </p>
            <div className="flex flex-col sm:flex-row gap-3">
              <Button
                id="settings-logout-button"
                variant="destructive"
                size="sm"
                className="w-full sm:w-auto"
                onClick={logout}
              >
                Sign Out
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="w-full sm:w-auto text-red-400 border-red-500/30 hover:bg-red-500/10"
                disabled={userRole !== 'admin'}
              >
                Factory Reset Local Database
              </Button>
            </div>
          </CardContent>
        </Card>

      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Status item sub-component
// ---------------------------------------------------------------------------
function StatusItem({ label, ok }: { label: string; ok: boolean }) {
  return (
    <div className="flex items-center gap-2 text-xs text-slate-600">
      <span className={`w-2 h-2 rounded-full ${ok ? 'bg-green-500' : 'bg-red-400 animate-pulse'}`} />
      <span className="font-mono uppercase text-[10px]">{label}: {ok ? 'OK' : 'Offline'}</span>
    </div>
  );
}
