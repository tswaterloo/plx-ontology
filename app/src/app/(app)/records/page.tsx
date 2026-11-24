"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { RecordList } from "@/components/record-list";
import { getRecordViews, suggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ApiError, plainError } from "@/lib/errors";
import { formatDateTime, recordLabel, safeHref, typeLabel } from "@/lib/format";
import { useRecentRecords, useSaved } from "@/lib/saved";
import type { RecordViews, SuggestResponse } from "@/lib/types";

/** Every record ("All", the first tab), then the four kinds people look for and the record types each one holds. */
const RECORD_KINDS = [
  { key: "all", label: "All", types: [], hint: "a name or any number: customer, supplier, order, PO, invoice, shipment, receipt, credit note, lot, item or batch" },
  { key: "customers", label: "Customers", types: ["Customer", "customer", "Chain"], hint: "a customer's name or number", list: "customers" },
  { key: "orders", label: "Orders", types: ["PurchaseOrder", "SalesOrder", "purchase_order", "sales_order", "po", "order"], hint: "a PO or customer order number", list: "purchase_orders" },
  { key: "suppliers", label: "Suppliers", types: ["Supplier", "Vendor", "supplier"], hint: "a supplier's name or number", list: "suppliers" },
  { key: "documents", label: "Documents", types: ["SalesInvoice", "SupplierInvoice", "Shipment", "Receipt", "SalesCreditMemo", "PurchaseInvoice"], hint: "an invoice, shipment, receipt or credit note number", list: "sales_invoices" },
  { key: "products", label: "Products", types: ["Item", "Lot", "item", "lot", "Product", "Batch"], hint: "an item number, name or lot", list: "products" },
  { key: "production", label: "Production", types: ["Batch"], hint: "a batch code, lot or product", list: "batches" },
  { key: "lists", label: "Lists", types: [], hint: "a name or any number" },
] as const;
/** What a tab's saved records are called ("Your saved records", not "Your saved all"). */
const SAVED_NOUN: Record<string, string> = { all: "records", production: "batches", lists: "records" };
type Kind = (typeof RECORD_KINDS)[number];

function kindOf(key: string | null): Kind {
  return RECORD_KINDS.find((k) => k.key === key) ?? RECORD_KINDS[0];
}

/** The record type a saved record's key names ("o:<type>/<id>"). */
function savedType(key: string): string | null {
  return /^o:([^/]+)\//.exec(key)?.[1] ?? null;
}

/** Every business view as a list: one picker over the views the server offers, then the list. */
function AllLists({ view, onView }: { view: string; onView: (v: string) => void }) {
  const [views, setViews] = useState<RecordViews["views"] | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    setProblem(null);
    getRecordViews(ctl.signal).then((r) => setViews(r.views), (e) => !ctl.signal.aborted && setProblem(e));
    return () => ctl.abort();
  }, [round]);
  if (problem) return <LoadProblem message={plainError(problem)} onRetry={() => setRound((n) => n + 1)} />;
  if (!views) return <Pending label="Reading the lists" />;
  const current = views.find((v) => v.view === view) ?? views[0];
  if (!current) return <p className="text-[13px] text-muted">No list is available to you yet.</p>;
  return (
    <div>
      <label className="flex flex-col gap-1 text-[12px] text-muted">
        List
        <select aria-label="List" value={current.view} onChange={(e) => onView(e.target.value)} className="h-8 w-64 max-w-full cursor-pointer rounded-md border border-border bg-bg px-2 text-[13px] text-fg">
          {views.map((v) => (
            <option key={v.view} value={v.view}>
              {v.title}
            </option>
          ))}
        </select>
      </label>
      <div className="mt-4">
        <RecordList key={current.view} view={current.view} />
      </div>
    </div>
  );
}

