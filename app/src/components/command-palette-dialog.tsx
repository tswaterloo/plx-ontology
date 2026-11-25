"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { CornerDownLeft, Search, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isObjectType, suggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useShortcut } from "@/lib/platform";
import { VERIFIED_EXAMPLES } from "@/lib/examples";
import { objectHref, recordLabel, safeHref, typeLabel } from "@/lib/format";
import { currentPageQuery, scopedToPage } from "@/lib/page-context";
import { stashPending } from "@/lib/pending";
import { useAuth } from "./auth";
import { arrange, type Arranged } from "./ask-box";
import { PlaceIcon, SystemCode } from "./ui/glyphs";

/** The command bar's dialog, loaded on first use so it stays out of every page's first load. */

/** Typed like a record's number (PO, SO, invoice, item or lot: "106093", "PO 106093",
 * "LOT-23A112"): Enter waits for the lookup and opens the one record it names, asking only
 * when nothing matches. */
export function numberShaped(text: string): boolean {
  return text.length <= 30 && /\d/.test(text) && /^(?:[a-z]{1,4}[\s#]+)?[\w./-]+$/i.test(text);
}

type Item =
  | { kind: "ask"; id: string; q: string }
  | { kind: "go"; id: string; label: string; href: string; hint: string }
  | { kind: "record"; id: string; s: Arranged }
  | { kind: "example"; id: string; q: string };

// `ops`: an Admin page, listed only for the readers Admin is open to (the server refuses the rest)
const PLACES: { label: string; href: string; hint: string; words: string; ops?: boolean }[] = [
  { label: "Home", href: "/", hint: "What needs you today, and what Assist prepared", words: "my work today home front page briefing prepared assist autopilot drafts entries" },
  { label: "Ask", href: "/ask", hint: "New question", words: "ask question chat" },
  { label: "Assist", href: "/assist", hint: "What is prepared for you, drafts waiting for your OK, routines, log", words: "assist prepared drafts routines log plan autopilot" },
  { label: "Insights", href: "/insights", hint: "Dashboards: sales, purchasing, receivables and team views", words: "insights dashboards charts tiles measures reports numbers" },
  { label: "Meetings", href: "/meetings", hint: "Upcoming meetings, weekly review and follow-ups", words: "meetings calendar prep weekly review follow-ups action items" },
  { label: "Team", href: "/team", hint: "People, workload and approvals", words: "team people workload approvals grants" },
  { label: "Late POs", href: "/?show=late", hint: "Every PO past its needed-by date", words: "late orders purchase pos overdue" },
  { label: "Alerts", href: "/?show=alerts", hint: "Active alerts and notification rules", words: "alerts watching briefing rules attention" },
  { label: "Messages and tasks", href: "/activity", hint: "Work log: each message and task you started, and whether it went out", words: "activity actions messages tasks sent log journal" },
  { label: "Orders", href: "/orders", hint: "Purchase orders late first, and sales orders", words: "orders purchase po sales late" },
  { label: "Customers", href: "/customers", hint: "Find a customer", words: "customers accounts chains" },
  { label: "Suppliers", href: "/suppliers", hint: "Find a supplier", words: "suppliers vendors" },
  { label: "Products", href: "/products", hint: "Find a product, item or lot", words: "products items lots stock" },
  { label: "Records", href: "/records", hint: "Find a customer, order, invoice, shipment, supplier or product", words: "records customers orders invoices shipments receipts lots suppliers products items find" },
  { label: "More", href: "/more", hint: "Invoices, shipments, receipts, batches and every list", words: "invoices shipments receipts batches lists documents production" },
  { label: "Switches", href: "/assist?show=switches", hint: "What gets prepared for you, and Auto", words: "autopilot switches auto assist prepare" },
  { label: "Work log", href: "/work-log", hint: "What you did, day by day", words: "work log history time saved" },
  { label: "Memory", href: "/memory", hint: "What the Workbench remembers for you", words: "memory remember notes facts" },
  { label: "Templates", href: "/templates", hint: "Reply and document templates", words: "templates drafts replies documents" },
  { label: "Data health", href: "/data-health", hint: "Completeness, freshness, duplicates and sync checks", words: "data health trust fresh freshness accuracy scores duplicates missing fields sync mismatch", ops: true },
  { label: "Reliability", href: "/ops", hint: "What is broken now: capture, timers, backups", words: "reliability ops alarms broken capture timers backups health", ops: true },
  { label: "Help", href: "/help", hint: "How to ask, what each screen is for", words: "help guide how to docs questions" },
];

export default function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const { shortcut } = useShortcut();
  const router = useRouter();
  const { state } = useAuth();
  const [q, setQ] = useState("");
  const [records, setRecords] = useState<Arranged[]>([]);
  const [searching, setSearching] = useState(false);
  // the record search could not run (a refused or failed read): never "no record by that name"
  const [unsearched, setUnsearched] = useState(false);
  const [active, setActive] = useState(0);
  const list = useRef<HTMLUListElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // the text the records shown were looked up for
  const settledFor = useRef("");
  const openRef = useRef(open);
  openRef.current = open;

  // reset on open; records as you type (debounced, the stale request cancelled)
  useEffect(() => {
    if (open) {
      setQ("");
      setRecords([]);
      setUnsearched(false);
      setSearching(false);
      settledFor.current = "";
      setActive(0);
    }
  }, [open]);
  useEffect(() => {
    const text = q.trim();
    if (!open || text.length < 2) {
      setRecords([]);
      setSearching(false);
      return;
    }
    const c = new AbortController();
    setSearching(true);
    const t = setTimeout(() => {
      suggest(text, c.signal)
        .then((r) => {
          setRecords(Array.isArray(r.suggestions) ? arrange(r.suggestions, text).slice(0, 7) : []);
          setUnsearched(false);
          settledFor.current = text;
        })
        .catch(() => {
          if (c.signal.aborted) return;
          setRecords([]);
          setUnsearched(true);
        })
        .finally(() => !c.signal.aborted && setSearching(false));
    }, 80);
    return () => {
      clearTimeout(t);
      c.abort();
    };
  }, [q, open]);

  const items: { group: string; items: Item[] }[] = useMemo(() => {
    const text = q.trim();
    const low = text.toLowerCase();
    const ops = state.status === "signed-in" && state.me.ops_reader === true;
    const places = PLACES.filter((p) => (ops || !p.ops) && (!low || p.words.includes(low) || p.label.toLowerCase().includes(low))).map(
      (p): Item => ({ kind: "go", id: `go-${p.href}`, label: p.label, href: p.href, hint: p.hint }),
    );
    if (!text)
      return [
        { group: "Go to", items: places },
        { group: "Ask", items: VERIFIED_EXAMPLES.slice(0, 4).map((x, i): Item => ({ kind: "example", id: `ex-${i}`, q: x })) },
      ];
    const ask = { group: "Ask", items: [{ kind: "ask", id: "ask", q: text } as Item] };
    const recs = { group: "Records", items: records.map((s, i): Item => ({ kind: "record", id: `rec-${s.object_id}-${i}`, s })) };
    const go = { group: "Go to", items: places };
    // Enter opens what was typed for: a record's exact number or name, a page's name ("late"), else the question
    const order = records[0]?.match === "exact" ? [recs, ask, go] : low.length >= 3 && places.some((p) => p.kind === "go" && p.label.toLowerCase().startsWith(low)) ? [go, ask, recs] : [ask, recs, go];
    return order.filter((g) => g.items.length > 0);
  }, [q, records, state]);
  const flat = useMemo(() => items.flatMap((g) => g.items), [items]);
  useEffect(() => setActive((a) => Math.min(a, Math.max(0, flat.length - 1))), [flat.length]);
  useEffect(() => {
    list.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const recordRoute = (s: Arranged): string => {
    const raw = safeHref(s.open_url);
    if (raw) {
      if (raw.startsWith("/object/")) {
        const rest = raw.slice("/object/".length).split(/[?#]/, 1)[0];
        const segs = rest.split("/").filter(Boolean);
        if (segs.length === 1 && segs[0].includes(":")) {
          const decoded = decodeURIComponent(segs[0]);
          const kind = decoded.slice(0, decoded.indexOf(":"));
          if (isObjectType(kind)) return objectHref(kind, decoded);
        }
      }
      return raw;
    }
    if (/^(?:person|staff|employee)$/i.test(s.type)) return `/team?person=${encodeURIComponent(s.label)}`;
    if (s.type && s.object_id && isObjectType(s.type)) return objectHref(s.type, s.object_id);
    return "/";
  };

  const choose = useCallback(
    (it: Item | undefined) => {
      if (!it) return;
      onOpenChange(false);
      if (it.kind === "go") router.push(it.href);
      else if (it.kind === "record") router.push(recordRoute(it.s));
      else {
        stashPending(it.q, []);
        // the page it was typed on goes with a line about it or an instruction ("reply to
        // this" on an email); a plain question is asked of everything
        router.push(`/ask?q=${encodeURIComponent(it.q)}${scopedToPage(it.q) ? currentPageQuery() : ""}`);
      }
    },
    [onOpenChange, router],
  );

  // Enter on a number typed before its lookup came back: the lookup decides
  const enterNumber = useCallback(
    async (text: string) => {
      setSearching(true);
      let found: Arranged[] = [];
      try {
        const r = await suggest(text);
        found = Array.isArray(r.suggestions) ? arrange(r.suggestions, text) : [];
      } catch {
        found = [];
      }
      if (!openRef.current) return;
      const exact = found.filter((s) => s.match === "exact");
      if (exact.length === 1) choose({ kind: "record", id: `rec-${exact[0].object_id}`, s: exact[0] });
      else if (exact.length > 1) {
        // several records carry that number: shown to pick from, none opened
        setRecords(found.slice(0, 7));
        settledFor.current = text;
        setActive(0);
        setSearching(false);
      } else choose({ kind: "ask", id: "ask", q: text });
    },
    [choose],
  );

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) {
      e.preventDefault();
      setActive((a) => (flat.length ? (a + 1) % flat.length : 0));
    } else if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) {
      e.preventDefault();
      setActive((a) => (flat.length ? (a - 1 + flat.length) % flat.length : 0));
    } else if (e.key === "Home" && flat.length > 0 && !q) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && flat.length > 0 && !q) {
      e.preventDefault();
      setActive(flat.length - 1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const text = q.trim();
      if (numberShaped(text) && settledFor.current !== text) void enterNumber(text);
      else choose(flat[active]);
    }
  }

  let index = -1;
  const typed = q.trim().length >= 2;
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(0_0_0/0.18)] animate-fade-in dark:bg-[rgb(0_0_0/0.6)]" />
        <Dialog.Content
          className="fixed top-[max(12px,12vh)] left-1/2 z-50 flex max-h-[min(540px,calc(100dvh-24px))] w-[min(660px,calc(100vw-24px))] -translate-x-1/2 flex-col overflow-hidden rounded-lg border border-border-strong bg-raised shadow-[0_24px_64px_-12px_rgb(0_0_0/0.28)] animate-fade-in focus:outline-none dark:shadow-[0_24px_64px_-12px_rgb(0_0_0/0.75)]"
          aria-describedby={undefined}
          data-testid="command-palette"
        >
          <Dialog.Title className="sr-only">Find a record, go somewhere or ask</Dialog.Title>
          <div className="flex items-center gap-3 border-b border-border px-5">
            <Search className="size-[18px] shrink-0 text-muted" strokeWidth={1.8} aria-hidden />
            <input
              ref={inputRef}
              autoFocus
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setActive(0);
              }}
              onKeyDown={onKeyDown}
              placeholder="Search records or ask a question"
              className="h-[54px] min-w-0 flex-1 bg-transparent text-[16px] tracking-[-0.01em] text-fg placeholder:text-faint focus:outline-none"
              role="combobox"
              aria-expanded
              aria-controls="palette-list"
              aria-activedescendant={flat[active] ? `palette-${flat[active].id}` : undefined}
              aria-label="Find a record, go somewhere or ask"
              data-testid="palette-input"
            />
            {searching && <span className="size-3.5 shrink-0 animate-spin rounded-full border-[1.5px] border-border-strong border-t-muted" aria-label="Searching" role="status" />}
            {q.length > 0 && (
              <button
                type="button"
                aria-label="Clear search"
                onClick={() => {
                  setQ("");
                  setActive(0);
                  inputRef.current?.focus();
                }}
                className="inline-flex size-6 shrink-0 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 cursor-pointer"
                data-testid="palette-clear"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            )}
            <kbd className="hidden h-5 items-center rounded-xs border border-border bg-surface px-1.5 font-sans text-[11px] text-muted sm:flex" aria-hidden>
              Esc
            </kbd>
          </div>
          <ul ref={list} id="palette-list" role="listbox" aria-label="Results" className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 pt-1 pb-2">
            {items.map((g) => (
              <li key={g.group} role="presentation">
                <p className="px-3 pt-3 pb-1.5 text-[11px] leading-4 font-medium text-faint" role="presentation">
                  {g.group}
                </p>
                <ul role="presentation">
                  {g.items.map((it) => {
                    index += 1;
                    const i = index;
                    const on = i === active;
                    return (
                      <li
                        key={it.id}
                        id={`palette-${it.id}`}
                        role="option"
                        aria-selected={on}
                        data-index={i}
                        onMouseMove={() => !on && setActive(i)}
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => choose(it)}
                        className={cn(
                          "flex h-10 cursor-pointer items-center gap-3 rounded-md px-3 text-[13px] transition-colors",
                          it.kind === "record" && "h-11",
                          on ? "bg-nav-on text-fg" : "text-fg/85 hover:bg-hover",
                        )}
                        data-testid="palette-item"
                      >
                        <Row it={it} active={on} />
                        <span className={cn("ml-auto shrink-0 pl-2 text-[12px] text-muted", on ? "flex" : "hidden")} aria-hidden>
                          {it.kind === "ask" || it.kind === "example" ? "Ask" : "Open"}
                          <CornerDownLeft className="ml-1.5 size-3.5 self-center" strokeWidth={2} />
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
            {typed && !searching && records.length === 0 && (
              <li role="presentation" className="px-3 pt-3 text-[12px] leading-4 text-faint" data-testid="palette-no-record">
                {unsearched ? "Records couldn't be searched just now. Ask still works." : "No record by that name or number."}
              </li>
            )}
          </ul>
          <div className="hidden h-9 shrink-0 items-center gap-4 border-t border-border bg-surface/60 px-4 text-[12px] text-muted sm:flex" aria-hidden>
            <span className="flex items-center gap-1.5">
              <Key>↑</Key>
              <Key>↓</Key>
              Move
            </span>
            <span className="flex items-center gap-1.5">
              <Key>↵</Key>
              Open
            </span>
            <span className="ml-auto flex items-center gap-1.5">
              <Key>{shortcut("K")}</Key>
              Anywhere
            </span>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Key({ children }: { children: React.ReactNode }) {
  return <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded-xs border border-border bg-bg px-1 font-sans text-[11px] leading-none text-muted dark:bg-surface-2">{children}</kbd>;
}

const PARTY = /^(?:supplier|vendor|customer|contact|company|person|staff)$/i;

function Row({ it, active }: { it: Item; active?: boolean }) {
  if (it.kind === "ask" || it.kind === "example")
    return (
      <>
        <PlaceIcon href="/ask" className={active ? "text-fg" : undefined} />
        <span className="min-w-0 truncate">
          {it.kind === "ask" && <span className="text-muted">Ask </span>}
          <span className={it.kind === "ask" ? "font-medium text-fg" : "text-fg"}>{it.q}</span>
        </span>
      </>
    );
  if (it.kind === "go")
    return (
      <>
        <PlaceIcon href={it.href} className={active ? "text-fg" : undefined} />
        <span className="shrink-0 font-medium text-fg">{it.label}</span>
        <span className="hidden min-w-0 truncate text-[12px] text-muted sm:inline">{it.hint}</span>
      </>
    );
  const s = it.s;
  const party = PARTY.test(s.type);
  const sub = [typeLabel(s.type), s.facts ?? s.sublabel].filter(Boolean).join(" · ");
  return (
    <>
      {party ? null : <SystemCode system={s.systems[0] ?? "BC"} size={24} />}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] leading-[17px] font-medium text-fg">{recordLabel(s.type, s.label)}</span>
        <span className="block truncate text-[12px] leading-4 text-muted">{sub}</span>
      </span>
      {s.systems.length > 1 && (
        <span className="hidden shrink-0 items-center gap-0.5 sm:flex" aria-hidden>
          {s.systems.slice(1, 4).map((x) => (
            <SystemCode key={x} system={x} size={14} />
          ))}
        </span>
      )}
    </>
  );
}
