import React from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Activity, AlertTriangle, CheckCircle2, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

export default function AnalyticsPreview() {
  return (
    <div className="space-y-6 animate-in slide-in-from-bottom-2 fade-in duration-300">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-800 uppercase">Analytics Preview</h2>
          <p className="text-slate-500 text-sm">Locally view inspection outcomes returned from downstream cloud analytics.</p>
        </div>
        <Button variant="outline">
          <Activity className="w-4 h-4 mr-2" /> Request Re-analysis
        </Button>
      </div>

      <div className="grid gap-6 md:grid-cols-4">
        <Card>
          <CardContent className="p-6">
            <h3 className="text-[10px] font-bold text-slate-500 uppercase mb-2">Total Cages Analyzed</h3>
            <p className="text-3xl font-bold text-slate-800">1,248</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <h3 className="text-[10px] font-bold text-slate-500 uppercase mb-2">Anomalies Detected</h3>
            <p className="text-3xl font-bold text-amber-500">14</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <h3 className="text-[10px] font-bold text-slate-500 uppercase mb-2">Empty Compartments</h3>
            <p className="text-3xl font-bold text-slate-600">42</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-6">
            <h3 className="text-[10px] font-bold text-slate-500 uppercase mb-2">Analysis Latency Avg</h3>
            <p className="text-3xl font-bold font-mono text-slate-600">1.2s</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent Findings Feed</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {[
              { time: '10 mins ago', rack: 'RCK-A4', loc: '[2,4]', type: 'anomaly', desc: 'Water bottle level below 10%', status: 'Flagged' },
              { time: '12 mins ago', rack: 'RCK-A4', loc: '[2,2]', type: 'empty', desc: 'No occupancy detected', status: 'Logged' },
              { time: '1 hour ago', rack: 'RCK-B1', loc: '[1,1]', type: 'anomaly', desc: 'Multiple occupants detected (expected 1)', status: 'Review Required' },
            ].map((finding, i) => (
              <div key={i} className="flex items-start justify-between p-4 border border-gray-200 rounded bg-white hover:bg-gray-50 transition-colors cursor-pointer">
                <div className="flex items-start space-x-4">
                  <div className={cn("mt-1 flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center border", 
                    finding.type === 'anomaly' ? 'bg-amber-500/10 text-amber-500 border-amber-500/30' : 'bg-gray-50 text-slate-500 border-gray-200'
                  )}>
                    {finding.type === 'anomaly' ? <AlertTriangle className="w-4 h-4" /> : <CheckCircle2 className="w-4 h-4" />}
                  </div>
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="font-bold text-slate-800 uppercase">{finding.rack}</span>
                      <span className="font-mono text-[10px] text-slate-500 bg-gray-50 border border-gray-200 px-1.5 py-0.5 rounded uppercase">Comp {finding.loc}</span>
                    </div>
                    <p className="text-sm text-slate-600 mt-1">{finding.desc}</p>
                    <p className="text-[10px] text-slate-500 mt-1 uppercase">{finding.time}</p>
                  </div>
                </div>
                <div className="flex items-center space-x-3">
                  <Badge variant={finding.type === 'anomaly' ? 'warning' : 'outline'}>
                    {finding.status}
                  </Badge>
                  <ChevronRight className="w-4 h-4 text-slate-500" />
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
