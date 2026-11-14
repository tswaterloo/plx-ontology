"use client";

import { useReaderWords } from "@/components/view-as";
import { AlertPlaybookPanel } from "./alert-playbook";
import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { useListKeys } from "@/lib/list-keys";
import { EvidenceTrailCard } from "@/components/evidence-trail";
import { getBriefing, getBriefingItems, setWatcherItemState } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate, money, objectHref, plural, readable, recordLabel, safeHref } from "@/lib/format";
import { briefingKind } from "@/lib/status";
import type { Briefing, BriefingGroup, BriefingItem, BriefingItems, BriefingView, LatePo } from "@/lib/types";
import { DataNotes } from "./data-notes";
import { ItemFeedback, MarkedLine, WrongReason, type ItemVerdict } from "./item-feedback";
import { lateEdge, lateFigure, StatusGlyph } from "./ui/glyphs";
import { StatusWord } from "./ui/status-badge";
import { StaleNote } from "./ui/notice";

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

/** "Chase DSM" when there is someone on record to chase (the supplier's name, else the contact's); else null ("Open"). */
function chaseWho(item: BriefingItem, supplier?: string | null): string | null {
  const c = item.next_action?.contact?.replace(/\s*<[^>]+>/, "").trim();
  if (!c) return null;
  return supplier || c;
}

/**
 * One row: the counterpart's tile, the record and its counterpart, then the summary; the
 * money and days on the right with the kind drawn as a glyph. The whole row opens the record;
 * the chase link and the evidence sit on top of it. The next step lives on the record page.
 */
const WATCHER = "watcher_attention";

/** A watcher item set aside by this reader: what was done, and an undo. */
function SetAside({ item, state, until, onUndo, busy, testId }: { item: BriefingItem; state: string; until?: string | null; onUndo: () => void; busy: boolean; testId: string }) {
  return (
    <li className="flex items-baseline gap-2 px-3 py-2 text-[12px] leading-4 text-muted" data-testid={testId}>
      <span className="min-w-0 truncate">
        {recordLabel(item.object_type, item.subject_label)}: {state === "snoozed" && until ? `snoozed until ${formatDate(until, { short: true })}` : "dismissed"}
      </span>
      <button type="button" onClick={onUndo} disabled={busy} className="shrink-0 font-medium text-fg hover:underline disabled:opacity-60 cursor-pointer">
        Undo
      </button>
    </li>
  );
}

