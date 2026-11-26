"use client";

import { Maximize2, Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export type DotGridTone = "neutral" | "accent" | "ok" | "risk" | "late";

export interface DotGridNode {
  id: string;
  x: number;
  y: number;
  width?: number;
  height?: number;
  kindLabel: string;
  title: string;
  description?: string;
  badge?: string;
  tone?: DotGridTone;
  icon?: ReactNode;
}

export interface DotGridEdge {
  id: string;
  from: string;
  to: string;
  label?: string;
  active?: boolean;
}

export interface DotGridCanvasProps {
  nodes: DotGridNode[];
  edges: DotGridEdge[];
  selectedId?: string | null;
  onSelectNode?: (id: string) => void;
  height?: number;
  emptyMessage?: string;
  ariaLabel?: string;
  className?: string;
}

const DEFAULT_NODE_WIDTH = 236;
const DEFAULT_NODE_HEIGHT = 104;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 2.0;
const ZOOM_STEP = 0.18;
const GRID_SPACING = 22;

const TONE_ACCENT: Record<DotGridTone, string> = {
  neutral: "text-muted bg-surface-2/80 border-border",
  accent: "text-fg bg-surface-2 border-border-strong",
  ok: "text-ok bg-surface-2/80 border-border",
  risk: "text-risk bg-surface-2/80 border-border",
  late: "text-late bg-surface-2/80 border-border",
};

const TONE_LABEL: Record<DotGridTone, string> = {
  neutral: "text-faint",
  accent: "text-accent-text",
  ok: "text-ok",
  risk: "text-risk",
  late: "text-late",
};

const TONE_EDGE_DOT: Record<DotGridTone, string> = {
  neutral: "border-border-strong bg-raised",
  accent: "border-accent/60 bg-surface",
  ok: "border-ok/60 bg-surface",
  risk: "border-risk/60 bg-surface",
  late: "border-late/60 bg-surface",
};

function clampZoom(z: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(z.toFixed(3))));
}

/** Pure: compute pan & zoom to fit every node cleanly inside a viewport box. */
export function computeFitViewport(
  nodes: DotGridNode[],
  viewportW: number,
  viewportH: number,
  padding = 48,
): { x: number; y: number; zoom: number } {
  if (nodes.length === 0) return { x: 0, y: 0, zoom: 1 };
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const n of nodes) {
    const w = n.width ?? DEFAULT_NODE_WIDTH;
    const h = n.height ?? DEFAULT_NODE_HEIGHT;
    if (n.x < minX) minX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.x + w > maxX) maxX = n.x + w;
    if (n.y + h > maxY) maxY = n.y + h;
  }
  const contentW = Math.max(1, maxX - minX);
  const contentH = Math.max(1, maxY - minY);
  const availW = Math.max(120, viewportW - padding * 2);
  const availH = Math.max(120, viewportH - padding * 2);
  const zoom = clampZoom(Math.min(1, availW / contentW, availH / contentH));
  const x = Math.round((viewportW - contentW * zoom) / 2 - minX * zoom);
  const y = Math.round((viewportH - contentH * zoom) / 2 - minY * zoom);
  return { x, y, zoom };
}

/** Pure: cubic Bezier SVG path and midpoint between two node connection ports. */
export function edgeCurve(from: DotGridNode, to: DotGridNode): { path: string; midX: number; midY: number } {
  const fw = from.width ?? DEFAULT_NODE_WIDTH;
  const fh = from.height ?? DEFAULT_NODE_HEIGHT;
  const tw = to.width ?? DEFAULT_NODE_WIDTH;
  const th = to.height ?? DEFAULT_NODE_HEIGHT;
  const forward = to.x + tw / 2 >= from.x + fw / 2;
  const x1 = forward ? from.x + fw : from.x;
  const y1 = from.y + fh / 2;
  const x2 = forward ? to.x : to.x + tw;
  const y2 = to.y + th / 2;
  const dx = Math.max(48, Math.abs(x2 - x1) * 0.45) * (forward ? 1 : -1);
  const cx1 = x1 + dx;
  const cy1 = y1;
  const cx2 = x2 - dx;
  const cy2 = y2;
  // Midpoint at t = 0.5 on cubic Bezier
  const midX = 0.125 * (x1 + 3 * cx1 + 3 * cx2 + x2);
  const midY = 0.125 * (y1 + 3 * cy1 + 3 * cy2 + y2);
  return {
    path: `M ${x1} ${y1} C ${cx1} ${cy1}, ${cx2} ${cy2}, ${x2} ${y2}`,
    midX,
    midY,
  };
}

