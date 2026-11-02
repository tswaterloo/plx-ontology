"use client";

import { ArrowRight, ChevronDown, Info, Mail } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { EvidenceTrailCard } from "@/components/evidence-trail";
import { getBriefing, getBriefingItems } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate, money, objectHref, plural, readable, recordLabel, safeHref } from "@/lib/format";
import { briefingKind } from "@/lib/status";
import type { Briefing, BriefingGroup, BriefingItem, BriefingItems, BriefingView, LatePo } from "@/lib/types";
import { StatusBadge } from "./ui/status-badge";

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
  if (m) return `Needed by ${formatDate(m[1], { short: true })}; no confirmed date.`;
  const u = /^An open purchase order needed by (\d{4}-\d{2}-\d{2})(.*)$/i.exec(summary.trim());
  if (u) return `Needed by ${formatDate(u[1], { short: true })}${readable(u[2]).replace(/^,?\s*/, "; ")}`;
  return readable(summary);
}

function Marks({ item }: { item: BriefingItem }) {
  return (
    <>
      {item.new_since_visit && <StatusBadge status="new" label="New" />}
      {item.acknowledged && <StatusBadge status="neutral" label="Acknowledged" />}
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
  const supplier = latePo?.supplier ?? null;
  const subject = [recordLabel(item.object_type, item.subject_label), supplier].filter(Boolean).join(" · ");
  const days = typeof item.days === "number" ? item.days : latePo?.days_late;
  const amount = item.amount && !item.amount_suspect ? money(item.amount) : null;
  const numbers = [typeof days === "number" ? plural(days, "day") : null, amount].filter(Boolean).join(" · ");
  const who = chaseWho(item, supplier);
  return (
    <li className="py-3" data-testid="briefing-item">
      <div className="flex items-start gap-3">
        {/* its place in the list as shown: ranks of items left out (tests, other teams) would leave gaps */}
        <span className="w-5 shrink-0 pt-0.5 text-right text-meta tabular-nums text-muted">{position ?? item.rank}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <StatusBadge shown={kind} />
            <Marks item={item} />
            <Link href={href} className="min-w-0 truncate text-row font-medium hover:underline">
              {subject}
            </Link>
            <span className="ml-auto flex items-center gap-3">
              {numbers && <span className="text-row tabular-nums text-muted">{numbers}</span>}
              <Link href={`${href}${href.includes("?") ? "&" : "?"}focus=chase`} className="inline-flex items-center gap-1 whitespace-nowrap text-row font-medium text-accent hover:underline">
                {who ? `Chase ${who}` : "Open"} <ArrowRight className="size-3.5" aria-hidden />
              </Link>
            </span>
          </div>
          <div className={cn("mt-0.5 flex flex-wrap items-baseline gap-x-3 text-meta text-muted", dense && "mt-0")}>
            <span className="min-w-0 flex-1 truncate">{shortSummary(item.summary)}</span>
            {item.trail && (
              <button
                type="button"
                onClick={() => setOpen((v) => !v)}
                aria-expanded={open}
                className="inline-flex shrink-0 items-center gap-0.5 hover:text-fg cursor-pointer"
              >
                How we know <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} aria-hidden />
              </button>
            )}
          </div>
          {item.amount && item.amount_suspect && <p className="mt-0.5 text-meta text-suspect">{itemFacts(item)[0]}</p>}
          {open && item.trail && (
            <div className="mt-3">
              <EvidenceTrailCard trail={item.trail} showNext={false} />
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
      <span className="w-6 shrink-0 text-right text-meta tabular-nums text-muted">{item.rank}</span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{recordLabel(item.object_type, item.subject_label)}</span>
          <Marks item={item} />
        </div>
        <p className="text-meta text-muted">{[shortSummary(item.summary), ...facts].join(" · ")}</p>
      </div>
      <Link href={href} className="shrink-0 text-meta font-medium text-accent hover:underline">
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
      setError("This list didn't load. Try again.");
    } finally {
      setBusy(false);
    }
  }
  const shown = rows ?? [];
  const kind = briefingKind(group.event_type, { fallbackLabel: group.label });
  return (
    <li className="py-2.5" data-testid="briefing-group">
      <div className="flex flex-wrap items-center gap-2 text-row">
        <StatusBadge shown={kind} />
        <span className="font-medium">{group.label}</span>
        <span className="tabular-nums text-muted">{group.count}</span>
        {group.new_since_visit > 0 && <StatusBadge status="new" label={`${group.new_since_visit} new`} />}
        {group.acknowledged > 0 && <span className="text-meta text-muted">{group.acknowledged} acknowledged</span>}
        {rest > 0 && rows === null && (
          <button
            type="button"
            onClick={() => page(0)}
            disabled={busy}
            className="ml-auto text-row font-medium text-accent hover:underline cursor-pointer"
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
            {unchecked.length} checks didn&apos;t run in full this time{compact ? "" : ", so this list may be short"}
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
          {n} more for your team {n === 1 ? "is" : "are"} in places you can&apos;t open (a count only).
        </p>
      ))}
      {briefing.routing_basis === "no_department_on_record" && (
        <p>No department is on record for you, so nothing is routed to you yet.</p>
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
          ? "The watch list hasn't run on this server yet."
          : "Your briefing couldn't be read just now. Try again in a few minutes."}
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
    cn("inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-row cursor-pointer", on ? "bg-surface-2 font-medium text-fg" : "text-muted hover:text-fg");
  return (
    <div className="space-y-4">
      {briefing.today && (
        <p className="text-row font-medium text-fg" data-testid="briefing-today">
          <span className="text-muted font-normal">Today in one line: </span>
          {briefing.today}
        </p>
      )}
      <p className="text-row text-muted" data-testid="briefing-heading">
        {atLeast}
        {plural(briefing.count, "item")} {briefing.count === 1 ? "needs" : "need"} attention{sinceVisit ? " since your last visit" : ""}
        <button type="button" onClick={() => setRankOpen((v) => !v)} aria-expanded={rankOpen} className="ml-2 inline-flex items-center gap-0.5 text-meta hover:text-fg cursor-pointer">
          <Info className="size-3.5" aria-hidden /> How this is ranked
        </button>
      </p>
      {rankOpen && <p className="rounded-lg bg-surface-2 p-3 text-meta text-muted">{readable(briefing.rank_basis)}</p>}
      <div className="flex flex-wrap items-center gap-1 border-b border-border pb-2" role="tablist" aria-label="Show">
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
        <p className="text-row text-muted">{sinceVisit ? "Nothing new since your last visit." : "Nothing routed to you needs attention."}</p>
      ) : (
        <ol className="divide-y divide-border">
          {shownItems.map((i, n) => (
            <BriefingRow key={i.id} item={i} latePo={byId.get(i.object_id)} position={n + 1} />
          ))}
        </ol>
      )}
      {shownGroups.length > 0 && briefing.count > items.length && (
        <div data-testid="briefing-groups">
          <h2 className="text-section">Everything else, by type</h2>
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
          <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-row text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
            What was checked <ChevronDown className="size-3.5 group-open:rotate-180" aria-hidden />
          </summary>
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
        <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-row text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
          <Mail className="size-3.5" aria-hidden /> Daily digest preview <ChevronDown className="size-3.5 group-open:rotate-180" aria-hidden />
        </summary>
        <div className="mt-2 space-y-1">
          <p className="text-meta text-muted">Preview only: this digest is not sent.</p>
          <pre className="whitespace-pre-wrap rounded-lg bg-surface-2 p-3 text-meta" data-testid="digest-preview">
            {briefing.digest_preview}
          </pre>
        </div>
      </details>
    </div>
  );
}

function labelOf(type: string): string {
  return type.replace(/_/g, " ");
}
