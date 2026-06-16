import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { Badge } from '../components/ui/Badge';
import { Input } from '../components/ui/Input';
import { Building2, Layers, MapPin, Box, Server, Search, CheckCircle, AlertTriangle, ChevronRight, ChevronDown, Activity, Network } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useSystem } from '../context/SystemContext';
import { hierarchyData } from '../data/mockHierarchy';

// Mock hierarchical data (imported)

const TypeIcon = ({ type, className }: { type: string, className?: string }) => {
  switch (type) {
    case 'facility': return <Building2 className={className} />;
    case 'floor': return <Layers className={className} />;
    case 'room': return <MapPin className={className} />;
    case 'rack': return <Box className={className} />;
    case 'device': return <Server className={className} />;
    default: return <Box className={className} />;
  }
};

const StatusIcon = ({ status, className }: { status: string, className?: string }) => {
  switch (status) {
    case 'ok':
    case 'online': return <CheckCircle className={cn("text-green-500", className)} />;
    case 'warning': return <AlertTriangle className={cn("text-amber-500", className)} />;
    case 'error':
    case 'offline': return <AlertTriangle className={cn("text-red-500", className)} />;
    default: return null;
  }
};

// Recursive Tree Node
const TreeNode = ({ node, level = 0, selectedNodeId, onSelectNode, parentPath = '' }: any) => {
  const [expanded, setExpanded] = useState(level < 2);
  const isSelected = selectedNodeId === node.id;
  
  const hasChildren = node.children && node.children.length > 0;
  const isDevice = node.type === 'rack' && node.device;

  const currentPath = parentPath ? `${parentPath} / ${node.name}` : node.name;

  return (
    <div className="select-none">
      <div 
        className={cn(
          "flex items-center py-1.5 px-2 rounded cursor-pointer transition-colors group",
          isSelected ? "bg-blue-600/20" : "hover:bg-white/5"
        )}
        style={{ paddingLeft: `${level * 1.5 + 0.5}rem` }}
        onClick={() => {
          if (hasChildren) setExpanded(!expanded);
          onSelectNode({ ...node, fullPath: currentPath });
        }}
      >
        <div className="w-5 h-5 flex items-center justify-center mr-1">
          {hasChildren ? (
             expanded ? <ChevronDown className="w-4 h-4 text-slate-500" /> : <ChevronRight className="w-4 h-4 text-slate-500" />
          ) : <div className="w-4 h-4" />}
        </div>
        
        <TypeIcon type={node.type} className={cn("w-4 h-4 mr-2", isSelected ? "text-blue-400" : "text-slate-500")} />
        
        <span className={cn(
          "text-sm font-medium flex-1 truncate",
          isSelected ? "text-blue-600" : "text-slate-600 group-hover:text-slate-900"
        )}>
          {node.name}
        </span>
        
        <StatusIcon status={node.status} className="w-3.5 h-3.5 ml-2" />
      </div>

      {expanded && hasChildren && (
        <div className="mt-0.5">
          {node.children.map((child: any) => (
            <TreeNode 
              key={child.id} 
              node={child} 
              level={level + 1} 
              selectedNodeId={selectedNodeId}
              onSelectNode={onSelectNode}
              parentPath={currentPath}
            />
          ))}
        </div>
      )}
      
      {/* Show Device as a child of Rack */}
      {expanded && isDevice && (
        <div 
          className={cn(
            "flex items-center py-1.5 px-2 rounded cursor-pointer transition-colors ml-4",
            selectedNodeId === node.device.id ? "bg-blue-600/20" : "hover:bg-white/5"
          )}
          style={{ paddingLeft: `${(level + 1) * 1.5 + 0.5}rem` }}
          onClick={(e) => {
             e.stopPropagation();
             onSelectNode({ ...node.device, name: node.device.id, type: 'device', parentRack: node.name, fullPath: `${currentPath} / ${node.device.id}` });
          }}
        >
          <div className="w-5 h-5 flex items-center justify-center mr-1"><div className="w-4 h-4" /></div>
          <Server className={cn("w-4 h-4 mr-2", selectedNodeId === node.device.id ? "text-blue-400" : "text-slate-500")} />
          <span className={cn(
            "text-sm font-mono flex-1 truncate",
            selectedNodeId === node.device.id ? "text-blue-400" : "text-slate-300"
          )}>
            {node.device.id}
          </span>
          <StatusIcon status={node.device.status} className="w-3.5 h-3.5 ml-2" />
        </div>
      )}
    </div>
  );
};

