/**
 * src/components/ConnectionBar.tsx
 *
 * Section 7 — Persistent status bar showing WebSocket, MQTT, and Pi status.
 * Rendered inside AppLayout just below the top header.
 * Displays live colour-coded dots for each signal; never shows stale data.
 */

import React from 'react';
import { Wifi, WifiOff, Radio, Camera, AlertTriangle } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useSystem } from '../context/SystemContext';
import type { WsStatus } from '../types/gantry.types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function wsDotClass(status: WsStatus): string {
  switch (status) {
    case 'connected':    return 'bg-green-500';
    case 'connecting':   return 'bg-amber-400 animate-pulse';
    case 'reconnecting': return 'bg-amber-500 animate-pulse';
    case 'disconnected': return 'bg-red-500';
  }
}

function wsLabel(status: WsStatus): string {
  switch (status) {
    case 'connected':    return 'WS: Connected';
    case 'connecting':   return 'WS: Connecting…';
    case 'reconnecting': return 'WS: Reconnecting…';
    case 'disconnected': return 'WS: Offline';
  }
}

interface DotProps {
  color: 'green' | 'amber' | 'red' | 'gray';
  pulse?: boolean;
}

function StatusDot({ color, pulse }: DotProps) {
  const base = 'w-2 h-2 rounded-full shrink-0';
  const colorMap = {
    green: 'bg-green-500',
    amber: 'bg-amber-400',
    red:   'bg-red-500',
    gray:  'bg-slate-400',
  };
  return (
    <span className={cn(base, colorMap[color], pulse && 'animate-pulse')} />
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ConnectionBar() {
  const { wsStatus, mqttConnected, piOnline, alerts, activeRackId } = useSystem();

  const errorCount = alerts.filter(a => a.level === 'error').length;

  return (
    <div
      id="connection-bar"
      className="flex items-center gap-4 px-4 xl:px-6 py-1.5 bg-[#1a2440] border-b border-white/10 text-[10px] font-mono uppercase text-white/60 overflow-x-auto no-scrollbar"
    >
      {/* WebSocket status */}
      <div className="flex items-center gap-1.5 shrink-0">
        {wsStatus === 'connected'
          ? <Wifi className="w-3 h-3 text-green-400" />
          : <WifiOff className="w-3 h-3 text-red-400" />
        }
        <span
          className={cn(
            wsStatus === 'connected'  ? 'text-green-400' :
            wsStatus === 'disconnected' ? 'text-red-400'  : 'text-amber-400'
          )}
        >
          {wsLabel(wsStatus)}
        </span>
      </div>

      <div className="w-px h-3 bg-white/20 shrink-0" />

      {/* MQTT broker status */}
      <div className="flex items-center gap-1.5 shrink-0">
        <Radio className="w-3 h-3" />
        <StatusDot color={mqttConnected ? 'green' : 'red'} pulse={!mqttConnected} />
        <span className={mqttConnected ? 'text-green-400' : 'text-red-400'}>
          MQTT: {mqttConnected ? 'OK' : 'Offline'}
        </span>
      </div>

      <div className="w-px h-3 bg-white/20 shrink-0" />

      {/* Pi status */}
      <div className="flex items-center gap-1.5 shrink-0">
        <Camera className="w-3 h-3" />
        <StatusDot
          color={piOnline ? 'green' : activeRackId ? 'red' : 'gray'}
          pulse={activeRackId !== null && !piOnline}
        />
        <span
          className={
            !activeRackId ? 'text-white/30' :
            piOnline      ? 'text-green-400' : 'text-red-400'
          }
        >
          Pi: {!activeRackId ? 'No rack' : piOnline ? 'Online' : 'Offline'}
        </span>
      </div>

      {/* Active rack badge */}
      {activeRackId && (
        <>
          <div className="w-px h-3 bg-white/20 shrink-0" />
          <div className="flex items-center gap-1.5 shrink-0 text-blue-300">
            <span>Rack: {activeRackId}</span>
          </div>
        </>
      )}

      {/* Alert badge */}
      {errorCount > 0 && (
        <>
          <div className="w-px h-3 bg-white/20 shrink-0" />
          <div className="flex items-center gap-1.5 shrink-0 text-red-400">
            <AlertTriangle className="w-3 h-3" />
            <span>{errorCount} Alert{errorCount > 1 ? 's' : ''}</span>
          </div>
        </>
      )}

      {/* Spacer */}
      <div className="flex-1" />

      {/* Timestamp */}
      <CurrentTime />
    </div>
  );
}

function CurrentTime() {
  const [time, setTime] = React.useState(() => new Date().toLocaleTimeString());
  React.useEffect(() => {
    const id = setInterval(() => setTime(new Date().toLocaleTimeString()), 1000);
    return () => clearInterval(id);
  }, []);
  return <span className="text-white/30 shrink-0">{time}</span>;
}