function Records() {
  const params = useSearchParams();
  const router = useRouter();
  const kind = kindOf(params.get("kind"));
  const [q, setQ] = useState(params.get("q") ?? "");
  const [found, setFound] = useState<SuggestResponse | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [slow, setSlow] = useState(false);
  const saved = useSaved();
  const mine = useMemo(() => saved.filter((s) => s.kind === "object" && (kind.key === "all" || (kind.types as readonly string[]).includes(savedType(s.key) ?? ""))), [saved, kind]);
  // what this person opened lately, of the tab's kind (a saved one is listed under Saved only)
  const opened = useRecentRecords();
  const recent = useMemo(() => {
    const savedHrefs = new Set(mine.map((s) => s.href));
    return opened.filter((r) => (kind.key === "all" || (kind.types as readonly string[]).includes(r.type)) && !savedHrefs.has(r.href)).slice(0, 10);
  }, [opened, kind, mine]);
  const query = q.trim();

  useEffect(() => {
    setProblem(null);
    setSlow(false);
    if (!query) {
      setFound(null);
      return;
    }
    const ctl = new AbortController();
    // a search never waits for good: it says it is slow, then gives up with a retry; both timers
    // end the moment the answer (or its failure) arrives
    let slowT: number | undefined;
    let giveUp: number | undefined;
    const settled = () => {
      window.clearTimeout(slowT);
      window.clearTimeout(giveUp);
    };
    const t = window.setTimeout(() => {
      suggest(query, ctl.signal, 20).then(
        (r) => {
          settled();
          setFound(r);
        },
        (e) => {
          settled();
          if (!ctl.signal.aborted) setProblem(e);
        },
      );
    }, 80);
    slowT = window.setTimeout(() => setSlow(true), 8000);
    giveUp = window.setTimeout(() => {
      ctl.abort();
      setProblem(new ApiError(503, "search_busy"));
    }, 20000);
    return () => {
      window.clearTimeout(t);
      settled();
      ctl.abort();
    };
  }, [query, round]);

  // the answer for the words typed so far stays up, dimmed, while the next one is read
  const waiting = !!query && found?.query !== query;
  const rows = (found ? found.suggestions : []).filter((s) => kind.key === "all" || (kind.types as readonly string[]).includes(s.type));
  const elsewhere = waiting ? 0 : (found?.suggestions ?? []).length - rows.length;
  const go = (key: string) => {
    const next = new URLSearchParams(params.toString());
    next.set("kind", key);
    router.replace(`/records?${next.toString()}`);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar heading title="Records" pin={`Records: ${kind.label}`} />
      <PageIntro path="/records" />
      <div className={cn("w-full overflow-y-auto px-6 py-5", "list" in kind || kind.key === "lists" ? "max-w-[1280px]" : "max-w-[880px]")}>
        <div role="tablist" aria-label="Kind of record" className="flex h-9 gap-5 overflow-x-auto whitespace-nowrap border-b border-border">
          {RECORD_KINDS.map((k) => (
            <button
              key={k.key}
              type="button"
              role="tab"
              aria-selected={k.key === kind.key}
              onClick={() => go(k.key)}
              className={cn("-mb-px shrink-0 border-b-2 text-[13px] cursor-pointer", k.key === kind.key ? "border-accent font-medium text-fg" : "border-transparent text-muted hover:text-fg")}
            >
              {k.label}
            </button>
          ))}
        </div>
        <label className="mt-4 block text-[12px] text-muted" htmlFor="records-q">
          Find {kind.hint}
        </label>
        <input
          id="records-q"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            // Enter opens the best match ("106093" opens that PO)
            const top = !waiting && rows[0] && safeHref(rows[0].open_url);
            if (e.key === "Enter" && top) {
              e.preventDefault();
              router.push(top);
            }
          }}
          autoComplete="off"
          spellCheck={false}
          maxLength={100}
          className="mt-1 h-9 w-full rounded-md border border-border bg-bg px-3 text-[14px] outline-none focus-visible:border-accent"
          data-testid="records-search"
        />
        <div className="mt-4" role="tabpanel" aria-label={kind.label}>
          {problem ? (
            <LoadProblem message={plainError(problem)} onRetry={() => setRound((n) => n + 1)} />
          ) : waiting && rows.length === 0 ? (
            <Pending label={slow ? "Still looking. This is taking longer than usual" : `Looking for ${kind.label.toLowerCase()}`} />
          ) : query ? (
            rows.length ? (
              <ul className={cn("divide-y divide-border border-y border-border", waiting && "opacity-60")} aria-busy={waiting} data-testid="records-found">
                {rows.map((r) => (
                  <li key={`${r.type}/${r.object_id}`}>
                    <Link href={safeHref(r.open_url) ?? "/records"} className="flex items-baseline gap-3 py-2 text-[13px] hover:bg-hover">
                      <span className="w-32 shrink-0 text-[12px] text-muted">{typeLabel(r.type)}</span>
                      <span className="min-w-0 flex-1 truncate font-medium">{recordLabel(r.type, r.label)}</span>
                      {r.sublabel && <span className="min-w-0 max-w-[40%] truncate text-[12px] text-muted">{r.sublabel}</span>}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] text-muted" data-testid="records-none">
                {kind.key === "all" ? <>No match for &ldquo;{query}&rdquo;.</> : <>No {kind.label.toLowerCase()} match &ldquo;{query}&rdquo;.</>}
                {" "}Try a different spelling, or a number.
                {elsewhere > 0 && ` ${elsewhere} other ${elsewhere === 1 ? "record matches" : "records match"} under another tab.`}
              </p>
            )
          ) : (
            <>
            {!recent.length && !mine.length && !("list" in kind) && (
              <p className="mb-6 text-[13px] text-muted" data-testid="records-empty-help">
                Type a name or a number above. A customer or supplier by name, an order, PO, invoice or batch by its number; a close spelling is found too. Records you open show up here.
              </p>
            )}
            {"list" in kind && <div className="mb-6"><RecordList key={kind.list} view={kind.list} /></div>}
            {kind.key === "lists" && <AllLists view={params.get("view") ?? ""} onView={(v) => { const next = new URLSearchParams(params.toString()); next.set("view", v); router.replace(`/records?${next.toString()}`); }} />}
            {recent.length > 0 && (
              <section aria-labelledby="records-recent" className="mb-6">
                <h2 id="records-recent" className="text-[13px] font-semibold">
                  Opened lately
                </h2>
                <ul className="mt-2 divide-y divide-border border-y border-border" data-testid="records-recent-list">
                  {recent.map((r) => (
                    <li key={r.href}>
                      <Link href={r.href} className="flex items-baseline justify-between gap-4 py-2 text-[13px] hover:bg-hover">
                        <span className="truncate">{r.label}</span>
                        <span className="shrink-0 text-[12px] text-muted">{formatDateTime(r.at)}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <section aria-labelledby="records-saved">
              <h2 id="records-saved" className="text-[13px] font-semibold">
                Your saved {SAVED_NOUN[kind.key] ?? kind.label.toLowerCase()}
              </h2>
              {mine.length ? (
                <ul className="mt-2 divide-y divide-border border-y border-border" data-testid="records-saved-list">
                  {mine.map((s) => (
                    <li key={s.key}>
                      <Link href={s.href} className="block truncate py-2 text-[13px] hover:bg-hover">
                        {s.label}
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1 text-[13px] text-muted">None saved yet. Open one and press Save to keep it here.</p>
              )}
            </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** Records: customers, orders, suppliers and products, found by name or number, as this reader may see them. */
export default function RecordsPage() {
  return (
    <Suspense>
      <Records />
    </Suspense>
  );
}