function ItemRow({ item, latePo, grouped, testId }: { item: BriefingItem; latePo?: LatePo; grouped: boolean; testId: string }) {
  const [open, setOpen] = useState(false);
  // a watcher item this reader dismissed or snoozed (kept per reader on the server)
  const [aside, setAside] = useState<{ state: string; until?: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  // marked not useful or wrong by this reader: suppressed for them from now on
  const [marked, setMarked] = useState<ItemVerdict | null>(null);
  const [asking, setAsking] = useState(false);   // Wrong: what is wrong, under the line
  const watcher = item.event_type === WATCHER;
  async function setState(state: "dismissed" | "snoozed" | "open") {
    setBusy(true);
    setFailed(null);
    try {
      const done = await setWatcherItemState(item.id, state);
      setAside(done.state === "open" ? null : { state: done.state, until: done.until });
    } catch {
      setFailed(state === "open" ? "Couldn't undo. Try again." : "Couldn't save that. Try again.");
    } finally {
      setBusy(false);
    }
  }
  if (marked) return <MarkedLine verdict={marked} testId={testId} />;
  if (watcher && aside) {
    return <SetAside item={item} state={aside.state} until={aside.until} onUndo={() => setState("open")} busy={busy} testId={testId} />;
  }
  const href = safeHref(item.open_url) ?? objectHref(item.object_type, item.object_id);
  const kind = briefingKind(item.event_type, { amountSuspect: !!(item.amount && item.amount_suspect), fallbackLabel: item.label });
  const supplier = latePo?.supplier ?? item.counterpart ?? null;
  const days = typeof item.days === "number" ? item.days : latePo?.days_late;
  const amount = item.amount && !item.amount_suspect ? money(item.amount) : null;
  const who = chaseWho(item, supplier);
  const suspect = kind.status === "suspect";
  return (
    <li className="group relative" data-testid={testId}>
      <div
        className={cn(
          "flex gap-3 rounded-[3px] px-3 py-2.5 transition-colors duration-100 hover:bg-hover",
          "has-[a[data-row-link]:focus-visible]:ring-2 has-[a[data-row-link]:focus-visible]:ring-ring/50",
          item.acknowledged && "opacity-70",
          kind.status === "late" && typeof days === "number" && lateEdge(days),
        )}
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <Link href={href} className="shrink-0 text-[13px] leading-[18px] font-semibold outline-none after:absolute after:inset-0 after:rounded-[3px]" data-row-link>
              {recordLabel(item.object_type, item.subject_label)}
            </Link>
            {supplier && <span className="min-w-0 truncate text-[13px] leading-[18px] text-fg/80">{supplier}</span>}
            {/* under its type's header the kind goes without saying; a data problem is said */}
            {suspect || !grouped ? (
              <StatusWord shown={kind} className="shrink-0" title={suspect ? itemFacts(item)[0] : undefined} />
            ) : (
              <span data-status={kind.status} className="sr-only">
                {kind.label}
              </span>
            )}
            <span className={cn("ml-auto flex shrink-0 items-center gap-1.5 self-center pl-2 text-[12px] leading-4 font-medium tabular-nums", kind.status === "late" && lateFigure(days))}>
              {typeof days === "number" && `${days}d`}
              {kind.status !== "late" && <StatusGlyph status={kind.status} size={13} />}
            </span>
          </div>
          <div className="mt-0.5 flex items-baseline gap-2.5 text-[12px] leading-4 text-muted">
            {item.new_since_visit && (
              <span className="inline-flex shrink-0 items-center gap-1 self-center text-fg">
                <StatusGlyph status="new" size={11} />
                New
              </span>
            )}
            {item.acknowledged && <span className="shrink-0 text-fg/80">Acknowledged</span>}
            <span className="min-w-0 flex-1 truncate">{[amount ? shortSummary(item.summary).replace(/\.$/, "") : shortSummary(item.summary), amount].filter(Boolean).join(" · ")}</span>
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              className={cn("relative z-10 shrink-0 hover:text-fg cursor-pointer", !open && "lg:opacity-0 lg:group-hover:opacity-100 lg:focus-visible:opacity-100")}
            >
              {open ? "Hide" : item.trail ? "Evidence and what to do" : "What to do"}
            </button>
            {watcher && (
              <span className="relative z-10 flex shrink-0 items-baseline gap-2.5">
                <button type="button" onClick={() => setState("snoozed")} disabled={busy} className="font-medium text-fg hover:underline disabled:opacity-60 cursor-pointer">
                  Snooze a week
                </button>
                <button type="button" onClick={() => setState("dismissed")} disabled={busy} className="font-medium text-fg hover:underline disabled:opacity-60 cursor-pointer">
                  Dismiss
                </button>
              </span>
            )}
            <ItemFeedback item={item} onMarked={setMarked} onWrong={() => setAsking(true)} className="lg:opacity-0 lg:group-hover:opacity-100 lg:focus-within:opacity-100" />
            <Link
              href={`${href}${href.includes("?") ? "&" : "?"}focus=chase`}
              className="relative z-10 max-w-[40%] shrink-0 truncate font-medium text-fg hover:underline max-sm:hidden lg:opacity-0 lg:group-hover:opacity-100 lg:focus-visible:opacity-100"
            >
              {who ? `Chase ${who}` : "Open"}
            </Link>
          </div>
          {/* a watcher item's reason, as the answer gate's claims back it */}
          {watcher && item.why && (
            <p className="mt-0.5 text-[12px] leading-4 text-fg/80" data-testid="watcher-why">
              {item.why}
            </p>
          )}
          {asking && <WrongReason item={item} onMarked={setMarked} onCancel={() => setAsking(false)} />}
          {failed && (
            <p role="alert" className="relative z-10 mt-0.5 text-[12px] leading-4 text-fg">
              {failed}
            </p>
          )}
        </div>
      </div>
      {open && (
        <div className="relative z-10 space-y-3 pr-3 pb-3 pl-[56px]">
          {item.trail && <EvidenceTrailCard trail={item.trail} bare inPage showNext={false} />}
          {!watcher && <AlertPlaybookPanel alertId={item.id} />}
        </div>
      )}
    </li>
  );
}

/** One of the briefing's ranked items, as a row (also used on its own). */
export function BriefingRow({ item, latePo, grouped = false }: { item: BriefingItem; latePo?: LatePo; dense?: boolean; position?: number; grouped?: boolean }) {
  return <ItemRow item={item} latePo={latePo} grouped={grouped} testId="briefing-item" />;
}

/** A type: its header, its ranked items, then the rest of it a page at a time. */
function Group({ group, top, view, loadItems, byId }: { group: BriefingGroup; top: BriefingItem[]; view: BriefingView; loadItems: LoadItems; byId: Map<string, LatePo> }) {
  const [rows, setRows] = useState<BriefingItem[]>([]);
  const [more, setMore] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function page(offset: number) {
    setBusy(true);
    setError(null);
    try {
      const got = await loadItems(group.event_type, offset, view);
      setRows((prev) => [...(offset === 0 ? [] : prev), ...(got.items ?? [])]);
      setMore(got.more);
    } catch {
      setError("Couldn't load the rest. Try again.");
    } finally {
      setBusy(false);
    }
  }
  const topIds = new Set(top.map((i) => i.id));
  const rest = rows.filter((r) => !topIds.has(r.id));
  const left = Math.max(0, group.count - top.length - rest.length);
  const canMore = more === null ? left > 0 : more && left > 0;
  const kind = briefingKind(group.event_type, { fallbackLabel: group.label });
  // nothing of this type read yet: one line that opens it
  if (top.length === 0 && rows.length === 0)
    return (
      <section data-testid="briefing-group" aria-label={group.label} className="px-2">
        <div className="flex h-10 items-center gap-3 rounded-[3px] px-3 hover:bg-hover">
          <StatusGlyph status={kind.status} size={13} />
          <span className="min-w-0 truncate text-[13px]">
            {group.label} <span className="text-[12px] num text-faint">{group.count}</span>
          </span>
          {group.new_since_visit > 0 && (
            <span className="inline-flex shrink-0 items-center gap-1 text-[12px] text-muted">
              <StatusGlyph status="new" size={11} />
              {group.new_since_visit} new
            </span>
          )}
          {group.acknowledged > 0 && <span className="shrink-0 text-[12px] text-muted">{group.acknowledged} acknowledged</span>}
          {error && <span className="shrink-0 text-[12px] text-muted">{error}</span>}
          {canMore && (
            <button type="button" onClick={() => page(0)} disabled={busy} className="ml-auto shrink-0 text-[12px] font-medium text-muted hover:text-fg disabled:opacity-60 cursor-pointer">
              {busy ? "Loading" : `Show ${left} more`}
            </button>
          )}
        </div>
      </section>
    );
  return (
    <section data-testid="briefing-group" aria-label={group.label}>
      <div className="sticky top-[92px] z-10 flex h-9 items-end gap-3 bg-list px-5 pb-1.5 lg:top-[92px]">
        <h2 className="min-w-0 truncate text-[12px] font-medium text-muted">
          {group.label} <span className="font-normal num text-faint">{group.count}</span>
        </h2>
        <span className="ml-auto flex shrink-0 items-center gap-3 text-[12px] text-muted">
          {group.new_since_visit > 0 && (
            <span className="inline-flex items-center gap-1">
              <StatusGlyph status="new" size={11} />
              {group.new_since_visit} new
            </span>
          )}
          {group.acknowledged > 0 && <span>{group.acknowledged} acknowledged</span>}
        </span>
      </div>
      <ol className="px-2">
        {top.map((i) => (
          <BriefingRow key={i.id} item={i} latePo={byId.get(i.object_id)} grouped />
        ))}
        {rest.map((r) => (
          <ItemRow key={r.id} item={r} latePo={byId.get(r.object_id)} grouped testId="briefing-row" />
        ))}
      </ol>
      {error && <p className="px-5 pt-1 text-[12px] text-muted">{error}</p>}
      {canMore && (
        <button
          type="button"
          onClick={() => page(rows.length)}
          disabled={busy}
          className="mx-5 mt-1 mb-3 inline-flex h-7 items-center gap-1 text-[12px] font-medium text-muted hover:text-fg disabled:opacity-60 cursor-pointer"
        >
          {busy ? "Loading" : `Show ${left} more`}
        </button>
      )}
    </section>
  );
}

/** Why the briefing has fewer items than it might: unchecked kinds, other teams, places you can't open. */
export function BriefingCounts({ briefing, compact: _compact = false }: { briefing: Briefing; compact?: boolean }) {
  const w = useReaderWords();
  const groups = briefing.groups ?? [];
  const unchecked = (briefing.checks ?? []).filter((c) => c.status !== "complete");
  const lines = unchecked.map((c) => <p key={c.check}>{c.words[0].toUpperCase() + c.words.slice(1)}.</p>);
  const hidden = Object.entries(briefing.hidden_by_type ?? {});
  if (!unchecked.length && !briefing.other_teams && !hidden.length && briefing.routing_basis !== "no_department_on_record") return null;
  return (
    <div className="space-y-1.5 text-[12px] leading-[18px] text-muted" data-testid="briefing-counts">
      {unchecked.length > 1 ? (
        <details className="group/checks">
          <summary className="flex cursor-pointer list-none items-center gap-1 hover:text-fg [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-3.5 shrink-0 transition-transform group-open/checks:rotate-90" aria-hidden />
            {unchecked.length} checks didn&apos;t finish this run
          </summary>
          <div className="mt-1 space-y-1 pl-[18px]">{lines}</div>
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
      {hidden.map(([type, n]) => (
        <p key={type}>
          {(groups.find((g) => g.event_type === type)?.label ?? labelOf(type)) + ": "}
          {n} more for your team {n === 1 ? "is" : "are"} in places you can&apos;t open.
        </p>
      ))}
      {briefing.routing_basis === "no_department_on_record" && <p>No department on record for {w.you}, so nothing is routed to {w.you}.</p>}
    </div>
  );
}

function SideTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="mb-2 text-[12px] leading-4 font-medium text-muted">{children}</h3>;
}

/** A folded section of the side panel: its title is the toggle. */
function Fold({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <details className="group/fold">
      <summary className="-mx-2 flex h-8 cursor-pointer list-none items-center gap-1.5 rounded-lg px-2 text-[13px] hover:bg-hover [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 shrink-0 text-muted transition-transform group-open/fold:rotate-90" aria-hidden />
        {title}
      </summary>
      <div className="pt-2 pb-1">{children}</div>
    </details>
  );
}

const TAB_LABEL: Record<string, string> = { po_late: "Late", po_undated: "No due date", po_due: "Due soon", eta_slipped: "Date slipped", vendor_no_reply: "No reply" };
const TAB = "relative flex h-full shrink-0 items-center gap-1.5 text-[13px] whitespace-nowrap cursor-pointer";
const TAB_ON = "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:bg-accent";

/**
 * The full briefing (/briefing): tabs for all, new since the last visit and each type; every
 * type's ranked items under its own header, the rest of it a page at a time; beside the list,
 * the count, how it's ranked, what wasn't covered, what was checked and the digest preview.
 */
export function BriefingCard({
  briefing: first,
  loadView = (v) => getBriefing(undefined, v),
  loadItems = (t, o, v) => getBriefingItems(t, o, v),
  latePos = [],
  onRetry,
}: {
  briefing: Briefing;
  loadView?: LoadView;
  loadItems?: LoadItems;
  latePos?: LatePo[];
  onRetry?: () => void;
}) {
  const w = useReaderWords();
  const [view, setView] = useState<BriefingView>(first.since_visit ? "since_visit" : "all");
  const [briefing, setBriefing] = useState<Briefing>(first);
  const [tab, setTab] = useState<string>("all");
  const [rankOpen, setRankOpen] = useState(false);
  useEffect(() => setBriefing(first), [first]);
  const listRef = useRef<HTMLDivElement>(null);
  useListKeys(listRef);
  const byId = useMemo(() => new Map(latePos.map((p) => [p.object_id, p])), [latePos]);
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
      <div className="px-5 py-10 text-[13px] text-muted" data-testid="briefing-unavailable">
        {briefing.status === "not_configured" ? (
          <p>The briefing hasn&apos;t run on this server.</p>
        ) : (
          <>
            <p>Couldn&apos;t load the briefing. Try again in a few minutes.</p>
            {onRetry && (
              <button type="button" onClick={onRetry} className="mt-3 inline-flex h-10 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-4 font-medium text-fg cursor-pointer">
                Try again
              </button>
            )}
          </>
        )}
      </div>
    );
  }
  const items = briefing.items ?? [];
  const groups = briefing.groups ?? [];
  const types = briefing.types ?? [];
  const sinceVisit = view === "since_visit";
  // each type in the order of its best-ranked item; a type with none in the top list after
  const best = (t: string) => Math.min(...items.filter((i) => i.event_type === t).map((i) => i.rank), Infinity);
  const ordered = groups.map((g, n) => [g, n] as const).sort(([a, i], [b, j]) => best(a.event_type) - best(b.event_type) || i - j).map(([g]) => g);
  const shownGroups = tab === "all" ? ordered : ordered.filter((g) => g.event_type === tab);
  const hasTop = (g: BriefingGroup) => items.some((i) => i.event_type === g.event_type);
  const quiet = shownGroups.filter((g) => !hasTop(g) && g.count > 0);
  // top items whose type has no group still show, under their own kind
  const orphans = items.filter((i) => !groups.some((g) => g.event_type === i.event_type) && (tab === "all" || i.event_type === tab));

  return (
    <div className="grid flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_300px] lg:grid-rows-[auto_1fr] xl:grid-cols-[minmax(0,1fr)_320px]">
      {/* the count and today's line: above the list on a phone, atop the side panel on a desktop */}
      <div className="space-y-3 px-5 pt-5 pb-4 lg:col-start-2 lg:row-start-1 lg:border-l lg:border-border lg:bg-[color-mix(in_srgb,var(--surface)_70%,var(--bg))] lg:px-6 lg:pt-6 lg:pb-0">
        <StaleNote since={briefing.stale_since} />
        <p className="flex flex-wrap items-baseline gap-x-1.5" data-testid="briefing-heading">
          {briefing.counts_exact === false && <span className="text-[13px] text-muted">at least </span>}
          <span className="text-[22px] leading-7 font-semibold tracking-[-0.02em] num">{briefing.count}</span>{" "}
          <span className="text-[13px] text-muted">
            {briefing.count === 1 ? "item needs" : "items need"} attention{sinceVisit ? " since your last visit" : ""}
          </span>
        </p>
        {briefing.today && (
          <p className="text-[13px] leading-5" data-testid="briefing-today">
            <span className="text-muted">Today: </span>
            {briefing.today}
          </p>
        )}
        <div>
          <button type="button" onClick={() => setRankOpen((v) => !v)} aria-expanded={rankOpen} className="text-[12px] font-medium text-muted hover:text-fg cursor-pointer">
            How this is ranked
          </button>
          {rankOpen && <p className="mt-1 text-[12px] leading-[18px] text-muted">{readable(briefing.rank_basis)}</p>}
        </div>
      </div>

      <section className="min-w-0 bg-list pb-8 lg:col-start-1 lg:row-span-2 lg:row-start-1" aria-label="Open items" ref={listRef}>
        <div className="sticky top-[52px] z-20 border-b border-border bg-list">
          <div className="flex h-10 gap-4 overflow-x-auto px-5 [mask-image:linear-gradient(to_right,#000_calc(100%-40px),transparent)] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" role="tablist" aria-label="Show">
            <button type="button" role="tab" aria-selected={!sinceVisit && tab === "all"} onClick={() => (setTab("all"), choose("all"))} className={cn(TAB, !sinceVisit && tab === "all" ? TAB_ON : "text-muted hover:text-fg")}>
              All <span className="text-[12px] font-normal num text-faint">{briefing.count}</span>
            </button>
            <button type="button" role="tab" aria-selected={sinceVisit} title="New since your last visit" onClick={() => (setTab("all"), choose("since_visit"))} className={cn(TAB, sinceVisit ? TAB_ON : "text-muted hover:text-fg")}>
              New{" "}<span className="sr-only">since your last visit</span> <span className="text-[12px] font-normal tabular-nums text-faint">{briefing.new_since_visit}</span>
            </button>
            {ordered.map((g) => (
              <button key={g.event_type} type="button" role="tab" aria-selected={tab === g.event_type} onClick={() => setTab(g.event_type)} className={cn(TAB, tab === g.event_type ? TAB_ON : "text-muted hover:text-fg")}>
                {TAB_LABEL[g.event_type] ?? g.label} <span className="text-[12px] font-normal num text-faint">{g.count}</span>
              </button>
            ))}
          </div>
        </div>
        {items.length === 0 && shownGroups.every((g) => g.count === 0) ? (
          <p className="px-5 py-10 text-[13px] text-muted">{sinceVisit ? `Nothing new since ${w.your} last visit.` : `Nothing open for ${w.you}.`}</p>
        ) : (
          <div data-testid="briefing-groups">
            {shownGroups.filter(hasTop).map((g) => (
              <Group key={`${view}-${g.event_type}`} group={g} top={items.filter((i) => i.event_type === g.event_type)} view={view} loadItems={loadItems} byId={byId} />
            ))}
            {quiet.length > 0 && (
              <div className="pt-3">
                {shownGroups.some(hasTop) && (
                  <div className="flex h-9 items-end px-5 pb-1.5">
                    <h2 className="text-[12px] font-medium text-muted">Also open</h2>
                  </div>
                )}
                {quiet.map((g) => (
                  <Group key={`${view}-${g.event_type}`} group={g} top={[]} view={view} loadItems={loadItems} byId={byId} />
                ))}
              </div>
            )}
            {orphans.length > 0 && (
              <ol className="px-2 pt-3">
                {orphans.map((i) => (
                  <BriefingRow key={i.id} item={i} latePo={byId.get(i.object_id)} />
                ))}
              </ol>
            )}
          </div>
        )}
      </section>

      <aside className="space-y-7 border-t border-border px-5 pt-6 pb-10 lg:col-start-2 lg:row-start-2 lg:border-t-0 lg:border-l lg:bg-[color-mix(in_srgb,var(--surface)_70%,var(--bg))] lg:px-6" aria-label="About this briefing">
        <BriefingCountsSection briefing={briefing} />
        <section className="-mt-2">
          {types.length > 0 && (
            <Fold title="Checks run">
              <ul className="space-y-1.5 text-[12px] leading-[18px]" data-testid="briefing-checks">
                {types.map((t) => (
                  <li key={t.event_type} className="flex gap-2">
                    <StatusGlyph status={t.status === "complete" ? "ok" : t.status === "failed" ? "late" : "risk"} size={12} className="mt-[3px]" />
                    <span className="min-w-0">
                      {t.words}
                      <span className="text-muted">
                        {t.found_live === false && " · none found in the live data yet (tested on sample data only)"}
                        {t.found_live && t.first_found && ` · first found ${t.first_found}`}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            </Fold>
          )}
          <Fold title="Daily digest preview">
            <p className="mb-2 text-[12px] text-muted">Preview only: this digest is not sent.</p>
            <pre className="max-h-[360px] overflow-auto rounded-xl bg-bg px-4 py-3.5 font-sans text-[12px] leading-[18px] whitespace-pre-wrap shadow-lift" data-testid="digest-preview">
              {briefing.digest_preview}
            </pre>
          </Fold>
        </section>
      </aside>
    </div>
  );
}

function BriefingCountsSection({ briefing }: { briefing: Briefing }) {
  const body = BriefingCounts({ briefing });
  const notes = briefing.data_note_items ?? [];
  if (!body && !notes.length) return null;
  return (
    <section>
      <SideTitle>Not in this list</SideTitle>
      {body}
      <DataNotes notes={notes} className="mt-1.5" />
    </section>
  );
}

function labelOf(type: string): string {
  const words = type.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
