import React, { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { 
  LayoutDashboard, 
  Video,
  HardDrive, 
  LineChart, 
  Activity, 
  ShieldCheck,
  Server,
  Network,
  MapPin,
  ChevronDown,
  X,
  Menu,
  Search,
  LogOut,
  UserPlus,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useSystem } from '../../context/SystemContext';
import { getAllDevices } from '../../data/mockHierarchy';
import { ConnectionBar } from '../ConnectionBar';
import { EmergencyStop } from '../EmergencyStop';

const navItems = [
  { name: 'Device Network', path: '/network', icon: Network },
  { name: 'Dashboard', path: '/', icon: LayoutDashboard },
  { name: 'Live Control', path: '/live-control', icon: Video },
  { name: 'Image & Sync', path: '/image-sync', icon: HardDrive },
  { name: 'Analytics Preview', path: '/analytics', icon: LineChart },
  { name: 'System Status', path: '/system-status', icon: Activity },
  { name: 'Settings & Security', path: '/settings', icon: ShieldCheck },
];

// Admin-only nav items shown below the divider
const adminNavItems = [
  { name: 'Create User', path: '/admin/users', icon: UserPlus },
];

export function AppLayout() {
  const { activeSystem, setActiveSystem, wsStatus, auth, alerts, logout } = useSystem();
  const location = useLocation();
  const navigate = useNavigate();
  const [isSelectorOpen, setIsSelectorOpen] = useState(false);
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const devices = getAllDevices();

  const filteredDevices = devices.filter(device => 
    device.name.toLowerCase().includes(searchQuery.toLowerCase()) || 
    device.parentPath.toLowerCase().includes(searchQuery.toLowerCase()) ||
    device.ip.includes(searchQuery)
  );

  useEffect(() => {
    if (!activeSystem && location.pathname !== '/network') {
      navigate('/network', { replace: true });
    }
  }, [activeSystem, location.pathname, navigate]);

  return (
    <div className="flex bg-app-bg h-screen w-full overflow-hidden text-slate-800 font-sans relative">
      
      {/* Mobile Sidebar Overlay */}
      {isSidebarOpen && (
        <div 
          className="fixed inset-0 bg-black/60 z-20 xl:hidden"
          onClick={() => setIsSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside className={cn(
        "fixed inset-y-0 left-0 z-30 w-64 flex-shrink-0 flex flex-col border-r border-[#1E2A4F] bg-[#1E2A4F] text-white transform transition-transform duration-300 xl:relative xl:translate-x-0 h-full shadow-lg",
        isSidebarOpen ? "translate-x-0" : "-translate-x-full"
      )}>
        <div className="h-16 flex items-center px-6 border-b border-sidebar-divider justify-between shrink-0" style={{ borderColor: 'rgba(255,255,255,0.1)' }}>
          <div className="flex items-center">
            <div className="w-8 h-8 bg-white/10 rounded flex items-center justify-center font-bold text-white mr-3">A</div>
            <div>
              <h1 className="text-sm font-bold tracking-widest text-white uppercase">ALLENTOWN</h1>
              <p className="text-[10px] text-blue-200 font-mono tracking-tighter uppercase">Edge Ops v2.4.1</p>
            </div>
          </div>
          <button 
            className="xl:hidden text-slate-400 hover:text-white"
            onClick={() => setIsSidebarOpen(false)}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        
        <nav className="flex-1 overflow-y-auto py-4 px-2 custom-scrollbar">
          <ul className="space-y-1">
            {navItems.map((item) => {
              const isDisabled = !activeSystem && item.path !== '/network';
              return (
                <li key={item.path}>
                  <NavLink
                    to={isDisabled ? '#' : item.path}
                    onClick={(e) => {
                      if (isDisabled) e.preventDefault();
                    }}
                    className={({ isActive }) =>
                      cn(
                        'flex items-center px-4 py-2.5 rounded-none text-sm font-medium transition-colors cursor-pointer',
                        isActive && !isDisabled
                          ? 'bg-transparent text-white border-r-4 border-[#C92A2A]' 
                          : 'text-slate-300 hover:bg-white/5 transition-colors',
                        isDisabled && 'opacity-50 cursor-not-allowed hover:bg-transparent'
                      )
                    }
                  >
                    {({ isActive }) => (
                      <>
                        <item.icon className={cn("w-5 h-5 mr-3 shrink-0 stroke-[1.5]", isActive && !isDisabled ? "text-white" : "text-slate-400")} />
                        {item.name}
                      </>
                    )}
                  </NavLink>
                </li>
              );
            })}
          </ul>

          {/* Admin-only section */}
          {auth?.role === 'admin' && (
            <>
              <div className="mx-4 my-3 border-t border-white/10" />
              <p className="px-4 text-[9px] font-mono text-white/30 uppercase mb-1">Admin</p>
              <ul className="space-y-1">
                {adminNavItems.map((item) => (
                  <li key={item.path}>
                    <NavLink
                      to={item.path}
                      className={({ isActive }) =>
                        cn(
                          'flex items-center px-4 py-2.5 rounded-none text-sm font-medium transition-colors cursor-pointer',
                          isActive
                            ? 'bg-transparent text-white border-r-4 border-[#C92A2A]'
                            : 'text-slate-300 hover:bg-white/5 transition-colors',
                        )
                      }
                    >
                      {({ isActive }) => (
                        <>
                          <item.icon className={cn('w-5 h-5 mr-3 shrink-0 stroke-[1.5]', isActive ? 'text-white' : 'text-slate-400')} />
                          {item.name}
                        </>
                      )}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </>
          )}
        </nav>
        
        {/* Sidebar bottom: E-stop + WS status */}
        <div className="p-3 border-t border-white/10 space-y-2">
          <EmergencyStop compact={false} />
          <div className="text-[9px] font-mono text-white/30 uppercase text-center">
            {wsStatus === 'connected' ? '● WS Connected' :
             wsStatus === 'reconnecting' ? '◌ Reconnecting…' : '○ WS Offline'}
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col min-w-0 bg-[#F4F7F9] overflow-hidden relative">
        {/* Top Header */}
        <header className="h-14 flex-shrink-0 border-b border-gray-200 bg-white flex items-center justify-between px-4 xl:px-6 z-10 relative">
          <div className="flex items-center gap-3 xl:gap-6">
            <button 
              className="xl:hidden p-1.5 text-slate-500 hover:text-slate-700 rounded hover:bg-gray-100"
              onClick={() => setIsSidebarOpen(true)}
            >
              <Menu className="w-5 h-5" />
            </button>
            <div 
              className="flex flex-col cursor-pointer group px-2 py-1.5 xl:-ml-3 rounded hover:bg-gray-50 transition-colors relative"
              onClick={() => setIsSelectorOpen(!isSelectorOpen)}
            >
              <div className="flex items-center text-[10px] text-slate-500 font-mono uppercase transition-colors group-hover:text-slate-700">
                <span>Active Context</span>
                <ChevronDown className={cn("w-3 h-3 ml-1 transition-transform", isSelectorOpen && "rotate-180")} />
              </div>
              {activeSystem ? (
                <div className="flex items-center text-sm font-bold text-slate-800 uppercase mt-0.5">
                   <MapPin className="w-3.5 h-3.5 mr-1.5 text-blue-600 shrink-0" />
                   <span className="truncate max-w-[120px] xl:max-w-[200px] 2xl:max-w-xs">{activeSystem.parentPath} / </span>
                   <span className="text-blue-600 ml-1 shrink-0">{activeSystem.name}</span>
                </div>
              ) : (
                <div className="flex items-center text-sm font-bold text-blue-600 uppercase mt-1 bg-blue-50 px-2.5 py-1 rounded border border-blue-100">
                   <Server className="w-3.5 h-3.5 mr-2" />
                   Select Target System...
                </div>
              )}
            </div>

            {/* System Selector Dropdown */}
            {isSelectorOpen && (
              <>
                <div 
                  className="fixed inset-0 z-40" 
                  onClick={() => setIsSelectorOpen(false)}
                />
                <div className="absolute top-14 left-6 w-[400px] bg-white border border-gray-200 rounded-lg shadow-2xl z-50 flex flex-col max-h-[60vh] overflow-hidden">
                  <div className="p-3 border-b border-gray-100 bg-gray-50 flex flex-col gap-2 shrink-0">
                    <div className="flex justify-between items-center">
                      <span className="text-xs font-bold text-slate-700 uppercase">Select Target System</span>
                      <button onClick={() => setIsSelectorOpen(false)} className="text-slate-400 hover:text-slate-600 transition-colors">
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                    <div className="relative mt-1">
                      <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 transform -translate-y-1/2" />
                      <input 
                        type="text" 
                        placeholder="Search devices..." 
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        className="w-full bg-white border border-gray-200 rounded py-1.5 pl-8 pr-3 text-xs text-slate-800 placeholder-slate-400 focus:outline-none focus:border-blue-500"
                        onClick={(e) => e.stopPropagation()}
                      />
                    </div>
                  </div>
                  <div className="overflow-y-auto custom-scrollbar p-2 space-y-1">
                    {filteredDevices.length > 0 ? filteredDevices.map((device) => {
                      const isActive = activeSystem?.id === device.id;
                      return (
                        <div
                          key={device.id}
                          className={cn(
                            "flex items-center p-3 rounded cursor-pointer transition-colors border",
                            isActive 
                              ? "bg-blue-50 border-blue-200" 
                              : "bg-transparent border-transparent hover:bg-gray-50"
                          )}
                          onClick={() => {
                            setActiveSystem(device);
                            setIsSelectorOpen(false);
                            if (location.pathname === '/network') navigate('/');
                          }}
                        >
                          <Server className={cn("w-5 h-5 mr-3 shrink-0", isActive ? "text-blue-600" : "text-slate-400")} />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center justify-between mb-0.5">
                              <span className={cn("text-sm font-bold uppercase truncate", isActive ? "text-blue-700" : "text-slate-800")}>
                                {device.name}
                              </span>
                              <div className="flex items-center">
                                <span className={cn(
                                  "w-1.5 h-1.5 rounded-full mr-1.5",
                                  device.status === 'online' ? "bg-green-500 animate-pulse" : 
                                  device.status === 'offline' ? "bg-red-500" : "bg-amber-500"
                                )} />
                                <span className="text-[9px] uppercase font-mono text-slate-500">{device.status}</span>
                              </div>
                            </div>
                            <div className="text-[10px] text-slate-500 uppercase truncate">
                              {device.parentPath}
                            </div>
                          </div>
                        </div>
                      )
                    }) : (
                       <div className="py-6 text-center text-slate-400 text-xs">
                         No devices found matching your search.
                       </div>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>

          <div className="flex items-center gap-3 text-[10px] font-mono">
            {/* Role badge */}
            {auth && (
              <div className="hidden sm:flex items-center gap-2 border border-gray-200 rounded px-2 py-1 bg-gray-50">
                <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
                <span className="text-slate-600 uppercase">{auth.role}</span>
                <span className="text-slate-400">·</span>
                <span className="text-slate-500">{auth.userId.slice(0, 8)}</span>
              </div>
            )}

            {/* Alert count */}
            {alerts.length > 0 && (
              <div className="flex items-center gap-1 text-red-500">
                <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
                <span>{alerts.length}</span>
              </div>
            )}

            {/* Logout */}
            {auth && (
              <button
                id="header-logout-button"
                onClick={logout}
                title="Sign out"
                className="w-8 h-8 flex items-center justify-center rounded bg-gray-100 text-slate-500 hover:bg-red-50 hover:text-red-500 transition-colors"
              >
                <LogOut className="w-4 h-4" />
              </button>
            )}
          </div>
        </header>

        {/* Connection status bar — Section 7 */}
        <ConnectionBar />

        {/* Page Content */}
        <div className="flex-1 overflow-auto p-6 text-slate-800 custom-scrollbar">
          <div className="mx-auto h-full max-w-[1400px]">
            <Outlet />
          </div>
        </div>
      </main>
      
    </div>
  );
}
