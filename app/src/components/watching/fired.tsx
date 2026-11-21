"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { shortSummary } from "@/components/briefing";
import { ShowMore } from "@/components/ui/show-more";
import { cn } from "@/lib/cn";
import { formatDate, formatDateTime, objectHref, readable, recordLabel, safeHref, typeLabel } from "@/lib/format";
import type { AlertKinds, Briefing, BriefingItem, WatchAlert } from "@/lib/types";
import { TypeItems } from "./alert-kinds";

export type Fired = {
  kind: string;
  label: string;
  on: boolean;
  count: number;
  fresh: number;
  top: BriefingItem[];
  rest: { type: string; label: string; more: number }[];
  /** The oldest "as of" of its types' checks (ISO), when the server says it. */
  asOf?: string | null;
  /** Its types whose check is not current: never shown as current, said unavailable. */
  unavailable?: { type: string; label: string; since: string | null }[];
};

/** What fired, per alert kind, most first: its count, how many are new since your visit,
 * its top items and how many more each type holds. Kinds with nothing are left out. */
export function firedKinds(reg: AlertKinds | null, briefing: Briefing | null): Fired[] {
  if (!reg || briefing?.status !== "read") return [];
  const kindOf = new Map(reg.types.map((t) => [t.type, t.kind]));
  const out: Fired[] = [];
  for (const k of reg.kinds) {
    const types = reg.types.filter((t) => t.kind === k.kind);
    const groups = (briefing.groups ?? []).filter((g) => types.some((t) => t.type === g.event_type));
    const count = groups.reduce((n, g) => n + g.count, 0);
    const unavailable = groups.filter((g) => g.unavailable).map((g) => ({ type: g.event_type, label: g.label, since: g.unavailable_since ?? null }));
    if (!count && unavailable.length === 0) continue;
    const asOfs = groups.map((g) => g.as_of).filter((a): a is string => !!a).sort();
    out.push({
      kind: k.kind,
      label: k.label,
      on: types.some((t) => t.default === "on"),
      count,
      fresh: groups.reduce((n, g) => n + g.new_since_visit, 0),
      top: (briefing.items ?? []).filter((i) => (i.alert_kind ?? kindOf.get(i.event_type)) === k.kind),
      rest: groups.filter((g) => g.count > g.in_top).map((g) => ({ type: g.event_type, label: g.label, more: g.count - g.in_top })),
      asOf: asOfs[0] ?? null,
      unavailable,
    });
  }
  return out.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function ItemRow({ it }: { it: BriefingItem }) {
  const href = safeHref(it.open_url) ?? objectHref(it.object_type, it.object_id);
  const line = shortSummary(it.summary);
  // nothing to say beside the title (a reply waiting says its subject and days): the title takes the line
  const bare = !line && !it.counterpart;
  return (
    <li className="grid grid-cols-[10px_minmax(0,11rem)_minmax(0,1fr)_auto] items-baseline gap-x-3 py-1.5 text-[13px] max-sm:grid-cols-[10px_minmax(0,1fr)_auto]">
      <span aria-hidden className={cn("size-1.5 translate-y-[-1px] rounded-full", it.new_since_visit ? "bg-accent" : "")} />
      <Link href={href} className={cn("truncate font-medium hover:underline max-sm:col-start-2", bare && "col-span-2 max-sm:col-span-1")}>
        {recordLabel(it.object_type, it.subject_label)}
      </Link>
      {!bare && <span className="truncate text-muted max-sm:col-span-2 max-sm:col-start-2 max-sm:row-start-2 max-sm:line-clamp-2 max-sm:whitespace-normal" title={readable(it.summary)}>
        {it.counterpart ? <span className="text-fg">{it.counterpart} · </span> : null}
        {line}
      </span>}
      <span className={cn("num shrink-0 text-[12px] max-sm:col-start-3 max-sm:row-start-1", it.days != null && it.days > 0 ? "text-late" : "text-muted")}>{it.days != null ? `${it.days} d` : ""}</span>
      {it.new_since_visit && <span className="sr-only">new</span>}
      <ItemNext it={it} />
    </li>
  );
}

/** Under an item: the next step and whom to ask, whether it is yours, and since when it shows. */
function ItemNext({ it }: { it: BriefingItem }) {
  const next = it.next_action?.text;
  const contact = it.next_action?.contact;
  const since = it.first_seen ? formatDateTime(it.first_seen).split(", ")[0] : null;
  if (!next && !contact && !it.yours && !since) return null;
  return (
    <p className="col-start-3 col-end-5 -mt-0.5 truncate text-[12px] leading-4 text-muted max-sm:col-start-2 max-sm:col-end-4 max-sm:whitespace-normal" data-testid="fired-next">
      {next && <span className="text-fg/80">{next}</span>}
      {contact && <span>{next ? " · " : ""}ask {contact}</span>}
      {it.yours && <span className="text-accent">{next || contact ? " · " : ""}yours</span>}
      {since && <span className="text-faint">{next || contact || it.yours ? " · " : ""}since {since}</span>}
    </p>
  );
}

/** A kind's last check over an hour ago: then (only) its own time is shown beside it. */
export const CHECK_BEHIND_MS = 60 * 60 * 1000;
export function checkBehind(asOf: string, now: number = Date.now()): boolean {
  const t = new Date(asOf).getTime();
  return Number.isFinite(t) && now - t > CHECK_BEHIND_MS;
}

/** One kind that fired: its count and new count, its top items, the rest a click away. */
export function FiredKind({ f }: { f: Fired }) {
  return (
    <section className="py-3" data-testid={`fired-${f.kind}`} aria-label={f.label}>
      <h3 className="flex items-baseline gap-3 text-[13px]">
        <span className="font-semibold">{f.label}</span>
        {/* a kind that could not be read has no count: never a 0 beside "unavailable" */}
        {(f.count > 0 || !(f.unavailable ?? []).length) && <span className="num text-fg">{f.count}</span>}
        {f.fresh > 0 && <span className="num text-[12px] text-accent">{f.fresh} new</span>}
        {/* the sidebar's Data as of is the screen's time; a kind's own check time only when it is behind */}
        {f.asOf && checkBehind(f.asOf) && <span className="text-[11px] text-faint" data-testid="fired-as-of">checked {formatDateTime(f.asOf)}</span>}
        <span className="ml-auto text-[11px] text-faint">{f.on ? "On" : "Off: shown here only"}</span>
      </h3>
      {(f.unavailable ?? []).map((u) => (
        <p key={u.type} className="mt-1 pl-[22px] text-[12px] text-muted" data-testid={`unavailable-${u.type}`}>
          {u.label}: unavailable{u.since ? ` since ${formatDateTime(u.since)}` : ""}. Shown again after its next check.
        </p>
      ))}
      {f.top.length > 0 && <ul className="mt-1">{f.top.map((it) => <ItemRow key={it.id} it={it} />)}</ul>}
      {f.rest.map((r) => (
        <div key={r.type} className="mt-1 pl-[22px] text-[12px]">
          <TypeItems type={r.type} count={r.more} more={`${f.top.length ? `${r.more} more` : `Show ${r.more === 1 ? "the 1" : `all ${r.more}`}`}: ${r.label.toLowerCase()}`} />
        </div>
      ))}
    </section>
  );
}

/** Rule and follow alerts by day, newest first. */
/** A changed value as a person reads it: a date as a day, anything else as it stands. */
function changeValue(v: string): string {
  return /^\d{4}-\d{2}-\d{2}(T|$)/.test(v) ? formatDate(v, { short: true }) || v : v;
}

/** An alert's field changes, the first three in line; the rest open in place. */
function AlertChanges({ changes }: { changes: NonNullable<WatchAlert["changes"]> }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="block text-[12px] text-fg/80" data-testid="alert-changes">
      {(open ? changes : changes.slice(0, 3)).map((c, i) => (
        <span key={`${c.property}-${i}`} className="mr-3 inline-block">
          <span className="text-muted">{c.property.replace(/_/g, " ")}</span>{" "}
          {c.before != null && <span className="num text-muted line-through decoration-[color:var(--faint)]">{changeValue(c.before)}</span>}
          {c.before != null && " → "}
          <span className="num">{c.after != null ? changeValue(c.after) : "cleared"}</span>
        </span>
      ))}
      <ShowMore hidden={changes.length - 3} open={open} onToggle={() => setOpen((v) => !v)} />
    </span>
  );
}

