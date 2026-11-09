"use client";

import { useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { formatDate, readable, SYSTEM_LABEL } from "@/lib/format";
import { buildHistory, facets, filterHistory, type HistoryKind, type HistoryRow } from "@/lib/history";
import type { LinkedItem, SourceSystem, TimelineEvent } from "@/lib/types";
import { SectionTitle } from "./record/parts";
import { EvidenceDrawer } from "./source-chip";
import { SystemTile } from "./ui/glyphs";

const SHOWN = 30;

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" aria-pressed={on} onClick={onClick} className={cn("cursor-pointer rounded-full border px-2.5 py-0.5 text-[12px] leading-5", on ? "border-fg bg-hover text-fg" : "border-border text-muted hover:bg-hover")}>
      {children}
    </button>
  );
}

function Row({ r }: { r: HistoryRow }) {
  const [open, setOpen] = useState(false);
  const body = (
    <>
      <SystemTile system={r.system} size={16} className="mt-[2px]" />
      <span className="min-w-0 flex-1">
        <span className="block text-[13px] leading-5">{r.count > 1 ? `${r.title} ×${r.count}` : r.title}</span>
        {r.summary && r.count === 1 && <span className="block truncate text-[12px] leading-4 text-muted">{readable(r.summary)}</span>}
        <span className="block text-[12px] leading-4 text-muted">
          {SYSTEM_LABEL[r.system] ?? r.system} · {r.kind}
        </span>
      </span>
      <span className="shrink-0 pt-0.5 text-[12px] leading-4 tabular-nums text-muted">{r.at ? formatDate(r.at, { short: true }) : "No date"}</span>
    </>
  );
  return (
    <li>
      {r.source && r.count === 1 ? (
        <>
          <button type="button" onClick={() => setOpen(true)} className="flex w-full cursor-pointer items-start gap-2.5 rounded-lg px-2 py-1.5 text-left hover:bg-hover">
            {body}
          </button>
          {open && <EvidenceDrawer source={r.source} open={open} onOpenChange={setOpen} />}
        </>
      ) : (
        <div className="flex items-start gap-2.5 px-2 py-1.5">{body}</div>
      )}
    </li>
  );
}

/**
 * Everything dated on this record in one stream (orders, shipments, invoices, payments,
 * mail, Teams, meetings, tasks), newest first, filterable by system and kind. Built only
 * from what the page already carries for this reader: it never counts what is hidden.
 * `partial`: the server said some linked records are not visible to the reader.
 */
export function History({ events, linked, partial }: { events: TimelineEvent[]; linked: LinkedItem[]; partial?: boolean }) {
  const rows = useMemo(() => buildHistory(events, linked), [events, linked]);
  const have = useMemo(() => facets(rows), [rows]);
  const [systems, setSystems] = useState<ReadonlySet<SourceSystem>>(new Set());
  const [kinds, setKinds] = useState<ReadonlySet<HistoryKind>>(new Set());
  const [all, setAll] = useState(false);
  const toggle = <T,>(set: ReadonlySet<T>, v: T): Set<T> => {
    const next = new Set(set);
    if (!next.delete(v)) next.add(v);
    return next;
  };
  const shown = filterHistory(rows, { systems, kinds });
  const list = all ? shown : shown.slice(0, SHOWN);
  return (
    <section data-testid="history">
      <SectionTitle>History</SectionTitle>
      {rows.length === 0 ? (
        <p className="text-[13px] text-muted" data-testid="history-empty">
          {partial ? "Nothing dated on this record. Some linked records are hidden from you." : "Nothing dated on this record."}
        </p>
      ) : (
        <>
          <div className="mb-2 space-y-1.5">
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by system">
              {have.systems.map((s) => (
                <Chip key={s} on={systems.has(s)} onClick={() => setSystems((v) => toggle(v, s))}>
                  {SYSTEM_LABEL[s] ?? s}
                </Chip>
              ))}
            </div>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter by kind">
              {have.kinds.map((k) => (
                <Chip key={k} on={kinds.has(k)} onClick={() => setKinds((v) => toggle(v, k))}>
                  {k}
                </Chip>
              ))}
            </div>
          </div>
          {shown.length === 0 ? (
            <p className="text-[13px] text-muted" data-testid="history-none">Nothing matches these filters.</p>
          ) : (
            <ol className="-mx-2 space-y-px" data-testid="history-rows">
              {list.map((r, i) => (
                <Row key={`${r.at}-${r.title}-${i}`} r={r} />
              ))}
            </ol>
          )}
          {shown.length > SHOWN && (
            <button type="button" onClick={() => setAll((v) => !v)} className="mt-1 cursor-pointer text-[12px] text-muted hover:text-fg">
              {all ? "Show fewer" : "Show all"}
            </button>
          )}
          {partial && <p className="mt-2 text-[12px] text-muted">Some linked records are not visible to you, so this may not be everything.</p>}
        </>
      )}
    </section>
  );
}
