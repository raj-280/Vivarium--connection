/**
 * src/pages/Setup.tsx
 *
 * First-time setup page — create the first admin account without logging in.
 * Accessible from the Login page via "First time? Create admin account" link.
 * The backend /setup endpoint locks itself once any admin exists.
 */

import React, { useState } from 'react';
import { ShieldCheck, Loader2, CheckCircle, AlertCircle, ArrowLeft } from 'lucide-react';
import appConfig from '../config/app.config';

interface Props {
  onBack: () => void;          // go back to Login
  onCreated: () => void;       // redirect to Login after success
}

export default function Setup({ onBack, onCreated }: Props) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm,  setConfirm]  = useState('');
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState<string | null>(null);
  const [done,     setDone]     = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }
    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${appConfig.apiBaseUrl}/setup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail ?? `Error ${res.status}`);
      }

      setDone(true);
      setTimeout(onCreated, 2000);   // auto-navigate back to login after 2s
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Setup failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0f1729]">
      <div className="w-full max-w-sm">

        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-12 h-12 bg-blue-600/30 border border-blue-500/40 rounded flex items-center justify-center mb-3">
            <ShieldCheck className="w-6 h-6 text-blue-400" />
          </div>
          <h1 className="text-xl font-bold tracking-widest text-white uppercase">First-Time Setup</h1>
          <p className="text-xs text-blue-300 font-mono tracking-tighter uppercase mt-1">
            Create your admin account
          </p>
        </div>

        {done ? (
          /* Success state */
          <div className="bg-[#1a2440] rounded-lg p-6 border border-green-500/30 flex flex-col items-center gap-3">
            <CheckCircle className="w-10 h-10 text-green-400" />
            <p className="text-white font-bold">Admin account created!</p>
            <p className="text-slate-400 text-xs font-mono text-center">
              Redirecting to login…
            </p>
          </div>
        ) : (
          <form
            onSubmit={handleSubmit}
            className="bg-[#1a2440] rounded-lg p-6 border border-white/10 space-y-4"
          >
            <p className="text-[10px] text-slate-400 font-mono uppercase bg-blue-900/20 border border-blue-500/20 rounded px-3 py-2">
              ℹ This form is disabled once any admin account exists.
            </p>

            {/* Username */}
            <div className="space-y-1">
              <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="setup-username">
                Admin Username
              </label>
              <input
                id="setup-username"
                type="text"
                required
                value={username}
                onChange={e => setUsername(e.target.value)}
                placeholder="admin"
                className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
              />
            </div>

            {/* Password */}
            <div className="space-y-1">
              <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="setup-password">
                Password
              </label>
              <input
                id="setup-password"
                type="password"
                required
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
              />
            </div>

            {/* Confirm Password */}
            <div className="space-y-1">
              <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="setup-confirm">
                Confirm Password
              </label>
              <input
                id="setup-confirm"
                type="password"
                required
                value={confirm}
                onChange={e => setConfirm(e.target.value)}
                placeholder="••••••••"
                className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
              />
            </div>

            {/* Error */}
            {error && (
              <div className="flex items-start gap-2 bg-red-900/20 border border-red-500/30 rounded px-3 py-2">
                <AlertCircle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
                <p className="text-xs text-red-400 font-mono">{error}</p>
              </div>
            )}

            {/* Submit */}
            <button
              id="setup-submit-button"
              type="submit"
              disabled={loading}
              className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-60 text-white font-bold text-sm uppercase py-2.5 rounded transition-colors"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
              {loading ? 'Creating…' : 'Create Admin Account'}
            </button>

            {/* Back */}
            <button
              type="button"
              onClick={onBack}
              className="w-full flex items-center justify-center gap-1.5 text-slate-500 hover:text-slate-300 text-xs font-mono uppercase transition-colors pt-1"
            >
              <ArrowLeft className="w-3.5 h-3.5" />
              Back to Login
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
