/**
 * src/components/EmergencyStop.tsx
 *
 * Section 7 / Section 9 Layer 1 — always visible, never disabled.
 *
 * Rules from the plan:
 *   - Always visible, never disabled (including during scan, capture, lock)
 *   - Sends "!" via sendCommand() which bypasses the lock queue server-side (Section 4.3)
 *   - CSRF token injected automatically by wsClient.send()
 *   - Shows a brief "SENT" flash after press, resets after 1.5s
 *   - Does NOT guard on userRole — the server enforces permissions
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { OctagonX } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useSystem } from '../context/SystemContext';

interface EmergencyStopProps {
  /** Override the target rack (defaults to activeRackId in context) */
  rackId?: string;
  /** Compact mode — icon-only, used in the sidebar/header */
  compact?: boolean;
}

export function EmergencyStop({ rackId, compact = false }: EmergencyStopProps) {
  const { sendCommand, activeRackId, wsStatus } = useSystem();
  const [flash, setFlash] = useState<'idle' | 'sent'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const handleStop = useCallback(() => {
    const target = rackId ?? activeRackId;
    if (!target) return;  // no rack — button is still rendered, just no-ops
    sendCommand('!', target);
    setFlash('sent');
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setFlash('idle'), 1500);
  }, [sendCommand, rackId, activeRackId]);

  const isConnected = wsStatus === 'connected';

  if (compact) {
    return (
      <button
        id="emergency-stop-compact"
        type="button"
        onClick={handleStop}
        aria-label="Emergency stop"
        title="Emergency Stop — sends ! command immediately"
        className={cn(
          'relative w-10 h-10 rounded flex items-center justify-center font-bold transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2',
          flash === 'sent'
            ? 'bg-red-400 scale-95'
            : 'bg-[#C92A2A] hover:bg-red-600 active:scale-95',
          !isConnected && 'opacity-60',
        )}
      >
        <OctagonX className="w-5 h-5 text-white" />
      </button>
    );
  }

  return (
    <button
      id="emergency-stop-button"
      type="button"
      onClick={handleStop}
      aria-label="Emergency stop — sends ! command immediately"
      title="Emergency Stop"
      className={cn(
        'group relative flex items-center justify-center gap-2 w-full px-4 py-3 rounded font-bold text-sm uppercase tracking-widest transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 select-none',
        flash === 'sent'
          ? 'bg-red-400 text-white scale-[0.98] shadow-inner'
          : 'bg-[#C92A2A] text-white hover:bg-red-600 active:scale-[0.98] shadow-md hover:shadow-red-900/30',
        !isConnected && 'opacity-70',
      )}
    >
      {/* Pulsing ring */}
      <span
        className={cn(
          'absolute inset-0 rounded border-2 border-red-500 pointer-events-none',
          flash === 'sent' ? 'animate-ping opacity-75' : 'opacity-0',
        )}
      />

      <OctagonX className="w-5 h-5 shrink-0" />

      <span>
        {flash === 'sent' ? 'STOP SENT' : 'EMERGENCY STOP'}
      </span>
    </button>
  );
}
