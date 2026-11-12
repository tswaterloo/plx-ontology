"use client";

import Link from "next/link";
import { useMemo } from "react";
import { cn } from "@/lib/cn";
import { formatDateTime, objectHref, recordLabel, safeHref, typeLabel } from "@/lib/format";
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
    if (!count) continue;
    out.push({
      kind: k.kind,
      label: k.label,
      on: types.some((t) => t.default === "on"),
      count,
      fresh: groups.reduce((n, g) => n + g.new_since_visit, 0),
      top: (briefing.items ?? []).filter((i) => (i.alert_kind ?? kindOf.get(i.event_type)) === k.kind),
      rest: groups.filter((g) => g.count > g.in_top).map((g) => ({ type: g.event_type, label: g.label, more: g.count - g.in_top })),
    });
  }
  return out.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function ItemRow({ it }: { it: BriefingItem }) {
  const href = safeHref(it.open_url) ?? objectHref(it.object_type, it.object_id);
  return (
    <li className="grid grid-cols-[10px_minmax(0,11rem)_minmax(0,1fr)_auto] items-baseline gap-x-3 py-1.5 text-[13px] max-sm:grid-cols-[10px_minmax(0,1fr)_auto]">
      <span aria-hidden className={cn("size-1.5 translate-y-[-1px] rounded-full", it.new_since_visit ? "bg-accent" : "")} />
      <Link href={href} className="truncate font-medium hover:underline max-sm:col-start-2">
        {recordLabel(it.object_type, it.subject_label)}
      </Link>
      <span className="truncate text-muted max-sm:col-span-2 max-sm:col-start-2 max-sm:row-start-2 max-sm:line-clamp-2 max-sm:whitespace-normal" title={it.summary}>
        {it.counterpart ? <span className="text-fg">{it.counterpart} · </span> : null}
        {it.summary}
      </span>
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

/** One kind that fired: its count and new count, its top items, the rest a click away. */
export function FiredKind({ f }: { f: Fired }) {
  return (
    <section className="py-3" data-testid={`fired-${f.kind}`} aria-label={f.label}>
      <h3 className="flex items-baseline gap-3 text-[13px]">
        <span className="font-semibold">{f.label}</span>
        <span className="num text-fg">{f.count}</span>
        {f.fresh > 0 && <span className="num text-[12px] text-accent">{f.fresh} new</span>}
        <span className="ml-auto text-[11px] text-faint">{f.on ? "On" : "Off: shown here only"}</span>
      </h3>
      {f.top.length > 0 && <ul className="mt-1">{f.top.map((it) => <ItemRow key={it.id} it={it} />)}</ul>}
      {f.rest.map((r) => (
        <div key={r.type} className="mt-1 pl-[22px] text-[12px]">
          <TypeItems type={r.type} count={r.more} more={`${r.more} more ${r.label.toLowerCase()}`} />
        </div>
      ))}
    </section>
  );
}

/** Rule and follow alerts by day, newest first. */
export function AlertFeed({ alerts }: { alerts: WatchAlert[] }) {
  const days = useMemo(() => {
    const out = new Map<string, WatchAlert[]>();
    for (const a of alerts) {
      const d = new Date(a.at).toLocaleDateString("en-CA", { weekday: "long", month: "short", day: "numeric", timeZone: "America/Toronto" });
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
                    <span className="block truncate text-[12px] text-muted">{a.origin === "rule" ? `Rule: ${a.rule_text ?? ""}` : `Following ${a.about.label ?? typeLabel(a.about.type ?? "")}`}</span>
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