/** Interactive dot-grid node canvas with pointer pan, wheel/button zoom, Bezier links, and a liquid glass dock. */
export function DotGridCanvas({
  nodes,
  edges,
  selectedId = null,
  onSelectNode,
  height = 480,
  emptyMessage = "No items mapped yet.",
  ariaLabel = "Interactive diagram canvas",
  className,
}: DotGridCanvasProps) {
  const uid = useId().replace(/:/g, "");
  const patternId = `${uid}-dot-grid`;
  const containerRef = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState<{ x: number; y: number; zoom: number }>(() =>
    computeFitViewport(nodes, 840, height),
  );
  const [panning, setPanning] = useState(false);
  const dragRef = useRef<{ pointerId: number; startX: number; startY: number; originX: number; originY: number } | null>(null);
  const userMovedRef = useRef(false);

  const nodeMap = useMemo(() => {
    const map = new Map<string, DotGridNode>();
    for (const n of nodes) map.set(n.id, n);
    return map;
  }, [nodes]);

  const portUsage = useMemo(() => {
    const incoming = new Set<string>();
    const outgoing = new Set<string>();
    for (const e of edges) {
      outgoing.add(e.from);
      incoming.add(e.to);
    }
    return { incoming, outgoing };
  }, [edges]);

  const fitView = useCallback(() => {
    const rect = containerRef.current?.getBoundingClientRect();
    const w = rect && rect.width > 0 ? rect.width : 840;
    const h = rect && rect.height > 0 ? rect.height : height;
    setViewport(computeFitViewport(nodes, w, h));
  }, [nodes, height]);

  const layoutKey = useMemo(() => nodes.map((n) => `${n.id}:${n.x}:${n.y}`).join("|"), [nodes]);
  useEffect(() => {
    if (!userMovedRef.current) {
      fitView();
    }
  }, [layoutKey, fitView]);

  const zoomBy = useCallback((delta: number, focal?: { x: number; y: number }) => {
    userMovedRef.current = true;
    setViewport((prev) => {
      const nextZoom = clampZoom(prev.zoom + delta);
      if (nextZoom === prev.zoom) return prev;
      const rect = containerRef.current?.getBoundingClientRect();
      const cx = focal ? focal.x : (rect && rect.width > 0 ? rect.width / 2 : 420);
      const cy = focal ? focal.y : (rect && rect.height > 0 ? rect.height / 2 : height / 2);
      const worldX = (cx - prev.x) / prev.zoom;
      const worldY = (cy - prev.y) / prev.zoom;
      return {
        zoom: nextZoom,
        x: Math.round(cx - worldX * nextZoom),
        y: Math.round(cy - worldY * nextZoom),
      };
    });
  }, [height]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement | null;
    if (target?.closest("[data-dot-node]") || target?.closest("[data-dot-controls]")) return;
    userMovedRef.current = true;
    setPanning(true);
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      originX: viewport.x,
      originY: viewport.y,
    };
    if (typeof e.currentTarget.setPointerCapture === "function") {
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        // Synthetic pointer events in test environments may not have an active pointer id.
      }
    }
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    setViewport((prev) => ({ ...prev, x: drag.originX + dx, y: drag.originY + dy }));
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    dragRef.current = null;
    setPanning(false);
    if (typeof e.currentTarget.releasePointerCapture === "function") {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        // Ignore if capture was not set.
      }
    }
  };

  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const rect = containerRef.current?.getBoundingClientRect();
    const focal = rect ? { x: e.clientX - rect.left, y: e.clientY - rect.top } : undefined;
    zoomBy(e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP, focal);
  };

  const gridStep = GRID_SPACING * viewport.zoom;
  const gridOffsetX = ((viewport.x % gridStep) + gridStep) % gridStep;
  const gridOffsetY = ((viewport.y % gridStep) + gridStep) % gridStep;
  const dotRadius = Math.max(0.9, Math.min(1.8, 1.2 * viewport.zoom));

  const renderedEdges = useMemo(() => {
    return edges.flatMap((edge) => {
      const from = nodeMap.get(edge.from);
      const to = nodeMap.get(edge.to);
      if (!from || !to) return [];
      const active = Boolean(edge.active) || selectedId === edge.from || selectedId === edge.to;
      return [{ edge, active, ...edgeCurve(from, to) }];
    });
  }, [edges, nodeMap, selectedId]);

  return (
    <div
      ref={containerRef}
      role="region"
      aria-label={ariaLabel}
      data-testid="dot-grid-canvas"
      data-zoom={viewport.zoom.toFixed(2)}
      data-pan-x={Math.round(viewport.x)}
      data-pan-y={Math.round(viewport.y)}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onWheel={onWheel}
      style={{ height }}
      className={cn(
        "relative w-full overflow-hidden rounded-xl border border-border bg-canvas select-none touch-none",
        panning ? "cursor-grabbing" : "cursor-grab",
        className,
      )}
    >
      {/* Inline SVG dot-matrix pattern background */}
      <svg
        aria-hidden
        data-testid="dot-grid-pattern-svg"
        className="pointer-events-none absolute inset-0 h-full w-full"
      >
        <defs>
          <pattern
            id={patternId}
            x={gridOffsetX}
            y={gridOffsetY}
            width={gridStep}
            height={gridStep}
            patternUnits="userSpaceOnUse"
          >
            <circle cx={dotRadius + 0.5} cy={dotRadius + 0.5} r={dotRadius} fill="var(--fg-faint)" fillOpacity={0.34} />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill={`url(#${patternId})`} />
      </svg>

      {nodes.length === 0 ? (
        <div className="relative z-10 flex h-full w-full items-center justify-center p-6 text-center">
          <div className="rounded-lg border border-border bg-surface/95 px-4 py-3 text-[13px] text-muted shadow-sm">
            {emptyMessage}
          </div>
        </div>
      ) : (
        <>
          {/* Transformed SVG edge layer */}
          <svg
            aria-hidden
            data-testid="dot-grid-edges"
            className="pointer-events-none absolute inset-0 h-full w-full overflow-visible"
          >
            <g transform={`translate(${viewport.x}, ${viewport.y}) scale(${viewport.zoom})`}>
              {renderedEdges.map(({ edge, active, path, midX, midY }) => (
                <g key={edge.id} data-edge-id={edge.id} data-active={active ? "true" : "false"}>
                  <path
                    d={path}
                    fill="none"
                    stroke={active ? "var(--accent)" : "var(--border-strong)"}
                    strokeWidth={active ? 2 : 1.5}
                    strokeLinecap="round"
                  />
                  {edge.label && (
                    <g transform={`translate(${midX}, ${midY})`}>
                      <rect
                        x={-Math.max(26, edge.label.length * 3.8 + 10)}
                        y={-10}
                        width={Math.max(52, edge.label.length * 7.6 + 20)}
                        height={20}
                        rx={5}
                        fill="var(--raised)"
                        stroke={active ? "var(--accent)" : "var(--border-strong)"}
                        strokeWidth={1}
                      />
                      <text
                        x={0}
                        y={3.5}
                        textAnchor="middle"
                        fontSize={10.5}
                        fontWeight={500}
                        fill={active ? "var(--accent-text)" : "var(--fg-muted)"}
                      >
                        {edge.label}
                      </text>
                    </g>
                  )}
                </g>
              ))}
            </g>
          </svg>

          {/* Transformed interactive node card layer */}
          <div
            data-testid="dot-grid-nodes"
            className="pointer-events-none absolute inset-0 origin-top-left"
            style={{ transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})` }}
          >
            {nodes.map((node) => {
              const tone = node.tone ?? "neutral";
              const w = node.width ?? DEFAULT_NODE_WIDTH;
              const h = node.height ?? DEFAULT_NODE_HEIGHT;
              const selected = selectedId === node.id;
              const hasIn = portUsage.incoming.has(node.id);
              const hasOut = portUsage.outgoing.has(node.id);
              return (
                <div
                  key={node.id}
                  data-dot-node={node.id}
                  style={{ left: node.x, top: node.y, width: w, minHeight: h }}
                  className="pointer-events-auto absolute"
                >
                  {/* Left connection port */}
                  <span
                    aria-hidden
                    className={cn(
                      "pointer-events-none absolute -left-1.5 top-1/2 z-10 size-3 -translate-y-1/2 rounded-full border",
                      hasIn || selected ? TONE_EDGE_DOT[selected ? "accent" : tone] : "border-border-strong bg-canvas",
                    )}
                  />
                  {/* Right connection port */}
                  <span
                    aria-hidden
                    className={cn(
                      "pointer-events-none absolute -right-1.5 top-1/2 z-10 size-3 -translate-y-1/2 rounded-full border",
                      hasOut || selected ? TONE_EDGE_DOT[selected ? "accent" : tone] : "border-border-strong bg-canvas",
                    )}
                  />

                  <button
                    type="button"
                    aria-pressed={selected}
                    onClick={() => onSelectNode?.(node.id)}
                    style={{ minHeight: h }}
                    className={cn(
                      "flex w-full flex-col justify-between rounded-xl border bg-surface/95 p-3.5 text-left shadow-sm transition-colors",
                      selected
                        ? "border-accent ring-1 ring-accent bg-row-on"
                        : "border-border-strong hover:border-control hover:bg-row-on/90",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        {node.icon && (
                          <span className={cn("inline-flex size-6 shrink-0 items-center justify-center rounded-md border", TONE_ACCENT[tone])}>
                            {node.icon}
                          </span>
                        )}
                        <span className={cn("truncate text-[11px] font-semibold tracking-wide uppercase", TONE_LABEL[tone])}>
                          {node.kindLabel}
                        </span>
                      </div>
                      {node.badge && (
                        <span className={cn("shrink-0 rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-none tabular-nums", TONE_ACCENT[tone])}>
                          {node.badge}
                        </span>
                      )}
                    </div>

                    <div className="mt-2 space-y-1">
                      <div className="truncate text-[13.5px] font-semibold leading-5 text-fg">
                        {node.title}
                      </div>
                      {node.description && (
                        <p className="text-[12px] leading-4 text-muted">
                          {node.description}
                        </p>
                      )}
                    </div>
                  </button>
                </div>
              );
            })}
          </div>
        </>
      )}

      {/* Bottom-left Liquid Glass zoom and fit dock */}
      <div
        data-dot-controls=""
        data-testid="dot-grid-dock"
        className="liquid-glass-dock absolute bottom-3 left-3 z-20 inline-flex items-center gap-0.5 rounded-lg p-1"
      >
        <button
          type="button"
          aria-label="Zoom out"
          onClick={() => zoomBy(-ZOOM_STEP)}
          className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-fg"
        >
          <Minus className="size-3.5" strokeWidth={2} aria-hidden />
        </button>
        <span className="min-w-[44px] text-center text-[11.5px] font-medium text-fg tabular-nums" data-testid="dot-grid-zoom-label">
          {Math.round(viewport.zoom * 100)}%
        </span>
        <button
          type="button"
          aria-label="Zoom in"
          onClick={() => zoomBy(ZOOM_STEP)}
          className="inline-flex size-7 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-fg"
        >
          <Plus className="size-3.5" strokeWidth={2} aria-hidden />
        </button>
        <div className="mx-0.5 h-4 w-px bg-border-strong" aria-hidden />
        <button
          type="button"
          aria-label="Fit view"
          onClick={() => {
            userMovedRef.current = false;
            fitView();
          }}
          className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[11.5px] font-medium text-muted hover:bg-hover hover:text-fg"
        >
          <Maximize2 className="size-3" strokeWidth={2} aria-hidden />
          <span>Fit</span>
        </button>
      </div>
    </div>
  );
}
