"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { brokenScale, dayLabel, yearOf, type Custody, type CustodyEvent, type Track, type MarkKind } from "@/lib/custody";
import { formatDateTime, readable } from "@/lib/format";
import { EvidenceDrawer } from "./source-chip";

/**
 * The chain of custody of one order, drawn: a time axis with a track per system, a mark per
 * dated event (its shape says what kind), the needed-by rule, today, the late gap hatched and
 * the supplier's promise as it moved. One readout line under it always names the focused
 * event; hover, ←/→ or a tap focuses, a click opens the source. Below 560 px it turns into a
 * vertical spine.
 */

const TRACK_LABEL: Record<Track, string> = { BC: "Business Central", Portal: "Portal", Email: "Email", Teams: "Teams", Other: "Other" };
const TRACK_TAG: Record<Track, string> = { BC: "BC", Portal: "Portal", Email: "Email", Teams: "Teams", Other: "Other" };
const MARK_WORD: Record<MarkKind, string> = {
  issued: "Order placed",
  promise: "Promise",
  lead: "Possible confirmation",
  message: "Message",
  shipped: "Shipped",
  receipt: "Received",
  document: "Document",
  record: "Record",
};

const GUTTER = 84;
const PAD_R = 20;
const TOP = 34;
const TRACK_H = 30;
const PROMISE_H = 30;
const AXIS = 30;

function Mark({ kind, x, y, focus }: { kind: MarkKind; x: number; y: number; focus: boolean }) {
  const common = "transition-none";
  const shape =
    kind === "issued" || kind === "record" ? (
      <rect x={x - 3.5} y={y - 3.5} width={7} height={7} className={cn("fill-fg", common)} />
    ) : kind === "promise" ? (
      <rect x={x - 4} y={y - 4} width={8} height={8} transform={`rotate(45 ${x} ${y})`} className="fill-bg stroke-fg" strokeWidth={1.5} />
    ) : kind === "lead" ? (
      <rect x={x - 4} y={y - 4} width={8} height={8} transform={`rotate(45 ${x} ${y})`} className="fill-bg stroke-risk" strokeWidth={1.5} strokeDasharray="2 1.5" />
    ) : kind === "message" ? (
      <circle cx={x} cy={y} r={3.75} className="fill-bg stroke-fg" strokeWidth={1.5} />
    ) : kind === "shipped" ? (
      <path d={`M${x - 4.5},${y + 4} L${x},${y - 4.5} L${x + 4.5},${y + 4} Z`} className="fill-fg" />
    ) : kind === "receipt" ? (
      <circle cx={x} cy={y} r={4.5} className="fill-ok" />
    ) : (
      <rect x={x - 3.5} y={y - 4.5} width={7} height={9} className="fill-bg stroke-fg" strokeWidth={1.5} />
    );
  return (
    <g>
      {focus && <circle cx={x} cy={y} r={9} className="fill-none stroke-ring" strokeWidth={2} />}
      {shape}
    </g>
  );
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => setW(Math.round(el.getBoundingClientRect().width));
    fit();
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit);
    ro?.observe(el);
    return () => ro?.disconnect();
  }, []);
  return [ref, w];
}

export function CustodyRail({ custody, title = "Timeline" }: { custody: Custody; title?: string }) {
  const [box, width] = useWidth<HTMLDivElement>();
  const { events } = custody;
  // the focused event: the latest by default (what a reader wants first)
  const [focus, setFocus] = useState(events.length - 1);
  const [open, setOpen] = useState<CustodyEvent | null>(null);
  const focused = events[Math.min(focus, events.length - 1)];
  const lateDays = custody.gap ? custody.gap[1] - custody.gap[0] : null;
  return (
    <section className="border-t border-rule" aria-label={title} data-testid="custody">
      <div className="flex min-h-11 flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-2.5">
        <h2 className="text-section">{title}</h2>
        <p className="text-meta text-muted">
          {events.length} events
          {custody.due !== null && <>, needed by {dayLabel(custody.due, yearOf(custody.today))}</>}
          {lateDays !== null && lateDays > 0 && <>, <span className="text-late">{lateDays} days late</span></>}
          {custody.promises.length > 1 && <>, promise moved {custody.promises.length - 1} times</>}
        </p>
      </div>
      <div ref={box} className="min-w-0">
        {width === 0 ? null : width < 560 ? (
          <Spine custody={custody} onOpen={setOpen} />
        ) : (
          <Chart custody={custody} width={width} focus={focus} setFocus={setFocus} onOpen={setOpen} />
        )}
      </div>
      {width >= 560 && focused && (
        <p className="flex min-h-9 flex-wrap items-baseline gap-x-2 border-t border-border py-2 text-meta" aria-live="polite" data-testid="custody-readout">
          <span className="tabular-nums text-muted">{formatDateTime(focused.at)}</span>
          <span className="text-[11px] font-medium text-faint">{TRACK_TAG[focused.track]}</span>
          <span className="font-medium text-fg">{readable(focused.title).replace(/^bc\b/i, "BC")}</span>
          {focused.detail && <span className="min-w-0 truncate text-meta text-muted">{readable(focused.detail)}</span>}
          {focused.source && (
            <button type="button" onClick={() => setOpen(focused)} className="ml-auto inline-flex items-center gap-0.5 font-medium text-accent hover:underline cursor-pointer">
              Open source
            </button>
          )}
        </p>
      )}
      <ol className="sr-only">
        {events.map((e) => (
          <li key={e.id}>
            {dayLabel(e.day, yearOf(custody.today))}, {TRACK_LABEL[e.track]}: {readable(e.title)}
          </li>
        ))}
      </ol>
      {open?.source && <EvidenceDrawer source={open.source} open onOpenChange={(o) => !o && setOpen(null)} />}
    </section>
  );
}

