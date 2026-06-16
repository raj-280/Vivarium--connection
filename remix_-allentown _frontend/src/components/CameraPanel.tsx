/**
 * src/components/CameraPanel.tsx
 *
 * Section 7 / Section 8 — Live video + Capture button.
 *
 * Behaviour:
 *   - When streamUrl is non-null (lock held), opens a <video> element at
 *     that URL for WebRTC via go2rtc.
 *   - Capture button sends "CAPTURE" command over WS (CSRF injected by wsClient).
 *   - On CAPTURE_STARTED:  spinner appears.
 *   - On capture_complete: spinner clears, success flash shown.
 *   - Hard 60s client-side timeout clears spinner with an error if
 *     capture_complete never arrives (Section 9 Layer 1).
 *   - Viewer role: no Capture button (read-only feed).
 *   - When streamUrl is null: placeholder is shown.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Camera, Loader2, CheckCircle, AlertCircle, Maximize, Video } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useSystem } from '../context/SystemContext';
import appConfig from '../config/app.config';
import { Button } from './ui/Button';
import type { WsMessage } from '../types/gantry.types';
import wsClient from '../lib/wsClient';

type CaptureState = 'idle' | 'pending' | 'success' | 'error';

interface CameraPanelProps {
  rackId?: string;
  className?: string;
}

export function CameraPanel({ rackId, className }: CameraPanelProps) {
  const { sendCommand, activeRackId, userRole, streamUrl } = useSystem();
  const target = rackId ?? activeRackId;
  const isViewer = userRole === 'viewer';

  const [captureState, setCaptureState] = useState<CaptureState>('idle');
  const [captureError, setCaptureError] = useState<string | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  // ── Listen for CAPTURE_STARTED and capture_complete on the global WS ──
  useEffect(() => {
    const handler = (msg: WsMessage) => {
      if (msg.type === 'status' && (msg.data as Record<string, unknown>)['CAPTURE_STARTED']) {
        // Pi published CAPTURE_STARTED — start the hard timeout
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = setTimeout(() => {
          setCaptureState('error');
          setCaptureError('Capture timed out — no response from Pi after 60s.');
        }, appConfig.captureTimeoutMs);
      }

      if (msg.type === 'capture_complete') {
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        setCaptureState('success');
        setTimeout(() => setCaptureState('idle'), 2500);
      }

      // If we see a response with CAPTURE_STARTED inside the data blob
      if (
        msg.type === 'status' &&
        typeof (msg.data as Record<string, unknown>)['raw'] === 'string' &&
        ((msg.data as Record<string, unknown>)['raw'] as string).startsWith('CAPTURE_STARTED')
      ) {
        setCaptureState('pending');
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = setTimeout(() => {
          setCaptureState('error');
          setCaptureError('Capture timed out (60s).');
        }, appConfig.captureTimeoutMs);
      }
    };

    wsClient.setOnMessage(handler);
    return () => {
      // Restore the context handler on unmount — SystemContext re-registers it
      // on the next render cycle.
    };
  }, []);

  // Cleanup timeout on unmount
  useEffect(() => () => { if (timeoutRef.current) clearTimeout(timeoutRef.current); }, []);

  const handleCapture = useCallback(() => {
    if (!target || captureState === 'pending') return;
    setCaptureState('pending');
    setCaptureError(null);
    // wsClient injects csrf_token automatically for type=CAPTURE
    wsClient.send({ type: 'CAPTURE', rack_id: target, command: 'CAPTURE' });

    // Start the hard timeout immediately (server may not ACK in time)
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => {
      setCaptureState('error');
      setCaptureError('No acknowledgement from server. Check connection.');
    }, appConfig.captureTimeoutMs);
  }, [target, captureState]);

  const hasStream = Boolean(streamUrl);

  return (
    <div
      id="camera-panel"
      className={cn(
        'flex flex-col overflow-hidden rounded-lg border border-gray-200 bg-gray-950',
        className,
      )}
    >
      {/* Header bar */}
      <div className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-800">
        <div className="flex items-center gap-2 text-[10px] font-mono text-slate-400 uppercase">
          <Video className="w-3 h-3" />
          <span>Live Feed</span>
          {hasStream ? (
            <span className="flex items-center gap-1 text-green-400">
              <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
              WebRTC
            </span>
          ) : (
            <span className="text-slate-600">No stream</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {hasStream && (
            <span className="text-[9px] font-mono text-slate-600 uppercase">
              {target ?? '—'}
            </span>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6 text-slate-500 hover:text-slate-300"
            aria-label="Fullscreen"
            onClick={() => videoRef.current?.requestFullscreen?.()}
          >
            <Maximize className="w-3 h-3" />
          </Button>
        </div>
      </div>

      {/* Video / placeholder */}
      <div className="relative flex-1 min-h-[260px] flex items-center justify-center bg-gray-950">
        {hasStream ? (
          <video
            ref={videoRef}
            className="absolute inset-0 w-full h-full object-contain"
            autoPlay
            muted
            playsInline
            src={streamUrl!}
            aria-label={`Live camera feed for rack ${target}`}
          />
        ) : (
          <div className="flex flex-col items-center gap-3 text-slate-600">
            <Camera className="w-10 h-10 opacity-30" />
            <span className="text-[11px] font-mono uppercase">
              {target ? 'Awaiting lock for stream…' : 'No rack selected'}
            </span>
          </div>
        )}

        {/* Position overlay (bottom-left) */}
        <PositionOverlay />

        {/* Capture state overlay */}
        {captureState === 'pending' && (
          <div className="absolute inset-0 bg-black/40 flex items-center justify-center">
            <div className="flex flex-col items-center gap-2 text-white">
              <Loader2 className="w-8 h-8 animate-spin text-blue-400" />
              <span className="text-xs font-mono uppercase">Capturing…</span>
            </div>
          </div>
        )}
        {captureState === 'success' && (
          <div className="absolute inset-0 bg-green-900/30 flex items-center justify-center pointer-events-none">
            <CheckCircle className="w-12 h-12 text-green-400 animate-in zoom-in duration-200" />
          </div>
        )}
        {captureState === 'error' && (
          <div className="absolute bottom-4 left-0 right-0 flex justify-center px-4">
            <div className="flex items-center gap-2 bg-red-900/80 text-red-200 text-[11px] font-mono px-3 py-2 rounded-lg">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {captureError ?? 'Capture failed.'}
            </div>
          </div>
        )}
      </div>

      {/* Capture button row */}
      {!isViewer && (
        <div className="p-3 bg-gray-900 border-t border-gray-800 flex items-center gap-3">
          <button
            id="capture-image-button"
            type="button"
            disabled={!target || captureState === 'pending'}
            onClick={handleCapture}
            className={cn(
              'flex-1 flex items-center justify-center gap-2 py-2 rounded text-sm font-semibold uppercase tracking-wide transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-blue-500',
              captureState === 'pending'
                ? 'bg-blue-900/50 text-blue-300 cursor-wait'
                : 'bg-blue-600 text-white hover:bg-blue-500 active:scale-[0.98]',
              (!target) && 'opacity-50 cursor-not-allowed',
            )}
          >
            {captureState === 'pending'
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <Camera className="w-4 h-4" />
            }
            <span>{captureState === 'pending' ? 'Capturing…' : 'Capture Image'}</span>
            <span className="text-[10px] bg-blue-900/60 px-1.5 py-0.5 rounded font-mono">
              Space
            </span>
          </button>

          {/* Dismiss error */}
          {captureState === 'error' && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => { setCaptureState('idle'); setCaptureError(null); }}
              className="text-slate-400 text-xs"
            >
              Dismiss
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Position overlay — reads directly from context
// ---------------------------------------------------------------------------
function PositionOverlay() {
  const { gantryPosition } = useSystem();
  const { x, y, c } = gantryPosition;
  if (x === null && y === null) return null;

  return (
    <div className="absolute bottom-3 left-3 font-mono text-[10px] space-y-0.5 text-green-400 bg-black/50 px-2 py-1.5 rounded pointer-events-none">
      <div>X: {x?.toFixed(1) ?? '—'} mm</div>
      <div>Y: {y?.toFixed(1) ?? '—'} mm</div>
      <div>C: {c?.toFixed(1) ?? '—'} mm</div>
    </div>
  );
}
