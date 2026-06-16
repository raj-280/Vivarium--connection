/**
 * src/pages/AdminUsers.tsx
 *
 * Admin-only page: create new user accounts (admin / operator / viewer).
 * Visible only to users whose role === 'admin'.
 * Calls POST /admin/users — added to server/api/routes.py.
 */

import React, { useState } from 'react';
import { UserPlus, CheckCircle, AlertCircle, Loader2 } from 'lucide-react';
import { useSystem } from '../context/SystemContext';
import appConfig from '../config/app.config';

type Role = 'admin' | 'operator' | 'viewer';

interface CreatedUser {
  user_id: string;
  username: string;
  role: Role;
}

export default function AdminUsers() {
  const { auth, userRole } = useSystem();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole]         = useState<Role>('operator');
  const [loading, setLoading]   = useState(false);
  const [success, setSuccess]   = useState<CreatedUser | null>(null);
  const [error, setError]       = useState<string | null>(null);

  // Guard — only admins should reach this page
  if (userRole !== 'admin') {
    return (
      <div className="p-8 text-center text-red-400 font-mono text-sm">
        Access denied — admin role required.
      </div>
    );
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      const res = await fetch(`${appConfig.apiBaseUrl}/admin/users`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${auth?.token ?? ''}`,
        },
        body: JSON.stringify({ username, password, role }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail ?? `Server error ${res.status}`);
      }

      const data: CreatedUser = await res.json();
      setSuccess(data);
      setUsername('');
      setPassword('');
      setRole('operator');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to create user');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="p-6 max-w-lg mx-auto">
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <div className="w-9 h-9 rounded bg-blue-600/20 flex items-center justify-center">
          <UserPlus className="w-5 h-5 text-blue-400" />
        </div>
        <div>
          <h1 className="text-white font-bold text-lg">Create User</h1>
          <p className="text-slate-400 text-xs font-mono">Admin panel — add new accounts</p>
        </div>
      </div>

      {/* Form */}
      <form
        onSubmit={handleSubmit}
        className="bg-[#1a2440] rounded-lg border border-white/10 p-6 space-y-5"
      >
        {/* Username */}
        <div className="space-y-1">
          <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="new-username">
            Username
          </label>
          <input
            id="new-username"
            type="text"
            required
            value={username}
            onChange={e => setUsername(e.target.value)}
            placeholder="e.g. john_operator"
            className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
          />
        </div>

        {/* Password */}
        <div className="space-y-1">
          <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="new-password">
            Password
          </label>
          <input
            id="new-password"
            type="password"
            required
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder="••••••••"
            className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:border-blue-500 transition-colors"
          />
        </div>

        {/* Role selector */}
        <div className="space-y-1">
          <label className="text-[10px] font-mono text-slate-400 uppercase" htmlFor="new-role">
            Role
          </label>
          <select
            id="new-role"
            value={role}
            onChange={e => setRole(e.target.value as Role)}
            className="w-full bg-[#0f1729] border border-white/10 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500 transition-colors"
          >
            <option value="admin">admin — full access</option>
            <option value="operator">operator — command racks</option>
            <option value="viewer">viewer — read-only</option>
          </select>
        </div>

        {/* Error banner */}
        {error && (
          <div className="flex items-start gap-2 bg-red-900/20 border border-red-500/30 rounded px-3 py-2">
            <AlertCircle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
            <p className="text-xs text-red-400 font-mono">{error}</p>
          </div>
        )}

        {/* Success banner */}
        {success && (
          <div className="flex items-start gap-2 bg-green-900/20 border border-green-500/30 rounded px-3 py-2">
            <CheckCircle className="w-4 h-4 text-green-400 mt-0.5 shrink-0" />
            <div>
              <p className="text-xs text-green-400 font-mono font-bold">User created!</p>
              <p className="text-[11px] text-green-300 font-mono mt-0.5">
                id: {success.user_id} · {success.username} · {success.role}
              </p>
            </div>
          </div>
        )}

        {/* Submit */}
        <button
          id="create-user-submit"
          type="submit"
          disabled={loading}
          className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-60 text-white font-bold text-sm uppercase py-2.5 rounded transition-colors"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <UserPlus className="w-4 h-4" />}
          {loading ? 'Creating…' : 'Create User'}
        </button>
      </form>

      <p className="mt-4 text-[10px] text-slate-600 text-center font-mono uppercase">
        Only admins can access this page · credentials are bcrypt-hashed
      </p>
    </div>
  );
}
