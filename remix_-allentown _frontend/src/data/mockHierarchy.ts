export const hierarchyData = [
  {
    id: 'fac-1',
    name: 'Allentown Main HQ',
    type: 'facility',
    status: 'ok',
    children: [
      {
        id: 'fl-1',
        name: 'Floor 1 (Lab)',
        type: 'floor',
        status: 'warning',
        children: [
          {
            id: 'rm-101',
            name: 'Room 101 - Observation',
            type: 'room',
            status: 'ok',
            children: [
              {
                id: 'rck-a1',
                name: 'Rack A1',
                type: 'rack',
                status: 'ok',
                device: { id: 'EDGE-WS-001', status: 'online', ip: '10.0.1.51', activeJob: 'JOB-0442' }
              },
              {
                id: 'rck-a2',
                name: 'Rack A2',
                type: 'rack',
                status: 'ok',
                device: { id: 'EDGE-WS-002', status: 'online', ip: '10.0.1.52', activeJob: 'IDLE' }
              }
            ]
          },
          {
            id: 'rm-102',
            name: 'Room 102 - Breeding',
            type: 'room',
            status: 'warning',
            children: [
              {
                id: 'rck-b1',
                name: 'Rack B1',
                type: 'rack',
                status: 'warning',
                device: { id: 'EDGE-WS-003', status: 'warning', ip: '10.0.1.53', activeJob: 'CALIBRATION' }
              }
            ]
          }
        ]
      },
      {
        id: 'fl-2',
        name: 'Floor 2 (Research)',
        type: 'floor',
        status: 'ok',
        children: [
          {
            id: 'rm-205',
            name: 'Room 205',
            type: 'room',
            status: 'ok',
            children: [
              {
                id: 'rck-c1',
                name: 'Rack C1',
                type: 'rack',
                status: 'ok',
                device: { id: 'EDGE-WS-012', status: 'offline', ip: '10.0.1.62', activeJob: 'OFFLINE' }
              }
            ]
          }
        ]
      }
    ]
  },
  {
    id: 'fac-2',
    name: 'Westside Facility',
    type: 'facility',
    status: 'ok',
    children: [
      {
        id: 'fl-1-w',
        name: 'Ground Floor',
        type: 'floor',
        status: 'ok',
        children: []
      }
    ]
  }
];

export const getAllDevices = () => {
   const devices: any[] = [];
   const traverse = (node: any, currentPath: string[]) => {
      const path = [...currentPath, node.name];
      if (node.type === 'rack' && node.device) {
         devices.push({
            id: node.device.id,
            name: node.device.id,
            status: node.device.status,
            ip: node.device.ip,
            activeJob: node.device.activeJob,
            parentPath: currentPath.join(' / '),
            rackName: node.name
         });
      }
      if (node.children) {
         node.children.forEach((child: any) => traverse(child, path));
      }
   };
   
   hierarchyData.forEach(node => traverse(node, []));
   return devices;
};