export function AlertFeed({ alerts }: { alerts: WatchAlert[] }) {
  const days = useMemo(() => {
    const out = new Map<string, WatchAlert[]>();
    for (const a of alerts) {
      const d = new Date(a.at).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "short", timeZone: "America/Toronto" }).replace("Sept", "Sep");
      out.set(d, [...(out.get(d) ?? []), a]);
    }
    return [...out.entries()];
  }, [alerts]);
  return (
    <div className="space-y-3">
      {days.map(([day, list]) => (
        <section key={day}>
          <h4 className="mb-0.5 text-[12px] text-muted">{day}</h4>
          <ul className="divide-y divide-border">
            {list.map((a) => {
              const href = a.record.type ? objectHref(a.record.type, a.record.object_id) : null;
              return (
                <li key={a.alert_id} className="grid grid-cols-[10px_minmax(0,1fr)_auto] items-baseline gap-x-3 py-1.5 text-[13px]">
                  <span aria-hidden className={cn("size-1.5 rounded-full", !a.read && "bg-accent")} />
                  <span className="min-w-0">
                    {href ? (
                      <Link href={href} className="hover:underline">
                        {a.sentence}
                      </Link>
                    ) : (
                      a.sentence
                    )}
                    {(a.changes ?? []).length > 0 && <AlertChanges changes={a.changes ?? []} />}
                    <span className="block truncate text-[12px] text-muted">
                      {a.origin === "rule" ? `Rule: ${a.rule_text ?? ""}` : `Following ${a.about.label ?? typeLabel(a.about.type ?? "")}`}
                      {a.record.object_id !== a.about.object_id && a.record.label ? ` · on ${a.record.label}` : ""}
                      {typeof a.counted === "number" && a.counted > 1 ? ` · ${a.counted} records` : ""}
                    </span>
                  </span>
                  <span className="num text-[12px] text-muted">{formatDateTime(a.at).split(", ")[1] ?? ""}</span>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
