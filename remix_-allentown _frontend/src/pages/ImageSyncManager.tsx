import React from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Progress } from '../components/ui/Progress';
import { CloudOff, RefreshCw, HardDrive, Wifi, Image as ImageIcon } from 'lucide-react';

export default function ImageSyncManager() {
  return (
    <div className="space-y-6 animate-in slide-in-from-bottom-2 fade-in duration-300">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-800 uppercase">Image & Sync Manager</h2>
          <p className="text-slate-500 text-sm">Manage local image storage and cloud synchronization queues.</p>
        </div>
        <div className="flex flex-col sm:flex-row space-y-2 sm:space-y-0 sm:space-x-2 w-full sm:w-auto">
          <Button variant="outline" className="w-full sm:w-auto">
            <CloudOff className="w-4 h-4 mr-2" /> Force Offline Mode
          </Button>
          <Button variant="default" className="w-full sm:w-auto">
            <RefreshCw className="w-4 h-4 mr-2" /> Force Sync Now
          </Button>
        </div>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        <Card>
          <CardContent className="p-6">
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-[10px] font-bold text-slate-500 uppercase">Local Storage Allocation</h3>
                <HardDrive className="w-5 h-5 text-slate-500" />
              </div>
              <Progress value={42} />
              <div className="flex justify-between text-[10px] font-mono uppercase">
                <span className="text-slate-500">Used: 840 GB</span>
                <span className="text-slate-800 font-bold">Free: 1.16 TB</span>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-[10px] font-bold text-slate-500 uppercase">Sync Queue</h3>
                <RefreshCw className="w-5 h-5 text-slate-500" />
              </div>
              <div className="text-3xl font-bold font-mono text-amber-500">428</div>
              <div className="text-[10px] text-slate-500 uppercase">Images pending upload to cloud</div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-6">
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-[10px] font-bold text-slate-500 uppercase">Cloud Connectivity</h3>
                <Wifi className="w-5 h-5 text-green-500" />
              </div>
              <div className="text-lg font-bold text-green-500">Connected</div>
              <div className="text-[10px] font-mono text-slate-500 uppercase">Latency: 45ms</div>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent Image Captures</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="border border-gray-200 rounded overflow-x-auto">
            <table className="w-full text-xs text-left min-w-[600px]">
              <thead className="bg-white text-slate-500 font-bold border-b border-gray-200 uppercase tracking-wider">
                <tr>
                  <th className="px-4 py-3">Timestamp</th>
                  <th className="px-4 py-3">Job ID</th>
                  <th className="px-4 py-3">Rack ID</th>
                  <th className="px-4 py-3">Loc [X,Y]</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Preview</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-panel font-mono text-[10px]">
                {[
                  { time: '2026-04-29 10:45:02', job: 'JOB-0442', rack: 'RCK-A4', loc: '[2,4]', status: 'queued' },
                  { time: '2026-04-29 10:44:52', job: 'JOB-0442', rack: 'RCK-A4', loc: '[2,3]', status: 'queued' },
                  { time: '2026-04-29 10:40:15', job: 'JOB-0442', rack: 'RCK-A4', loc: '[2,2]', status: 'synced' },
                  { time: '2026-04-29 10:39:40', job: 'JOB-0442', rack: 'RCK-A4', loc: '[2,1]', status: 'synced' },
                  { time: '2026-04-29 09:15:12', job: 'JOB-0441', rack: 'RCK-B1', loc: '[1,10]', status: 'synced' },
                ].map((row, i) => (
                  <tr key={i} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3 text-slate-500">{row.time}</td>
                    <td className="px-4 py-3 font-bold text-slate-800">{row.job}</td>
                    <td className="px-4 py-3 text-slate-600">{row.rack}</td>
                    <td className="px-4 py-3 text-blue-400">{row.loc}</td>
                    <td className="px-4 py-3">
                      {row.status === 'synced' 
                        ? <Badge variant="success">Synced</Badge>
                        : <Badge variant="warning">Queued</Badge>
                      }
                    </td>
                    <td className="px-4 py-3">
                      <Button variant="ghost" size="sm" className="h-6 px-1">
                        <ImageIcon className="w-4 h-4 text-slate-500" />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
