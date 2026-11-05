"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useListKeys } from "@/lib/list-keys";
import { EvidenceTrailCard } from "@/components/evidence-trail";
import { getBriefing, getBriefingItems } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate, money, objectHref, plural, readable, recordLabel, safeHref } from "@/lib/format";
import { briefingKind, STATUS_CLASS } from "@/lib/status";
import type { Briefing, BriefingGroup, BriefingItem, BriefingItems, BriefingView, LatePo } from "@/lib/types";
import { StatusWord } from "./ui/status-badge";

type LoadView = (view: BriefingView) => Promise<Briefing>;
type LoadItems = (eventType: string, offset: number, view: BriefingView) => Promise<BriefingItems>;

/** Totals said as what they are and never ranked (operating/briefing.py BASIS_WORDS). */
const BASIS_WORDS: Record<string, string> = {
  unit_price_sum: "Portal total; doesn't match its lines",
  portal_lines_unpriced: "Portal total; lines not priced",
  not_checked_against_lines: "total not checked against its lines",
};

/** The facts an item is ranked by, as short words: money involved, orders, days. A suspect
 * total is never stated as a figure: it reads "Check the total: why". */
export function itemFacts(item: BriefingItem): string[] {
  const out: string[] = [];
  const what =
    item.amount_basis === "loss" ? "lost" : item.amount_basis === "margin_at_stake" ? "margin at stake" : "order total";
  const basis = item.amount_basis ? BASIS_WORDS[item.amount_basis] : undefined;
  if (item.amount && item.amount_suspect) {
    out.push(`Check the total: ${item.amount_suspect}`);
  } else if (item.amount && basis) {
    // a total that is not its order's value (or not checked against its lines): said as what it is
    out.push(`${money(item.amount)} ${basis}`);
  } else if (item.amount && item.amount_disagree) {
    // both stated, neutrally: we can't know which side is right
    const sum = item.lines_sum ? ` (lines sum to ${money(item.lines_sum)})` : "";
    out.push(`${what} ${money(item.amount)} and its lines disagree${sum}`);
  } else if (item.amount) {
    out.push(`${money(item.amount)} ${what}`);
  }
  if (typeof item.orders_affected === "number" && item.orders_affected > 0)
    out.push(plural(item.orders_affected, "customer order"));
  if (typeof item.days === "number") out.push(plural(item.days, "day"));
  return out;
}

/** An item's summary said short ("Needed by 24 Jun; no confirmed date."). */
export function shortSummary(summary: string): string {
  const m = /^An open purchase order needed by (\d{4}-\d{2}-\d{2}) has no supplier confirmation\.?$/i.exec(summary.trim());
  if (m) return `Needed by ${formatDate(m[1], { short: true })}; no supplier date.`;
  if (/^An open purchase order has no due date recorded\.?$/i.test(summary.trim())) return "No due date on record.";
  const u = /^An open purchase order needed by (\d{4}-\d{2}-\d{2})(.*)$/i.exec(summary.trim());
  if (u) return `Needed by ${formatDate(u[1], { short: true })}${readable(u[2]).replace(/^,?\s*/, "; ")}`;
  return readable(summary);
}

function Marks({ item }: { item: BriefingItem }) {
  return (
    <>
      {item.new_since_visit && <span className="text-label font-medium text-new">New</span>}
      {item.acknowledged && <span className="text-label text-muted">Acknowledged</span>}
    </>
  );
}

/** "Chase DSM" when there is someone on record to chase (the supplier's name, else the contact's); else null ("Open"). */
function chaseWho(item: BriefingItem, supplier?: string | null): string | null {
  const c = item.next_action?.contact?.replace(/\s*<[^>]+>/, "").trim();
  if (!c) return null;
  return supplier || c;
}

/**
 * One row of "Needs you today": rank, a status badge, the record and its counterpart, the
 * numbers (days, money) right-aligned, and the action. The trail opens inline on demand;
 * the next step lives on the record page, never repeated per row.
 */
