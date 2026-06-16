import React, { useState } from 'react';
import { useSystem } from '../context/SystemContext';
import { Loader2, LogIn } from 'lucide-react';
import Setup from './Setup';

export default function Login() {
  const { login } = useSystem();
  const [username, setUsername]   = useState('');
  const [password, setPassword]   = useState('');
  const [loading, setLoading]     = useState(false);
  const [error, setError]         = useState<string | null>(null);
  const [showSetup, setShowSetup] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  // Show the first-time setup page when requested
  if (showSetup) {
    return (
      <Setup
        onBack={() => setShowSetup(false)}
        onCreated={() => setShowSetup(false)}
      />
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0f1729]">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-12 h-12 bg-[#C92A2A] rounded flex items-center justify-center font-bold text-white text-lg mb-3">
            A
          </div>
          <h1 className="text-xl font-bold tracking-widest text-white uppercase">Allentown</h1>
          <p className="text-xs text-blue-300 font-mono tracking-tighter uppercase mt-1">
            Vivarium Gantry System
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="bg-[#1a2440] rounded-lg p-6 border border-white/10 space-y-4"
        >
          <h2 className="text-sm font-bold text-white uppercase tracking-wide">Sign In</h2>

          <div className="space-y-1">
            <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="login-username">
              Username
            </label>
            <input
              id="login-username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={e => setUsername(e.target.value)}
              required
              className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
              placeholder="admin"
            />
          </div>

          <div className="space-y-1">
            <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="login-password">
              Password
            </label>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
              placeholder="••••••••"
            />
          </div>

          {error && (
            <p className="text-xs text-red-400 font-mono bg-red-900/20 px-3 py-2 rounded">
              {error}
            </p>
          )}

          <button
            id="login-submit-button"
            type="submit"
            disabled={loading}
            className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-60 text-white font-bold text-sm uppercase py-2.5 rounded transition-colors"
          >
            {loading
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <LogIn className="w-4 h-4" />
            }
            {loading ? 'Signing in…' : 'Sign In'}
          </button>

          <p className="text-[10px] text-slate-600 text-center font-mono uppercase">
            Dev: use credentials seeded in the DB
          </p>
        </form>

        {/* First-time setup link */}
        <button
          id="login-setup-link"
          type="button"
          onClick={() => setShowSetup(true)}
          className="mt-4 w-full text-center text-[10px] text-slate-500 hover:text-blue-400 font-mono uppercase transition-colors"
        >
          First time? Create admin account →
        </button>
      </div>
    </div>
  );
}