export default function FleetManager() {
  const [selectedNode, setSelectedNode] = useState<any | null>(null);
  const [activeTab, setActiveTab] = useState('overview');
  const { setActiveSystem } = useSystem();
  const navigate = useNavigate();

  const handleConnect = () => {
    let targetDevice = null;
    let targetPath = '';

    if (selectedNode?.type === 'device') {
      targetDevice = selectedNode;
      const pathParts = selectedNode.fullPath.split(' / ');
      pathParts.pop(); // remove device name
      pathParts.pop(); // remove rack name
      targetPath = pathParts.join(' / ');
    } else if (selectedNode?.type === 'rack' && selectedNode.device) {
      targetDevice = selectedNode.device;
      const pathParts = selectedNode.fullPath.split(' / ');
      pathParts.pop(); // remove rack name
      targetPath = pathParts.join(' / ');
    }

    if (targetDevice) {
      setActiveSystem({
        id: targetDevice.id || targetDevice.name,
        name: targetDevice.id || targetDevice.name,
        status: targetDevice.status,
        ip: targetDevice.ip,
        parentPath: targetPath,
        activeJob: targetDevice.activeJob
      });
      navigate('/');
    }
  };

  const getBreadcrumbs = (fullPath: string) => {
     if (!fullPath) return [];
     return fullPath.split(' / ');
  };

  return (
    <div className="space-y-6 animate-in slide-in-from-bottom-2 fade-in duration-300 h-[calc(100vh-6rem)] flex flex-col">
      <div className="flex items-center justify-between shrink-0 flex-col md:flex-row gap-4 items-start md:items-center">
        <div>
          <h2 className="text-2xl font-bold tracking-tight text-slate-800 uppercase">Device Network & Fleet</h2>
          <p className="text-slate-500 text-sm">Manage multiple interconnected inspection systems across facilities.</p>
        </div>
        <div className="flex space-x-2 w-full md:w-auto">
          <div className="relative w-full md:w-auto">
             <Search className="w-4 h-4 text-slate-500 absolute left-3 top-1/2 transform -translate-y-1/2" />
             <Input className="pl-9 w-full md:w-64 bg-white border-gray-200" placeholder="Search devices, racks..." />
          </div>
        </div>
      </div>

      <div className="flex-1 flex flex-col lg:grid lg:grid-cols-12 gap-6 min-h-0 overflow-y-auto lg:overflow-hidden pb-6 lg:pb-0">
        {/* Topology Tree */}
        <Card className="lg:col-span-4 flex flex-col h-[350px] lg:h-full overflow-hidden border-gray-200 bg-white shadow-sm shrink-0">
          <CardHeader className="border-b border-gray-100 py-3 bg-gray-50/50">
            <CardTitle className="text-xs">Topology Organization</CardTitle>
          </CardHeader>
          <CardContent className="flex-1 overflow-y-auto p-4 custom-scrollbar">
             {hierarchyData.map(node => (
               <TreeNode 
                 key={node.id} 
                 node={node} 
                 selectedNodeId={selectedNode?.id}
                 onSelectNode={setSelectedNode} 
               />
             ))}
          </CardContent>
        </Card>

        {/* Details Panel */}
        <div className="flex-1 lg:col-span-8 flex flex-col min-h-[500px] lg:min-h-0">
          {selectedNode ? (
            <Card className="flex-1 flex flex-col border-gray-200 bg-transparent shadow-none overflow-hidden">
              <CardHeader className="border-b border-gray-200 bg-gray-50 overflow-hidden">
                <div className="flex items-center text-[10px] uppercase font-bold text-slate-500 mb-2 truncate">
                   {getBreadcrumbs(selectedNode.fullPath).map((b, i, arr) => (
                      <React.Fragment key={i}>
                        <span className={i === arr.length - 1 ? "text-slate-300" : ""}>{b}</span>
                        {i < arr.length - 1 && <ChevronRight className="w-3 h-3 mx-1" />}
                      </React.Fragment>
                   ))}
                </div>
                <div className="flex items-center justify-between">
                  <CardTitle className="text-xl flex items-center">
                    <TypeIcon type={selectedNode.type} className="w-6 h-6 mr-3 text-slate-400" />
                    {selectedNode.type === 'device' ? selectedNode.name : selectedNode.name}
                  </CardTitle>
                  <Badge variant={selectedNode.status === 'ok' || selectedNode.status === 'online' ? 'success' : selectedNode.status === 'offline' ? 'outline' : 'warning'}>
                     {selectedNode.status.toUpperCase()}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent className="flex-1 p-6 overflow-y-auto">
                {selectedNode.type === 'device' || (selectedNode.type === 'rack' && selectedNode.device) ? (
                  <div className="flex flex-col h-full">
                    <div className="flex gap-2 mb-4 shrink-0 overflow-x-auto custom-scrollbar pb-1">
                      <button 
                        onClick={() => setActiveTab('overview')}
                        className={cn("text-xs uppercase px-4 py-2 rounded font-bold transition-colors whitespace-nowrap", activeTab === 'overview' ? "bg-blue-50 text-blue-600 border border-blue-200" : "bg-white text-slate-600 border border-gray-200 hover:bg-gray-50")}
                      >
                        Overview
                      </button>
                      <button 
                        onClick={() => setActiveTab('hardware')}
                        className={cn("text-xs uppercase px-4 py-2 rounded font-bold transition-colors whitespace-nowrap", activeTab === 'hardware' ? "bg-blue-50 text-blue-600 border border-blue-200" : "bg-white text-slate-600 border border-gray-200 hover:bg-gray-50")}
                      >
                        Hardware Config
                      </button>
                    </div>

                    {activeTab === 'overview' && (
                      <div className="flex-1 space-y-6 mt-0">
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                          <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
                            <div className="text-[10px] font-bold text-slate-500 uppercase mb-1">IP Address</div>
                            <div className="font-mono text-sm text-slate-800">{selectedNode.type === 'rack' ? selectedNode.device.ip : selectedNode.ip}</div>
                          </div>
                          <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
                            <div className="text-[10px] font-bold text-slate-500 uppercase mb-1">Active Job</div>
                            <div className="font-mono text-sm text-blue-600">{selectedNode.type === 'rack' ? selectedNode.device.activeJob : selectedNode.activeJob}</div>
                          </div>
                          <div className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
                            <div className="text-[10px] font-bold text-slate-500 uppercase mb-1">Last Sync</div>
                            <div className="font-mono text-sm text-slate-600">2 mins ago</div>
                          </div>
                        </div>

                        <div className="flex flex-col xl:flex-row gap-3">
                           <Button variant="default" onClick={handleConnect}>Connect & Control</Button>
                           <Button variant="outline">View Event Logs</Button>
                           <Button variant="outline">Run Diagnostics</Button>
                        </div>
                        
                        <Card className="border-gray-200 bg-white mt-6 shadow-sm">
                          <CardHeader className="py-3 px-4 border-b border-gray-100 bg-gray-50">
                            <CardTitle className="text-xs">Live Telemetry</CardTitle>
                          </CardHeader>
                          <CardContent className="p-4">
                            <div className="space-y-4">
                              <div>
                                <div className="flex justify-between text-[10px] font-bold uppercase text-slate-500 mb-1">
                                  <span>CPU Usage</span><span>42%</span>
                                </div>
                                <div className="h-1 w-full bg-white/5 rounded overflow-hidden">
                                  <div className="h-full bg-blue-500 w-[42%]" />
                                </div>
                              </div>
                              <div>
                                <div className="flex justify-between text-[10px] font-bold uppercase text-slate-500 mb-1">
                                  <span>Local Storage</span><span>840 GB / 2 TB</span>
                                </div>
                                <div className="h-1 w-full bg-white/5 rounded overflow-hidden">
                                  <div className="h-full bg-blue-500 w-[40%]" />
                                </div>
                              </div>
                            </div>
                          </CardContent>
                        </Card>
                      </div>
                    )}

                    {activeTab === 'hardware' && (
                      <div className="flex-1 mt-0">
                        <Card className="border-gray-200 bg-white">
                          <CardHeader className="border-b border-gray-100 bg-gray-50">
                            <CardTitle className="text-sm">Hardware Settings for {selectedNode.name}</CardTitle>
                          </CardHeader>
                          <CardContent className="p-6 space-y-6">
                            <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
                              <div className="space-y-3 border border-gray-200 bg-white p-4 rounded-lg shadow-sm">
                                 <div className="flex items-center justify-between">
                                    <div className="text-sm font-bold text-slate-800">Camera Motion Link</div>
                                    <div className="w-10 h-5 bg-blue-600 rounded-full flex items-center p-1 cursor-pointer">
                                       <div className="w-3 h-3 bg-white rounded-full translate-x-5" />
                                    </div>
                                 </div>
                                 <p className="text-[10px] text-slate-500">Syncs camera lens motion with mechanical gantry pathing.</p>
                              </div>

                              <div className="space-y-3 border border-gray-200 bg-white p-4 rounded-lg shadow-sm">
                                 <div className="text-sm font-bold text-slate-800 mb-2">Hardware Control Mode</div>
                                 <select className="w-full bg-white border border-gray-200 rounded px-3 py-2 text-sm text-slate-700 focus:outline-none focus:border-blue-500">
                                   <option>Fully Automated</option>
                                   <option>Semi-Automated (Verify)</option>
                                   <option>Manual Telemetry Override</option>
                                 </select>
                              </div>

                              <div className="space-y-3 border border-gray-200 bg-white p-4 rounded-lg shadow-sm">
                                 <div className="text-sm font-bold text-slate-800 mb-2">Automated Check Frequency</div>
                                 <select className="w-full bg-white border border-gray-200 rounded px-3 py-2 text-sm text-slate-700 focus:outline-none focus:border-blue-500">
                                   <option>Every 2 Hours</option>
                                   <option>Every 6 Hours</option>
                                   <option>Every 12 Hours</option>
                                   <option>Every 24 Hours</option>
                                   <option>Manual Trigger Only</option>
                                 </select>
                                 <p className="text-[10px] text-slate-500 mt-2">Frequency of whole-rack inspection scans.</p>
                              </div>

                              <div className="space-y-3 border border-gray-200 bg-white p-4 rounded-lg shadow-sm">
                                 <div className="text-sm font-bold text-slate-800 mb-2">LED Lighting Behavior</div>
                                 <select className="w-full bg-white border border-gray-200 rounded px-3 py-2 text-sm text-slate-700 focus:outline-none focus:border-blue-500">
                                   <option>Activate on Scan Only</option>
                                   <option>Always On</option>
                                   <option>Synchronize with Ambient Light</option>
                                 </select>
                              </div>
                            </div>
                            
                            <div className="flex justify-end pt-4 border-t border-gray-100">
                               <Button variant="default">Save Hardware Config</Button>
                            </div>
                          </CardContent>
                        </Card>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="space-y-6 flex flex-col items-center justify-center h-full opacity-50">
                     <Layers className="w-16 h-16 text-slate-600 mb-4" />
                     <p className="text-slate-400">Select a rack or device to connect and control the device-specific telemetry and options.</p>
                     <div className="text-[10px] uppercase font-bold text-slate-500">
                       Currently viewing aggregate status for {selectedNode.type}
                     </div>
                  </div>
                )}
              </CardContent>
            </Card>
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center border border-gray-200 border-dashed rounded-lg bg-gray-50 py-12 lg:py-0">
               <Network className="w-12 h-12 text-slate-600 mb-4" />
               <p className="text-slate-400">Select a node from the topology tree to view details.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
