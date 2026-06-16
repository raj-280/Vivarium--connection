/**
 * src/pages/LiveControl.tsx  (Stage 6 rewrite)
 *
 * Section 7 — wired to real WebSocket state via SystemContext.
 *
 * Changes from the mock version:
 *   - Jog buttons send real M701/M702/M703/M704 commands
 *   - Step size selector stores local state
 *   - Home button sends G28
 *   - "Move to Compartment" sends M700 Rn Cn
 *   - CameraPanel replaces the static video placeholder (WebRTC / go2rtc)
 *   - GantryGrid replaces the static Recent Captures strip
 *   - EmergencyStop rendered in the controls column — always visible
 *   - Position overlay reads from gantryPosition (not hardcoded strings)
 *   - Keyboard shortcut: Spacebar → Capture (Section 9 Layer 1)
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowUp, ArrowDown, ArrowLeft, ArrowRight,
  Crosshair, Settings2,
} from 'lucide-react';

import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { Badge } from '../components/ui/Badge';
import { Input } from '../components/ui/Input';
import { Label } from '../components/ui/Label';
import { CameraPanel } from '../components/CameraPanel';
import { GantryGrid } from '../components/GantryGrid';
import { EmergencyStop } from '../components/EmergencyStop';
import { useSystem } from '../context/SystemContext';
import appConfig from '../config/app.config';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Jog direction → M-command mapping (Arduino command set)
// M701 = Y+, M702 = Y-, M703 = X-, M704 = X+
// ---------------------------------------------------------------------------
function jogCommand(dir: 'up' | 'down' | 'left' | 'right', stepMm: number): string {
  switch (dir) {
    case 'up':    return `M701 F${stepMm}`;
    case 'down':  return `M702 F${stepMm}`;
    case 'left':  return `M703 F${stepMm}`;
    case 'right': return `M704 F${stepMm}`;
  }
}

export default function LiveControl() {
  const {
    sendCommand,
    activeRackId,
    gantryPosition,
    piOnline,
    wsStatus,
    userRole,
    scanState,
  } = useSystem();

  const [stepIndex, setStepIndex] = useState(appConfig.defaultJogStepIndex);
  const [targetRow, setTargetRow] = useState(0);
  const [targetCol, setTargetCol] = useState(0);
  const stepMm = appConfig.jogStepsMm[stepIndex];

  const isConnected = wsStatus === 'connected';
  const canCommand  = isConnected && userRole !== 'viewer' && !!activeRackId;

  // ── Spacebar → Capture shortcut (Section 9 Layer 1) ───────────────────
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (
        e.code === 'Space' &&
        !(e.target instanceof HTMLInputElement) &&
        !(e.target instanceof HTMLTextAreaElement)
      ) {
        e.preventDefault();
        if (canCommand) sendCommand('CAPTURE', activeRackId ?? undefined);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [canCommand, sendCommand, activeRackId]);

  const jog = useCallback((dir: 'up' | 'down' | 'left' | 'right') => {
    if (!canCommand) return;
    sendCommand(jogCommand(dir, stepMm), activeRackId ?? undefined);
  }, [canCommand, sendCommand, activeRackId, stepMm]);

  const home = useCallback(() => {
    if (!canCommand) return;
    sendCommand('G28', activeRackId ?? undefined);
  }, [canCommand, sendCommand, activeRackId]);

  const moveToCompartment = useCallback(() => {
    if (!canCommand) return;
    sendCommand(`M700 R${targetRow} C${targetCol}`, activeRackId ?? undefined);
  }, [canCommand, sendCommand, activeRackId, targetRow, targetCol]);

  // Status badge
  const statusText = !isConnected
    ? 'Disconnected'
    : !activeRackId
    ? 'No Rack'
    : !piOnline
    ? 'Pi Offline'
    : scanState === 'running'
    ? 'Scanning…'
    : 'Ready';

  const statusVariant: 'success' | 'warning' | 'destructive' =
    statusText === 'Ready' || statusText === 'Scanning…' ? 'success' :
    statusText === 'Pi Offline' ? 'destructive' : 'warning';

  return (
    <div className="space-y-4 animate-in slide-in-from-bottom-2 fade-in duration-300">
      {/* Page header */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-800 uppercase">
            Live Camera Control
          </h2>
          <p className="text-slate-500 text-sm">
            Manual gantry operation, live video feed, and on-demand capture.
          </p>
        </div>
        <div className="flex items-center gap-2 w-full sm:w-auto">
          <Badge
            variant={statusVariant}
            className={cn(
              'w-full sm:w-auto text-center justify-center',
              statusText === 'Ready' && 'animate-pulse',
            )}
          >
            {statusText}
          </Badge>
          {activeRackId && (
            <span className="text-[10px] font-mono text-slate-400 uppercase">
              {activeRackId}
            </span>
          )}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-12">

        {/* ── Left column: camera + grid ──────────────────────────────── */}
        <div className="lg:col-span-8 flex flex-col space-y-4">

          {/* Camera panel — replaces mock video placeholder */}
          <CameraPanel className="flex-1 min-h-[400px]" />

          {/* Gantry grid */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">
                Rack Grid — click cell to move ({appConfig.rackRows}×{appConfig.rackCols})
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-2 overflow-x-auto">
              <GantryGrid />
            </CardContent>
          </Card>
        </div>

        {/* ── Right column: controls ──────────────────────────────────── */}
        <div className="lg:col-span-4 space-y-4">

          {/* Emergency stop — always at the top, always visible */}
          <EmergencyStop />

          {/* Manual jogging */}
          <Card>
            <CardHeader>
              <CardTitle>Manual Jogging</CardTitle>
            </CardHeader>
            <CardContent className="space-y-5 pt-4">
              {/* D-pad */}
              <div className="flex items-center justify-center">
                <div className="grid grid-cols-3 gap-2">
                  <div />
                  <Button
                    id="jog-up"
                    variant="outline"
                    className="h-12 w-12"
                    title="Y+ (up)"
                    disabled={!canCommand}
                    onClick={() => jog('up')}
                  >
                    <ArrowUp className="w-5 h-5 text-slate-600" />
                  </Button>
                  <div />
                  <Button
                    id="jog-left"
                    variant="outline"
                    className="h-12 w-12"
                    title="X- (left)"
                    disabled={!canCommand}
                    onClick={() => jog('left')}
                  >
                    <ArrowLeft className="w-5 h-5 text-slate-600" />
                  </Button>
                  <Button
                    id="jog-home"
                    variant="outline"
                    className="h-12 w-12"
                    title="Home (G28)"
                    disabled={!canCommand}
                    onClick={home}
                  >
                    <Crosshair className="w-4 h-4 text-slate-500" />
                  </Button>
                  <Button
                    id="jog-right"
                    variant="outline"
                    className="h-12 w-12"
                    title="X+ (right)"
                    disabled={!canCommand}
                    onClick={() => jog('right')}
                  >
                    <ArrowRight className="w-5 h-5 text-slate-600" />
                  </Button>
                  <div />
                  <Button
                    id="jog-down"
                    variant="outline"
                    className="h-12 w-12"
                    title="Y- (down)"
                    disabled={!canCommand}
                    onClick={() => jog('down')}
                  >
                    <ArrowDown className="w-5 h-5 text-slate-600" />
                  </Button>
                  <div />
                </div>
              </div>

              {/* Step size selector */}
              <div className="space-y-2">
                <Label>Step Size (mm)</Label>
                <div className="flex gap-2">
                  {appConfig.jogStepsMm.map((s, i) => (
                    <Button
                      key={s}
                      id={`jog-step-${s}`}
                      variant="outline"
                      size="sm"
                      className={cn(
                        'flex-1',
                        i === stepIndex && 'bg-blue-50 border-blue-400 text-blue-700',
                      )}
                      onClick={() => setStepIndex(i)}
                    >
                      {s}
                    </Button>
                  ))}
                </div>
              </div>

              {/* Target cell move */}
              <div className="space-y-3 pt-3 border-t border-gray-200">
                <h3 className="text-[10px] font-bold text-slate-500 uppercase">
                  Move to Cell
                </h3>
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1">
                    <Label>Row</Label>
                    <Input
                      id="target-row"
                      type="number"
                      min={0}
                      max={appConfig.rackRows - 1}
                      value={targetRow}
                      onChange={e => setTargetRow(Number(e.target.value))}
                    />
                  </div>
                  <div className="space-y-1">
                    <Label>Column</Label>
                    <Input
                      id="target-col"
                      type="number"
                      min={0}
                      max={appConfig.rackCols - 1}
                      value={targetCol}
                      onChange={e => setTargetCol(Number(e.target.value))}
                    />
                  </div>
                </div>
                <Button
                  id="move-to-compartment"
                  variant="default"
                  className="w-full"
                  disabled={!canCommand}
                  onClick={moveToCompartment}
                >
                  Move to R{targetRow} C{targetCol}
                </Button>
              </div>
            </CardContent>
          </Card>

          {/* Live position readout */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Settings2 className="w-4 h-4" />
                Live Position
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-2 space-y-2">
              <PositionRow label="X" value={gantryPosition.x} homed={gantryPosition.homed_x} />
              <PositionRow label="Y" value={gantryPosition.y} homed={gantryPosition.homed_y} />
              <PositionRow label="C" value={gantryPosition.c} homed={gantryPosition.homed_c} />
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Position row sub-component
// ---------------------------------------------------------------------------
function PositionRow({
  label,
  value,
  homed,
}: {
  label: string;
  value: number | null;
  homed: boolean;
}) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-gray-100 last:border-0">
      <span className="text-[10px] font-bold uppercase text-slate-500">{label}</span>
      <div className="flex items-center gap-2">
        {!homed && value !== null && (
          <span className="text-[9px] font-mono text-amber-500 uppercase">!HOMED</span>
        )}
        <span className="text-sm font-mono font-bold text-slate-800">
          {value !== null ? `${value.toFixed(2)} mm` : '—'}
        </span>
      </div>
    </div>
  );
}