export function BriefingRow({ item, latePo, dense = false, position }: { item: BriefingItem; latePo?: LatePo; dense?: boolean; position?: number }) {
  const [open, setOpen] = useState(false);
  const href = safeHref(item.open_url) ?? objectHref(item.object_type, item.object_id);
  const kind = briefingKind(item.event_type, { amountSuspect: !!(item.amount && item.amount_suspect), fallbackLabel: item.label });
  const supplier = latePo?.supplier ?? item.counterpart ?? null;
  const subject = [recordLabel(item.object_type, item.subject_label), supplier].filter(Boolean).join(" · ");
  const days = typeof item.days === "number" ? item.days : latePo?.days_late;
  const amount = item.amount && !item.amount_suspect ? money(item.amount) : null;
  const numbers = [typeof days === "number" ? plural(days, "day") : null, amount].filter(Boolean).join(" · ");
  const who = chaseWho(item, supplier);
  return (
    <li className="-mx-2 px-2 py-3 has-[a[data-row-link]:focus-visible]:bg-accent-soft" data-testid="briefing-item">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <Link href={href} className="min-w-0 truncate text-row font-medium hover:underline focus-visible:outline-offset-4" data-row-link>
              {subject}
            </Link>
            <StatusWord shown={kind} />
            <Marks item={item} />
            <span className="ml-auto flex items-baseline gap-4">
              {numbers && <span className="text-meta tabular-nums text-muted">{numbers}</span>}
              <Link href={`${href}${href.includes("?") ? "&" : "?"}focus=chase`} className="whitespace-nowrap text-meta text-accent hover:underline">
                {who ? `Chase ${who}` : "Open"}
              </Link>
            </span>
          </div>
          <div className={cn("mt-0.5 flex flex-wrap items-baseline gap-x-3 text-meta text-muted", dense && "mt-0.5")}>
            <span className="min-w-0 basis-full sm:basis-0 sm:flex-1 sm:truncate">{shortSummary(item.summary)}</span>
            {item.trail && (
              <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                className="shrink-0 hover:text-fg hover:underline cursor-pointer"
              >
                {open ? "Hide evidence" : "Evidence"}
              </button>
            )}
          </div>
          {item.amount && item.amount_suspect && <p className="mt-0.5 text-meta text-suspect">{itemFacts(item)[0]}</p>}
          {open && item.trail && (
            <div className="mt-2">
              <EvidenceTrailCard trail={item.trail} bare inPage showNext={false} />
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

/** One line of a group's opened list: compact, with its facts and "Open". */
function Row({ item }: { item: BriefingItem }) {
  const href = safeHref(item.open_url) ?? objectHref(item.object_type, item.object_id);
  const facts = itemFacts(item);
  return (
    <li className="flex items-start gap-3 py-2 text-row" data-testid="briefing-row">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{recordLabel(item.object_type, item.subject_label)}</span>
          <Marks item={item} />
        </div>
        <p className="text-meta text-muted">{[shortSummary(item.summary), ...facts].join(" · ")}</p>
      </div>
      <Link href={href} className="shrink-0 text-meta text-accent hover:underline">
        Open
      </Link>
    </li>
  );
}

/** A type's count, and its "+N more" that opens every item of it, a page at a time. */
function Group({ group, view, loadItems }: { group: BriefingGroup; view: BriefingView; loadItems: LoadItems }) {
  const [rows, setRows] = useState<BriefingItem[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const rest = group.count - group.in_top;
  async function page(offset: number) {
    setBusy(true);
    setError(null);
    try {
      const got = await loadItems(group.event_type, offset, view);
      setRows((prev) => [...(offset === 0 ? [] : (prev ?? [])), ...(got.items ?? [])]);
      setMore(got.more);
    } catch {
      setError("Couldn't load this list. Try again.");
    } finally {
      setBusy(false);
    }
  }
  const shown = rows ?? [];
  const kind = briefingKind(group.event_type, { fallbackLabel: group.label });
  return (
    <li className="py-2.5" data-testid="briefing-group">
      <div className="flex flex-wrap items-center gap-2 text-row">
        <span className={cn("font-medium", kind.status !== "neutral" && STATUS_CLASS[kind.status].fg)}>{group.label}</span>
        <span className="tabular-nums text-muted">{group.count}</span>
        {group.new_since_visit > 0 && <span className="text-label font-medium text-new">{group.new_since_visit} new</span>}
        {group.acknowledged > 0 && <span className="text-meta text-muted">{group.acknowledged} acknowledged</span>}
        {rest > 0 && rows === null && (
          <button
            type="button"
            onClick={() => page(0)}
            disabled={busy}
            className="ml-auto text-row text-accent hover:underline cursor-pointer"
          >
            +{rest} more
          </button>
        )}
      </div>
      {error && <p className="text-meta text-late">{error}</p>}
      {rows !== null && (
        <>
          <ol className="mt-1 divide-y divide-border">
            {shown.map((r) => (
              <Row key={r.id} item={r} />
            ))}
          </ol>
          {more && (
            <button
              type="button"
              onClick={() => page(rows.length)}
              disabled={busy}
              className="mt-1 text-meta font-medium text-accent hover:underline cursor-pointer"
            >
              Show more ({rest - rows.length} left)
            </button>
          )}
        </>
      )}
    </li>
  );
}

/** Why the briefing has fewer items than it might: unchecked kinds, other teams, places you can't open. */
export function BriefingCounts({ briefing, compact = false }: { briefing: Briefing; compact?: boolean }) {
  const groups = briefing.groups ?? [];
  const unchecked = (briefing.checks ?? []).filter((c) => c.status !== "complete");
  const lines = unchecked.map((c) => (
    <p key={c.check}>{c.words[0].toUpperCase() + c.words.slice(1)}.</p>
  ));
  return (
    <div className="space-y-1 text-meta text-muted" data-testid="briefing-counts">
      {unchecked.length > 1 ? (
        <details>
          <summary className="cursor-pointer hover:text-fg">
            {unchecked.length} checks didn&apos;t finish this run{compact ? "" : "; the list may be incomplete"}
          </summary>
          <div className="mt-1 space-y-1">{lines}</div>
        </details>
      ) : (
        lines
      )}
      {briefing.other_teams > 0 && (
        <p>
          {plural(briefing.other_teams, "more item")} you may see {briefing.other_teams === 1 ? "is" : "are"} routed to
          other teams.
        </p>
      )}
      {Object.entries(briefing.hidden_by_type ?? {}).map(([type, n]) => (
        <p key={type}>
          {(groups.find((g) => g.event_type === type)?.label ?? labelOf(type)) + ": "}
          {n} more for your team {n === 1 ? "is" : "are"} in places you can&apos;t open.
        </p>
      ))}
      {briefing.routing_basis === "no_department_on_record" && (
        <p>No department on record for you, so nothing is routed to you.</p>
      )}
    </div>
  );
}

/**
 * The full briefing (/briefing): tabs for all, new since the last visit and each group; the
 * ranked items as compact rows; how it's ranked behind a button; what was checked and the
 * digest preview folded at the foot.
 */
export function BriefingCard({
  briefing: first,
  loadView = (v) => getBriefing(undefined, v),
  loadItems = (t, o, v) => getBriefingItems(t, o, v),
  latePos = [],
}: {
  briefing: Briefing;
  loadView?: LoadView;
  loadItems?: LoadItems;
  latePos?: LatePo[];
}) {
  const [view, setView] = useState<BriefingView>(first.since_visit ? "since_visit" : "all");
  const [briefing, setBriefing] = useState<Briefing>(first);
  const [tab, setTab] = useState<string>("all");
  const [rankOpen, setRankOpen] = useState(false);
  useEffect(() => setBriefing(first), [first]);
  const listRef = useRef<HTMLDivElement>(null);
  useListKeys(listRef);
  async function choose(next: BriefingView) {
    if (next === view) return;
    setView(next);
    try {
      setBriefing(await loadView(next));
    } catch {
      setView(view);
    }
  }
  if (briefing.status !== "read") {
    return (
      <p className="text-row text-muted" data-testid="briefing-unavailable">
        {briefing.status === "not_configured"
          ? "The briefing hasn't run on this server."
          : "Couldn't load the briefing. Try again in a few minutes."}
      </p>
    );
  }
  const items = briefing.items ?? [];
  const groups = briefing.groups ?? [];
  const types = briefing.types ?? [];
  const atLeast = briefing.counts_exact === false ? "at least " : "";
  const sinceVisit = view === "since_visit";
  const byId = new Map(latePos.map((p) => [p.object_id, p]));
  const shownItems = tab === "all" ? items : items.filter((i) => i.event_type === tab);
  const shownGroups = tab === "all" ? groups : groups.filter((g) => g.event_type === tab);
  const tabCls = (on: boolean) =>
    cn("-mb-px inline-flex h-9 items-center gap-1.5 border-b-2 px-1 text-row cursor-pointer", on ? "border-fg font-semibold text-fg" : "border-transparent text-muted hover:text-fg");
  return (
    <div className="space-y-4" ref={listRef}>
      {briefing.today && (
        <p className="text-row text-fg" data-testid="briefing-today">
          <span className="text-muted">Today: </span>
          {briefing.today}
        </p>
      )}
      <p className="text-row text-muted" data-testid="briefing-heading">
        {atLeast}
        {plural(briefing.count, "item")} {briefing.count === 1 ? "needs" : "need"} attention{sinceVisit ? " since your last visit" : ""}
        <button type="button" onClick={() => setRankOpen((v) => !v)} aria-expanded={rankOpen} className="ml-2 text-meta text-accent hover:underline cursor-pointer">
          How this is ranked
        </button>
      </p>
      {rankOpen && <p className="text-meta text-muted">{readable(briefing.rank_basis)}</p>}
      <div className="flex flex-wrap items-center gap-x-5 border-b border-border" role="tablist" aria-label="Show">
        <button type="button" role="tab" aria-selected={!sinceVisit && tab === "all"} onClick={() => (setTab("all"), choose("all"))} className={tabCls(!sinceVisit && tab === "all")}>
          All <span className="tabular-nums text-muted">{briefing.count}</span>
        </button>
        <button type="button" role="tab" aria-selected={sinceVisit} onClick={() => (setTab("all"), choose("since_visit"))} className={tabCls(sinceVisit)}>
          New since your last visit <span className="tabular-nums text-muted">{briefing.new_since_visit}</span>
        </button>
        {groups.map((g) => (
          <button key={g.event_type} type="button" role="tab" aria-selected={tab === g.event_type} onClick={() => setTab(g.event_type)} className={tabCls(tab === g.event_type)}>
            {g.label} <span className="tabular-nums text-muted">{g.count}</span>
          </button>
        ))}
      </div>
      {shownItems.length === 0 ? (
        <p className="text-row text-muted">{sinceVisit ? "Nothing new since your last visit." : "Nothing open for you."}</p>
      ) : (
        <ol className="divide-y divide-border">
          {shownItems.map((i, n) => (
            <BriefingRow key={i.id} item={i} latePo={byId.get(i.object_id)} position={n + 1} />
          ))}
        </ol>
      )}
      {shownGroups.length > 0 && briefing.count > items.length && (
        <div data-testid="briefing-groups">
          <h2 className="mt-2 text-section">The rest, by type</h2>
          <ul className="divide-y divide-border">
            {shownGroups.map((g) => (
              <Group key={`${view}-${g.event_type}`} group={g} view={view} loadItems={loadItems} />
            ))}
          </ul>
        </div>
      )}
      <BriefingCounts briefing={briefing} />
      {types.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer text-row text-muted hover:text-fg">What was checked</summary>
          <ul className="mt-1 space-y-0.5 text-meta" data-testid="briefing-checks">
            {types.map((t) => (
              <li key={t.event_type}>
                {t.words}
                {t.found_live === false && " · none found in the live data yet (tested on sample data only)"}
                {t.found_live && t.first_found && ` · first found ${t.first_found}`}
              </li>
            ))}
          </ul>
        </details>
      )}
      <details className="group">
        <summary className="cursor-pointer text-row text-muted hover:text-fg">Daily digest preview</summary>
        <div className="mt-2 space-y-1">
          <p className="text-meta text-muted">Preview only: this digest is not sent.</p>
          <pre className="whitespace-pre-wrap rounded border border-border bg-surface p-3 font-mono text-[12px] leading-[18px]" data-testid="digest-preview">
            {briefing.digest_preview}
          </pre>
        </div>
      </details>
    </div>
  );
}

function labelOf(type: string): string {
  const words = type.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
