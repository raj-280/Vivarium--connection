import React from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Progress } from '../components/ui/Progress';
import { Button } from '../components/ui/Button';
import { Play, Square, AlertCircle, Server, Cloud, HardDrive, Activity } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useSystem } from '../context/SystemContext';

export default function Dashboard() {
  const { activeSystem } = useSystem();

  return (
    <div className="space-y-6 animate-in slide-in-from-bottom-2 fade-in duration-300 pb-20">
      <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-800 uppercase flex items-center gap-3">
             Machine Overview 
             {activeSystem?.status === 'online' && (
                <Badge variant="success" className="text-[9px]">ONLINE - {activeSystem.ip}</Badge>
             )}
          </h2>
          <p className="text-slate-500 text-sm">Monitor local operational state and ongoing inspection tasks.</p>
        </div>
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center space-y-2 sm:space-y-0 sm:space-x-3 w-full md:w-auto">
          <Button variant="outline" className="font-mono w-full sm:w-auto">
            <AlertCircle className="w-4 h-4 mr-2 block sm:inline" /> View Active Alerts (0)
          </Button>
          <Button variant="default" className="w-full sm:w-auto">
            <Play className="w-4 h-4 mr-2 fill-current block sm:inline" /> Start Inspection
          </Button>
        </div>
      </div>

      {/* Grid of Top Stats */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="p-6">
            <div className="flex justify-between items-start">
              <div className="space-y-2">
                <p className="text-[10px] font-bold text-slate-500 uppercase">System State</p>
                <div className="flex items-center space-x-2">
                  <span className="relative flex h-2 w-2">
                    <span className={cn("absolute inline-flex h-full w-full rounded-full opacity-75", activeSystem?.status === 'online' ? "bg-green-500 animate-ping" : "bg-slate-400")}></span>
                    <span className={cn("relative inline-flex rounded-full h-2 w-2", activeSystem?.status === 'online' ? "bg-green-500" : "bg-slate-400")}></span>
                  </span>
                  <p className="text-2xl font-bold text-slate-800 tracking-widest">{activeSystem?.status === 'online' ? 'READY' : 'OFFLINE'}</p>
                </div>
              </div>
              <div className="w-10 h-10 rounded-full bg-slate-50 flex items-center justify-center">
                 <Server className="h-5 w-5 text-slate-600" />
              </div>
            </div>
          </CardContent>
        </Card>
        
        <Card>
          <CardContent className="p-6">
            <div className="flex justify-between items-start">
              <div className="space-y-2">
                <p className="text-[10px] font-bold text-slate-500 uppercase">Active Job</p>
                <p className={cn("text-2xl font-bold font-mono", activeSystem?.activeJob !== 'IDLE' ? "text-slate-800" : "text-slate-800")}>
                   {activeSystem?.activeJob || 'OFFLINE'}
                </p>
                <Badge variant="outline" className="text-slate-500 bg-slate-50">Rack A4</Badge>
              </div>
              <div className="w-10 h-10 rounded-full bg-blue-50 flex items-center justify-center">
                 <Activity className={cn("h-5 w-5", activeSystem?.activeJob !== 'IDLE' ? "text-blue-600" : "text-slate-600")} />
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            <div className="flex justify-between items-start">
              <div className="space-y-2">
                <p className="text-[10px] font-bold text-slate-500 uppercase">Images Pending Sync</p>
                <p className="text-2xl font-bold text-amber-600">428</p>
                <p className="text-[9px] text-slate-500 flex items-center uppercase">
                  <Cloud className="w-3 h-3 mr-1 text-slate-400" /> Syncing in background
                </p>
              </div>
              <div className="w-10 h-10 rounded-full bg-amber-50 flex items-center justify-center">
                <Cloud className="h-5 w-5 text-amber-600" />
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            <div className="flex justify-between items-start">
              <div className="space-y-2">
                <p className="text-[10px] font-bold text-slate-500 uppercase">Local Storage</p>
                <p className="text-2xl font-bold text-slate-800">42%</p>
                <p className="text-[9px] text-slate-500 uppercase">1.2 TB Free</p>
              </div>
              <div className="w-10 h-10 rounded-full bg-slate-50 flex items-center justify-center">
                <HardDrive className="h-5 w-5 text-slate-600" />
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-7">
        
        {/* Active Inspection Panel */}
        <Card className="lg:col-span-4">
          <CardHeader>
            <CardTitle>Active Inspection Task</CardTitle>
          </CardHeader>
          <CardContent className="pt-4">
            <div className="space-y-6">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[10px] font-bold text-slate-500 uppercase">Target</div>
                  <div className="font-mono text-lg font-bold text-slate-800">Rack B12 <span className="text-slate-500 text-sm">(4x10)</span></div>
                </div>
                <div className="text-right">
                  <div className="text-[10px] font-bold text-slate-500 uppercase">Current Position</div>
                  <div className="font-mono font-bold text-blue-600">X: 1420.5, Y: 400.0</div>
                </div>
              </div>
              
              <div className="space-y-2">
                <div className="flex items-center justify-between text-[10px] font-bold uppercase text-slate-500">
                  <span>Scan Progress</span>
                  <span className="font-mono">65% (26/40)</span>
                </div>
                <Progress value={65} className="bg-gray-200" indicatorClassName="bg-blue-600" />
              </div>

              <div className="flex items-center space-x-2 pt-4 border-t border-gray-100">
                <Button variant="destructive">
                  <Square className="w-3 h-3 mr-2 fill-current" /> Stop Job
                </Button>
                <Button variant="outline">Pause Sequence</Button>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Live Feed / System Logs */}
        <Card className="lg:col-span-3">
          <div className="px-6 py-4 border-b border-gray-100 flex flex-row items-center justify-between">
            <CardTitle>Event Log</CardTitle>
            <Badge variant="outline" className="border-blue-200 text-blue-700 bg-blue-50">LIVE</Badge>
          </div>
          <CardContent className="h-full pt-4">
            <div className="space-y-3 font-mono text-[10px] max-h-[220px] overflow-y-auto custom-scrollbar pr-2">
              {[
                { time: "10:45:02", type: "INFO", msg: "Image captured at [2,4]. Enqueued." },
                { time: "10:45:00", type: "MOVE", msg: "Position reached: X:1420.5, Y:400" },
                { time: "10:44:55", type: "MOVE", msg: "Moving to compartment [2,4]..." },
                { time: "10:44:52", type: "INFO", msg: "Image captured at [2,3]. Enqueued." },
                { time: "10:44:50", type: "MOVE", msg: "Position reached: X:1420.5, Y:300" },
                { time: "10:44:48", type: "SYNC", msg: "Cloud sync batch (#92) complete. 48 items." },
                { time: "10:44:45", type: "MOVE", msg: "Moving to compartment [2,3]..." },
              ].map((log, i) => (
                <div key={i} className="flex space-x-3 py-1 border-b border-gray-50 last:border-0 hover:bg-gray-50 transition-colors uppercase">
                  <span className="text-slate-400 w-16 shrink-0 font-medium">[{log.time}]</span>
                  <span className={cn("w-10 shrink-0 font-bold", 
                    log.type === 'MOVE' ? 'text-blue-600' : 
                    log.type === 'SYNC' ? 'text-green-600' :
                    'text-slate-500'
                  )}>
                    {log.type}:
                  </span>
                  <span className="text-slate-700 truncate">{log.msg}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

      </div>
    </div>
  );
}

