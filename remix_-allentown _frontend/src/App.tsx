import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { SystemProvider, useSystem } from './context/SystemContext';
import { AppLayout } from './components/layout/AppLayout';

import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import LiveControl from './pages/LiveControl';
import ImageSyncManager from './pages/ImageSyncManager';
import AnalyticsPreview from './pages/AnalyticsPreview';
import SystemStatus from './pages/SystemStatus';
import Settings from './pages/Settings';
import FleetManager from './pages/FleetManager';
import AdminUsers from './pages/AdminUsers';

/**
 * AuthGate — renders Login when the user has no auth token,
 * otherwise renders the protected app shell.
 *
 * BrowserRouter is now ABOVE this component (see App below), so Login
 * has full router context and can safely use <Link> / useNavigate.
 */
function AuthGate() {
  const { auth } = useSystem();

  if (!auth) {
    // Login now has router context — BrowserRouter wraps everything above
    return <Login />;
  }

  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Dashboard />} />
        <Route path="network" element={<FleetManager />} />
        <Route path="live-control" element={<LiveControl />} />
        <Route path="image-sync" element={<ImageSyncManager />} />
        <Route path="analytics" element={<AnalyticsPreview />} />
        <Route path="system-status" element={<SystemStatus />} />
        <Route path="settings" element={<Settings />} />
        <Route path="admin/users" element={<AdminUsers />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    // BrowserRouter is always present, wrapping SystemProvider + AuthGate
    // so all pages (including Login) have router context.
    <BrowserRouter>
      <SystemProvider>
        <AuthGate />
      </SystemProvider>
    </BrowserRouter>
  );
}
