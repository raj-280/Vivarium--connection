/**
 * src/components/CameraPanel.tsx
 *
 * Section 7 / Section 8 — Live video + Capture button.
 *
 * Behaviour:
 *   - When streamUrl is non-null (lock held), opens a WebRTC session to the
 *     Pi's MediaMTX using the WHEP protocol (RFC draft).
 *   - The WHEP URL (e.g. http://192.168.1.50:8889/rack-001/whep) is sent by
 *     the server over WebSocket as part of the stream_url message.
 *   - Capture button sends "CAPTURE" command over WS (CSRF injected by wsClient).
 *   - On CAPTURE_STARTED:  spinner appears.
 *   - On capture_complete: spinner clears, success flash shown.
 *   - Hard 60s client-side timeout clears spinner with an error if
 *     capture_complete never arrives (Section 9 Layer 1).
 *   - Viewer role: no Capture button (read-only feed).
 *   - When streamUrl is null: placeholder is shown.
 *
 * BUG FIX (Bug 5 — WebRTC implementation):
 *   The original code used <video src={streamUrl}> which is WRONG for WHEP.
 *   A WHEP endpoint is NOT an MP4/HLS URL — it requires a multi-step WebRTC
 *   handshake over HTTP (POST SDP offer → get SDP answer → RTCPeerConnection).
 *   Putting a WHEP URL in <video src> just fires a GET and gets a 405 error,
 *   leaving the video element permanently blank.
 *
 *   Fix: useWhepStream() hook implements the correct WHEP negotiation:
 *     1. Create RTCPeerConnection with a single recv-only transceiver.
 *     2. Create an SDP offer locally (createOffer).
 *     3. POST the offer to the WHEP endpoint (Content-Type: application/sdp).
 *     4. Set the SDP answer from the response as the remote description.
 *     5. Attach the resulting MediaStream to video.srcObject (NOT video.src).
 *
 *   MJPEG fallback: if streamUrl is empty but mjpegUrl is set, a plain <img>
 *   element is used as a last resort (works in all browsers, no JS needed).
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
type StreamStatus = 'idle' | 'connecting' | 'connected' | 'error';

interface CameraPanelProps {
  rackId?: string;
  className?: string;
}

// ===========================================================================
// useWhepStream — WHEP WebRTC negotiation hook
// ===========================================================================
//
// WHEP (WebRTC HTTP Egress Protocol) is MediaMTX's mechanism for browser-side
// WebRTC.  It is NOT a media URL — it is an HTTP signalling endpoint.
//
// Protocol:
//   1. Client creates an RTCPeerConnection and adds a recvonly transceiver.
//   2. Client calls createOffer() and setLocalDescription(offer).
//   3. Client waits for ICE gathering to complete.
//   4. Client POSTs the gathered SDP to the WHEP URL
//      (Content-Type: application/sdp).
//   5. Server responds with 201 Created + SDP answer body.
//   6. Client calls setRemoteDescription(answer).
//   7. RTCPeerConnection fires ontrack — client attaches stream to video.srcObject.
//
// FIX (Bug 7): ICE gather timeout reduced from 5s to 2s — on a LAN there are
// no STUN candidates to wait for, so gathering completes almost instantly.
// Sending a partial SDP after 5s just delays the error.
//
// FIX (Bug 8): sourceOnDemand retry — MediaMTX registers the path at startup
// but the camera hardware only opens on the FIRST WHEP connection.  The first
// attempt can fail with a 404/503 because MediaMTX hasn't opened the source
// yet.  We retry up to MAX_WHEP_ATTEMPTS times with an exponential backoff
// starting at WHEP_RETRY_BASE_MS so the camera has time to start.

const MAX_WHEP_ATTEMPTS = 4;
const WHEP_RETRY_BASE_MS = 1500; // doubles each attempt: 1.5s, 3s, 6s

function useWhepStream(
  videoRef: React.RefObject<HTMLVideoElement>,
  whepUrl: string | null,
) {
  const [status, setStatus] = useState<StreamStatus>('idle');
  const [error, setError]   = useState<string | null>(null);
  const pcRef = useRef<RTCPeerConnection | null>(null);

  const teardown = useCallback(() => {
    if (pcRef.current) {
      pcRef.current.ontrack          = null;
      pcRef.current.onicecandidate   = null;
      pcRef.current.onconnectionstatechange = null;
      pcRef.current.close();
      pcRef.current = null;
    }
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
  }, [videoRef]);

  useEffect(() => {
    if (!whepUrl) {
      teardown();
      setStatus('idle');
      setError(null);
      return;
    }

    let cancelled = false;

    // FIX (Bug 8): retry loop so sourceOnDemand has time to open the camera.
    const negotiate = async (attempt: number): Promise<void> => {
      if (cancelled) return;
      teardown();
      if (attempt === 1) {
        setStatus('connecting');
        setError(null);
      }

      const pc = new RTCPeerConnection({
        iceServers: [],  // LAN-only — no STUN/TURN needed
      });
      pcRef.current = pc;

      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });

      pc.ontrack = (evt) => {
        if (cancelled) return;
        if (videoRef.current && evt.streams[0]) {
          videoRef.current.srcObject = evt.streams[0];
          setStatus('connected');
        }
      };

      pc.onconnectionstatechange = () => {
        if (cancelled) return;
        const state = pc.connectionState;
        if (state === 'failed' || state === 'disconnected' || state === 'closed') {
          setStatus('error');
          setError(`WebRTC connection ${state}.`);
        }
      };

      try {
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        // FIX (Bug 7): 2s ICE gather timeout instead of 5s.
        // On a LAN there are no STUN candidates; gathering is near-instant.
        await new Promise<void>((resolve) => {
          if (pc.iceGatheringState === 'complete') { resolve(); return; }
          pc.onicegatheringstatechange = () => {
            if (pc.iceGatheringState === 'complete') resolve();
          };
          setTimeout(resolve, 2000); // FIX: was 5000
        });

        if (cancelled) return;

        const sdpOffer = pc.localDescription?.sdp;
        if (!sdpOffer) throw new Error('Failed to generate SDP offer.');

        const resp = await fetch(whepUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/sdp' },
          body: sdpOffer,
        });

        // FIX (Bug 8): 404/503 = sourceOnDemand camera not open yet — retry.
        if (!resp.ok) {
          if ((resp.status === 404 || resp.status === 503) && attempt < MAX_WHEP_ATTEMPTS) {
            const delay = WHEP_RETRY_BASE_MS * Math.pow(2, attempt - 1);
            console.info(
              `[useWhepStream] WHEP attempt ${attempt} got HTTP ${resp.status} — ` +
              `camera starting up, retrying in ${delay}ms…`
            );
            teardown();
            await new Promise(r => setTimeout(r, delay));
            return negotiate(attempt + 1);
          }
          throw new Error(
            `WHEP handshake failed: HTTP ${resp.status} from ${whepUrl}. ` +
            `Check that the Pi is online and MEDIAMTX_PI_HOST is set correctly.`
          );
        }

        const sdpAnswer = await resp.text();
        if (cancelled) return;

        await pc.setRemoteDescription(
          new RTCSessionDescription({ type: 'answer', sdp: sdpAnswer })
        );
        // ontrack fires → setStatus('connected')

      } catch (err: unknown) {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : String(err);
        // Retry on network errors too (camera may be opening)
        if (attempt < MAX_WHEP_ATTEMPTS) {
          const delay = WHEP_RETRY_BASE_MS * Math.pow(2, attempt - 1);
          console.info(`[useWhepStream] attempt ${attempt} failed (${msg}) — retrying in ${delay}ms…`);
          teardown();
          await new Promise(r => setTimeout(r, delay));
          return negotiate(attempt + 1);
        }
        setStatus('error');
        setError(msg);
        teardown();
      }
    };

    negotiate(1);

    return () => {
      cancelled = true;
      teardown();
      setStatus('idle');
    };
  }, [whepUrl, videoRef, teardown]);

  return { streamStatus: status, streamError: error };
}

export function CameraPanel({ rackId, className }: CameraPanelProps) {
  const { sendCommand, activeRackId, userRole, streamUrl, mjpegUrl } = useSystem();
  const target = rackId ?? activeRackId;
  const isViewer = userRole === 'viewer';

  const [captureState, setCaptureState] = useState<CaptureState>('idle');
  const [captureError, setCaptureError] = useState<string | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  // ── WHEP WebRTC hook (BUG FIX: replaces broken <video src={whepUrl}>) ──
  // streamUrl is the WHEP URL e.g. http://192.168.1.50:8889/rack-001/whep.
  // The hook negotiates RTCPeerConnection and sets video.srcObject automatically.
  // If streamUrl is empty but mjpegUrl is set, the MJPEG fallback is shown instead.
  const whepUrl = streamUrl || null;
  const { streamStatus, streamError } = useWhepStream(videoRef, whepUrl);

  // Decide what to render in the video area:
  //   1. WHEP (WebRTC) — primary, handled by useWhepStream via video.srcObject
  //   2. MJPEG fallback — <img> element, works in all browsers
  //   3. Placeholder — no lock held yet
  const showWhep  = Boolean(whepUrl);
  const showMjpeg = !showWhep && Boolean(mjpegUrl);
  const hasStream = showWhep || showMjpeg;

  // FIX (Bug 3): consolidated capture event handler — previously split into
  // two separate blocks in the same useEffect which caused:
  //   a) double timeout registration on every CAPTURE_STARTED
  //   b) state never set to 'pending' from the first block
  //   c) first block's timeout firing at 60s even after capture_complete
  //      cleared the second block's timeout, because each block registered
  //      its own independent timer.
  // Now: one handler, one timer ref, one clear path.
  //
  // FIX (Bug 4): use addMessageListener/removeMessageListener instead of
  // setOnMessage so this component doesn't stomp other WS listeners.
  useEffect(() => {
    const handler = (msg: WsMessage) => {
      const data = msg.data as Record<string, unknown>;
      const raw = typeof data?.['raw'] === 'string' ? (data['raw'] as string) : '';

      // CAPTURE_STARTED: set pending + arm the 60s safety timeout
      if (
        msg.type === 'capture_complete' ? false :
        (msg.type === 'status' && (data['CAPTURE_STARTED'] || raw.startsWith('CAPTURE_STARTED')))
      ) {
        setCaptureState('pending');
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = setTimeout(() => {
          setCaptureState('error');
          setCaptureError('Capture timed out — no response from Pi after 60s.');
        }, appConfig.captureTimeoutMs);
        return;
      }

      // capture_complete: clear timer, flash success
      if (msg.type === 'capture_complete') {
        if (timeoutRef.current) clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
        setCaptureState('success');
        setTimeout(() => setCaptureState('idle'), 2500);
      }
    };

    wsClient.addMessageListener(handler);
    return () => wsClient.removeMessageListener(handler);
  }, []);

  useEffect(() => () => { if (timeoutRef.current) clearTimeout(timeoutRef.current); }, []);

  const handleCapture = useCallback(() => {
    if (!target || captureState === 'pending') return;
    setCaptureState('pending');
    setCaptureError(null);
    wsClient.send({ type: 'CAPTURE', rack_id: target, command: 'CAPTURE' });
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => {
      setCaptureState('error');
      setCaptureError('No acknowledgement from server. Check connection.');
    }, appConfig.captureTimeoutMs);
  }, [target, captureState]);

  // Derive a human-readable stream badge label
  const streamBadge = showWhep
    ? (streamStatus === 'connected' ? 'WebRTC ✓' : streamStatus === 'connecting' ? 'Connecting…' : 'WebRTC')
    : showMjpeg ? 'MJPEG' : null;
  const badgeColour = streamStatus === 'connected' || showMjpeg ? 'text-green-400' : 'text-yellow-400';

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
          {streamBadge ? (
            <span className={cn('flex items-center gap-1', badgeColour)}>
              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
              {streamBadge}
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

        {/* ──────────────────────────────────────────────────
             WebRTC (WHEP) — primary path
             BUG FIX: video.srcObject is set by useWhepStream() above.
             We do NOT use src=... for WHEP URLs — that would fire a plain
             HTTP GET expecting an MP4, which always fails for a WHEP endpoint.
             The <video> element is always mounted when showWhep=true so the
             ref is stable for the hook to write into.
             ────────────────────────────────────────────────── */}
        <video
          ref={videoRef}
          className={cn(
            'absolute inset-0 w-full h-full object-contain',
            !showWhep && 'hidden',
          )}
          autoPlay
          muted
          playsInline
          aria-label={`Live camera feed for rack ${target}`}
          // NOTE: no `src` prop here — srcObject is set by useWhepStream()
        />

        {/* ──────────────────────────────────────────────────
             MJPEG fallback — plain <img> polling the MJPEG endpoint.
             Works in all browsers without any JS. Shown only when
             streamUrl is empty but mjpegUrl is set.
             ────────────────────────────────────────────────── */}
        {showMjpeg && (
          <img
            src={mjpegUrl!}
            className="absolute inset-0 w-full h-full object-contain"
            alt={`MJPEG fallback stream for rack ${target}`}
          />
        )}

        {/* Placeholder — no lock held */}
        {!hasStream && (
          <div className="flex flex-col items-center gap-3 text-slate-600">
            <Camera className="w-10 h-10 opacity-30" />
            <span className="text-[11px] font-mono uppercase">
              {target ? 'Awaiting lock for stream…' : 'No rack selected'}
            </span>
          </div>
        )}

        {/* WebRTC connecting spinner (shown while WHEP negotiation is in progress) */}
        {showWhep && streamStatus === 'connecting' && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/60">
            <div className="flex flex-col items-center gap-2 text-slate-300">
              <Loader2 className="w-8 h-8 animate-spin text-blue-400" />
              <span className="text-[11px] font-mono uppercase">Negotiating WebRTC…</span>
            </div>
          </div>
        )}

        {/* WebRTC error banner (WHEP negotiation failed) */}
        {showWhep && streamStatus === 'error' && streamError && (
          <div className="absolute bottom-10 left-0 right-0 flex justify-center px-4">
            <div className="flex items-center gap-2 bg-orange-900/80 text-orange-200 text-[11px] font-mono px-3 py-2 rounded-lg max-w-sm text-center">
              <AlertCircle className="w-4 h-4 shrink-0" />
              <span>{streamError}</span>
            </div>
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
