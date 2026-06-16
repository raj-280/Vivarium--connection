/**
 * src/components/GantryGrid.tsx
 *
 * Section 7 — 12×7 cell grid (or whatever RACK_ROWS/RACK_COLS are set to).
 * Clicking a cell sends M700 R{row} C{col} via sendCommand().
 *
 * Cell colour states:
 *   - active    → blue ring (gantry currently positioned here)
 *   - captured  → green fill
 *   - failed    → red fill
 *   - default   → neutral, hoverable
 *
 * The grid is read-only for viewers (server enforces, but we also dim it).
 */

import React, { useCallback, useMemo } from 'react';
import { cn } from '@/lib/utils';
import { useSystem } from '../context/SystemContext';
import type { GridCell } from '../types/gantry.types';
import appConfig from '../config/app.config';

interface GantryGridProps {
  rackId?: string;
  /** Allow the parent to set the "active" cell independently (e.g. from M700 response) */
  activeRow?: number | null;
  activeCol?: number | null;
  className?: string;
}

export function GantryGrid({ rackId, activeRow, activeCol, className }: GantryGridProps) {
  const { sendCommand, activeRackId, userRole, gridCells, gantryPosition, rackLayout } = useSystem();

  const target = rackId ?? activeRackId;
  const isViewer = userRole === 'viewer';

  // Compute the gantry's current cell from its X/Y position + rack geometry.
  // This is the same algorithm as the sample code's highlightRackFromPosition().
  const positionActiveCell = useMemo(() => {
    if (
      !rackLayout ||
      gantryPosition.x === null ||
      gantryPosition.y === null
    ) return null;

    const rowPos = (gantryPosition.y - rackLayout.offset_y) / rackLayout.pitch_y;
    const colPos = (gantryPosition.x - rackLayout.offset_x) / rackLayout.pitch_x;
    const row = Math.round(rowPos);
    const col  = Math.round(colPos);

    // Tolerance check — 3mm default matches POSITION_TOLERANCE_X_MM
    const TOL_MM = 3.0;
    const rowErr = Math.abs(rowPos - row) * rackLayout.pitch_y;
    const colErr = Math.abs(colPos - col) * rackLayout.pitch_x;

    if (row < 0 || row >= rackLayout.rows) return null;
    if (col < 0 || col >= rackLayout.columns) return null;
    if (rowErr > TOL_MM || colErr > TOL_MM) return null;

    return { row, col };
  }, [gantryPosition, rackLayout]);


  // Build a lookup map for O(1) cell-state access
  const cellMap = useMemo(() => {
    const m = new Map<string, GridCell>();
    for (const cell of gridCells) {
      m.set(`${cell.row},${cell.col}`, cell);
    }
    return m;
  }, [gridCells]);

  const handleCellClick = useCallback(
    (row: number, col: number) => {
      if (!target || isViewer) return;
      // M700 Rn Cn — 0-indexed rows/cols matching the server's grid validation
      sendCommand(`M700 R${row} C${col}`, target);
    },
    [target, isViewer, sendCommand],
  );

  // Use live layout dimensions when available, fall back to config defaults
  const rows = rackLayout?.rows ?? appConfig.rackRows;
  const cols = rackLayout?.columns ?? appConfig.rackCols;

  return (
    <div
      id="gantry-grid"
      className={cn('select-none', className)}
      aria-label={`Gantry grid ${rows}×${cols}`}
    >
      {/* Column headers */}
      <div
        className="grid gap-0.5 mb-0.5"
        style={{ gridTemplateColumns: `20px repeat(${cols}, 1fr)` }}
      >
        <div /> {/* corner spacer */}
        {Array.from({ length: cols }, (_, c) => (
          <div
            key={c}
            className="text-center text-[9px] font-mono text-slate-400 uppercase"
          >
            C{c}
          </div>
        ))}
      </div>

      {/* Rows */}
      {Array.from({ length: rows }, (_, r) => (
        <div
          key={r}
          className="grid gap-0.5 mb-0.5"
          style={{ gridTemplateColumns: `20px repeat(${cols}, 1fr)` }}
        >
          {/* Row label */}
          <div className="flex items-center justify-end pr-1 text-[9px] font-mono text-slate-400 uppercase">
            R{r}
          </div>

          {/* Cells */}
          {Array.from({ length: cols }, (_, c) => {
            const cell = cellMap.get(`${r},${c}`);

            // Cell is "active" if:
            //   1. The parent passes explicit activeRow/activeCol props, OR
            //   2. The gantry position maps to this cell (positionActiveCell)
            const isActiveByProps =
              (activeRow !== undefined && activeRow !== null ? activeRow === r : false) &&
              (activeCol !== undefined && activeCol !== null ? activeCol === c : false);
            const isActiveByPosition =
              positionActiveCell?.row === r && positionActiveCell?.col === c;
            const isActive = isActiveByProps || isActiveByPosition;

            return (
              <button
                key={c}
                id={`grid-cell-r${r}-c${c}`}
                type="button"
                aria-label={`Row ${r} Col ${c}`}
                onClick={() => handleCellClick(r, c)}
                disabled={!target || isViewer}
                className={cn(
                  'aspect-square rounded-sm border transition-all duration-100 focus:outline-none focus:ring-1 focus:ring-blue-400',
                  // Base
                  'text-[8px] font-mono',
                  // States
                  isActive
                    ? 'bg-blue-500 border-blue-400 ring-2 ring-blue-300 shadow-md'
                    : cell?.captured === true
                    ? 'bg-green-500/20 border-green-500/40 hover:bg-green-500/30'
                    : cell?.captured === false
                    ? 'bg-red-500/20 border-red-500/40 hover:bg-red-500/30'
                    : 'bg-slate-100 border-slate-200 hover:bg-blue-50 hover:border-blue-300',
                  // Disabled / viewer
                  (isViewer || !target) &&
                    'cursor-default opacity-60 hover:bg-slate-100 hover:border-slate-200',
                )}
              />
            );
          })}
        </div>
      ))}

      {/* Legend */}
      <div className="flex items-center gap-3 mt-2 text-[9px] font-mono text-slate-400 uppercase">
        <span className="flex items-center gap-1">
          <span className="w-3 h-3 rounded-sm bg-blue-500 inline-block" /> Active
        </span>
        <span className="flex items-center gap-1">
          <span className="w-3 h-3 rounded-sm bg-green-500/40 border border-green-500/50 inline-block" /> Captured
        </span>
        <span className="flex items-center gap-1">
          <span className="w-3 h-3 rounded-sm bg-slate-100 border border-slate-200 inline-block" /> Pending
        </span>
      </div>
    </div>
  );
}