function Chart({
  custody,
  width,
  focus,
  setFocus,
  onOpen,
}: {
  custody: Custody;
  width: number;
  focus: number;
  setFocus: (i: number) => void;
  onOpen: (e: CustodyEvent) => void;
}) {
  const hatch = useId().replace(/:/g, "");
  const { events, tracks, due, today, gap, promises } = custody;
  const hasPromises = promises.length > 0;
  const plotW = width - GUTTER - PAD_R;
  const stops = [custody.min, custody.max, today, ...(due !== null ? [due] : []), ...events.map((e) => e.day), ...promises.flatMap((p) => [p.day, p.promised])];
  const { x: sx, breaks } = useMemo(() => brokenScale(stops, plotW), [stops.join(","), plotW]); // eslint-disable-line react-hooks/exhaustive-deps
  const x = (d: number) => GUTTER + sx(d);
  const trackTop = TOP + (hasPromises ? PROMISE_H : 0);
  const trackY = (l: Track) => trackTop + tracks.indexOf(l) * TRACK_H + TRACK_H / 2;
  const bottom = trackTop + tracks.length * TRACK_H;
  const height = bottom + AXIS;
  const thisYear = yearOf(today);

  // marks on the same day in one track stack (dodge), a count past three
  const placed = useMemo(() => {
    const seen = new Map<string, number>();
    return events.map((e) => {
      const k = `${e.track}:${Math.round(sx(e.day) / 10)}`;
      const n = seen.get(k) ?? 0;
      seen.set(k, n + 1);
      // stacked within the track: centre, above, below
      const off = n === 0 ? 0 : n % 2 ? -8 : 8;
      return { e, k, x: GUTTER + sx(e.day), y: trackY(e.track) + (n > 2 ? 0 : off), hidden: n > 2 };
    }).map((p, _, all) => ({ ...p, stacked: all.filter((q) => q.k === p.k).length > 1 }));
  }, [events, sx, tracks]); // eslint-disable-line react-hooks/exhaustive-deps

  // month ticks
  const ticks: { x: number; label: string }[] = [];
  for (let d = custody.min; d <= custody.max; d++) {
    const date = new Date(d * 86_400_000);
    // no month label inside a stretch drawn short (its length is printed there instead)
    if (date.getUTCDate() === 1 && !breaks.some((b) => Math.abs(GUTTER + b.at - x(d)) < 26)) ticks.push({ x: x(d), label: date.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" }).replace("Sept", "Sep") + (date.getUTCMonth() === 0 ? ` ${date.getUTCFullYear()}` : "") });
  }

  function onKeyDown(ev: React.KeyboardEvent) {
    if (ev.key === "ArrowRight") setFocus(Math.min(events.length - 1, focus + 1));
    else if (ev.key === "ArrowLeft") setFocus(Math.max(0, focus - 1));
    else if (ev.key === "Home") setFocus(0);
    else if (ev.key === "End") setFocus(events.length - 1);
    else if (ev.key === "Enter" && events[focus]?.source) onOpen(events[focus]);
    else return;
    ev.preventDefault();
  }

  const fx = placed[focus]?.x;
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="block select-none text-fg focus:outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      role="group"
      aria-label={`Chain of custody, ${events.length} events. Left and right arrows move between them; Enter opens the source.`}
      tabIndex={0}
      onKeyDown={onKeyDown}
      data-testid="custody-chart"
    >
      <defs>
        <pattern id={hatch} width={5} height={5} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1={0} y1={0} x2={0} y2={5} className="stroke-late-line" strokeWidth={1} />
        </pattern>
      </defs>

      {/* the late gap: needed-by to today (or the late receipt), across every row */}
      {gap && (
        <g>
          <rect x={x(gap[0])} y={TOP - 4} width={Math.max(1, x(gap[1]) - x(gap[0]))} height={bottom - TOP + 4} className="fill-late-soft" />
          <rect x={x(gap[0])} y={TOP - 4} width={Math.max(1, x(gap[1]) - x(gap[0]))} height={bottom - TOP + 4} fill={`url(#${hatch})`} opacity={0.6} />
          <text x={x(gap[1]) - 6} y={bottom - 7} textAnchor="end" className="fill-late text-[12px] font-semibold tabular-nums">
            {gap[1] - gap[0]} days late
          </text>
        </g>
      )}

      {/* rows */}
      {tracks.map((l) => {
        const empty = !events.some((e) => e.track === l);
        return (
          <g key={l}>
            <text x={0} y={trackY(l) + 4} className="fill-muted text-[11px] font-medium">
              {TRACK_TAG[l]}
            </text>
            <line x1={GUTTER} x2={width - PAD_R} y1={trackY(l)} y2={trackY(l)} className="stroke-border" strokeDasharray={empty ? "1 3" : undefined} />
            {empty && (
              <text x={GUTTER + 6} y={trackY(l) - 5} className="fill-faint text-[10px]">
                no events
              </text>
            )}
          </g>
        );
      })}

      {/* quiet stretches, broken */}
      {breaks.map((b, i) => (
        <g key={i}>
          <rect x={GUTTER + b.at - 5} y={TOP} width={10} height={bottom - TOP} className="fill-bg" />
          <line x1={GUTTER + b.at - 3} x2={GUTTER + b.at - 3} y1={TOP} y2={bottom} className="stroke-border-strong" strokeDasharray="2 2" />
          <line x1={GUTTER + b.at + 3} x2={GUTTER + b.at + 3} y1={TOP} y2={bottom} className="stroke-border-strong" strokeDasharray="2 2" />
          <text x={GUTTER + b.at} y={TOP - 4} textAnchor="middle" className="fill-faint text-[10px] tabular-nums">
            {b.days} d
          </text>
        </g>
      ))}

      {/* the supplier's promise, as it moved: dashed from each promised day to the next, the slip labelled */}
      {hasPromises && (
        <g>
          <text x={0} y={TOP + PROMISE_H / 2 + 4} className="fill-muted text-[11px] font-medium">
            Promised
          </text>
          {promises.map((p, i) => {
            const prev = promises[i - 1];
            const y = TOP + PROMISE_H / 2;
            const slip = prev ? p.promised - prev.promised : 0;
            const pastDue = due !== null && p.promised > due;
            return (
              <g key={i}>
                {prev && (
                  <>
                    <line x1={x(prev.promised)} x2={x(p.promised)} y1={y} y2={y} className={pastDue ? "stroke-late" : "stroke-fg"} strokeDasharray="3 2" />
                    {slip !== 0 && (
                      <text x={(x(prev.promised) + x(p.promised)) / 2} y={y - 7} textAnchor="middle" className={cn("text-[10px] font-medium tabular-nums", pastDue ? "fill-late" : "fill-muted")}>
                        {slip > 0 ? "+" : ""}
                        {slip} d
                      </text>
                    )}
                  </>
                )}
                <rect x={x(p.promised) - 4} y={y - 4} width={8} height={8} transform={`rotate(45 ${x(p.promised)} ${y})`} className={cn("stroke-fg", i === promises.length - 1 ? "fill-fg" : "fill-bg")} strokeWidth={1.5} />
              </g>
            );
          })}
        </g>
      )}

      {/* needed-by and today */}
      {due !== null && (
        <g>
          <line x1={x(due)} x2={x(due)} y1={TOP - 14} y2={bottom} className="stroke-fg" />
          <text x={x(due) + 4} y={TOP - 18} className="fill-fg text-[11px] font-medium">
            Needed {dayLabel(due, thisYear)}
          </text>
        </g>
      )}
      <g>
        <line x1={x(today)} x2={x(today)} y1={TOP - 14} y2={bottom} className="stroke-accent" strokeDasharray="2 2" />
        <text x={x(today) - 4} y={TOP - 18} textAnchor="end" className="fill-accent text-[11px] font-medium">
          {custody.stale ? "As of" : "Today"} {dayLabel(today, thisYear)}
        </text>
      </g>

      {/* crosshair through the focused event */}
      {fx !== undefined && <line x1={fx} x2={fx} y1={TOP} y2={bottom} className="stroke-faint" strokeDasharray="1 2" />}

      {/* the events */}
      {placed.map(({ e, x: px, y, hidden, stacked }, i) =>
        hidden ? null : (
          <g
            key={e.id}
            onMouseEnter={() => setFocus(i)}
            onClick={() => e.source && onOpen(e)}
            className={e.source ? "cursor-pointer" : undefined}
            data-testid="custody-mark"
          >
            {/* hit area: generous alone, no taller than the stacking step when marks share a day */}
            <rect x={px - 9} y={y - (stacked ? 4 : 11)} width={18} height={stacked ? 8 : 22} fill="transparent" />
            <Mark kind={e.mark} x={px} y={y} focus={i === focus} />
          </g>
        ),
      )}

      {/* axis */}
      <line x1={GUTTER} x2={width - PAD_R} y1={bottom + 6} y2={bottom + 6} className="stroke-border-strong" />
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={t.x} x2={t.x} y1={bottom + 6} y2={bottom + 10} className="stroke-border-strong" />
          <text x={t.x + 3} y={bottom + 22} className="fill-muted text-[11px]">
            {t.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

/** Below 560 px: time runs down a spine, a row per event, the late stretch hatched on the spine. */
function Spine({ custody, onOpen }: { custody: Custody; onOpen: (e: CustodyEvent) => void }) {
  const { events, due, today, gap } = custody;
  const thisYear = yearOf(today);
  type Row = { kind: "event"; e: CustodyEvent } | { kind: "marker"; day: number; label: string; tone: "fg" | "accent" };
  const rows: Row[] = [
    ...events.map((e): Row => ({ kind: "event", e })),
    ...(due !== null ? [{ kind: "marker", day: due, label: "Needed by", tone: "fg" } as Row] : []),
    { kind: "marker", day: today, label: custody.stale ? "Data as of" : "Today", tone: "accent" } as Row,
  ].sort((a, b) => (a.kind === "event" ? a.e.day : a.day) - (b.kind === "event" ? b.e.day : b.day));
  const inGap = (d: number) => !!gap && d >= gap[0] && d <= gap[1];
  return (
    <ol className="relative" data-testid="custody-spine">
      {rows.map((r, i) => {
        const day = r.kind === "event" ? r.e.day : r.day;
        const prevDay = i > 0 ? (rows[i - 1].kind === "event" ? (rows[i - 1] as { e: CustodyEvent }).e.day : (rows[i - 1] as { day: number }).day) : day;
        const late = inGap(day) && inGap(prevDay) && i > 0;
        return (
          <li key={i} className="relative grid grid-cols-[56px_20px_minmax(0,1fr)] items-start">
            <span className={cn("pt-2.5 text-meta tabular-nums", r.kind === "marker" ? (r.tone === "accent" ? "font-semibold text-accent" : "font-semibold text-fg") : "text-muted")}>
              {dayLabel(day, thisYear)}
            </span>
            <span className={cn("relative flex h-full justify-center", late ? "bg-late-soft" : "")} aria-hidden>
              <span className={cn("absolute inset-y-0 w-0.5", late ? "bg-late-line" : "bg-border")} />
              <span className="relative mt-3.5">
                {r.kind === "marker" ? (
                  <span className={cn("block h-0.5 w-4", r.tone === "accent" ? "bg-accent" : "bg-fg")} />
                ) : (
                  <svg width={12} height={12} viewBox="0 0 12 12" className="block overflow-visible">
                    <Mark kind={r.e.mark} x={6} y={6} focus={false} />
                  </svg>
                )}
              </span>
            </span>
            {r.kind === "marker" ? (
              <p className={cn("min-h-11 pt-2.5 pl-2 text-label", r.tone === "accent" ? "text-accent" : "text-fg")}>
                {r.label}
                {r.tone === "accent" && gap && gap[1] === today && <span className="ml-2 normal-case tracking-normal text-late">{gap[1] - gap[0]} days late</span>}
              </p>
            ) : (
              <button
                type="button"
                onClick={() => r.e.source && onOpen(r.e)}
                disabled={!r.e.source}
                className="min-h-11 py-2 pl-2 text-left disabled:cursor-default cursor-pointer"
              >
                <span className="block text-row">
                  <span className="mr-1.5 text-[11px] font-medium text-faint">{TRACK_TAG[r.e.track]}</span>
                  {readable(r.e.title)}
                </span>
                {r.e.detail && <span className="block text-meta leading-5 text-muted">{readable(r.e.detail)}</span>}
                <span className="sr-only">{MARK_WORD[r.e.mark]}</span>
              </button>
            )}
          </li>
        );
      })}
    </ol>
  );
}
