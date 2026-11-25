"use client";

// The lists people look things up in: Orders, Customers, Suppliers, Products, and More (documents,
// batches, every list). One component; each route passes its place (a place with no tab strip of
// its own opens on its list), and with no place it keeps every kind as tabs.

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StoredAge } from "@/components/ui/stored-age";
import { RecordList } from "@/components/record-list";
import { getRecordViews, suggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime, recordLabel, safeHref, typeLabel } from "@/lib/format";
import { useRecentRecords, useSaved } from "@/lib/saved";
import { useRead } from "@/lib/use-read";
import type { RecordViews, SuggestResponse } from "@/lib/types";

/** Every record ("All", the first tab), then the four kinds people look for and the record types each one holds. */
const RECORD_KINDS = [
  { key: "all", label: "All", types: [], hint: "a name or number" },
  { key: "customers", label: "Customers", types: ["Customer", "customer", "Chain"], hint: "a customer's name or number", list: "customers" },
  { key: "orders", label: "Orders", types: ["PurchaseOrder", "SalesOrder", "purchase_order", "sales_order", "po", "order"], hint: "a PO or customer order number", list: "purchase_orders", sort: { key: "days_late", direction: "desc" } },
  { key: "suppliers", label: "Suppliers", types: ["Supplier", "Vendor", "supplier"], hint: "a supplier's name or number", list: "suppliers" },
  { key: "documents", label: "Documents", types: ["SalesInvoice", "SupplierInvoice", "Shipment", "Receipt", "SalesCreditMemo", "PurchaseInvoice"], hint: "an invoice, shipment, receipt or credit note number", list: "sales_invoices" },
  { key: "products", label: "Products", types: ["Item", "Lot", "item", "lot", "Product", "Batch"], hint: "an item number, name or lot", list: "products" },
  { key: "production", label: "Production", types: ["Batch"], hint: "a batch code, lot or product", list: "batches" },
  { key: "lists", label: "Lists", types: [], hint: "a name or any number" },
] as const;
/** What a tab's saved records are called ("Your saved records", not "Your saved all"). */
const SAVED_NOUN: Record<string, string> = { all: "records", production: "batches", lists: "records" };
type Kind = (typeof RECORD_KINDS)[number];
export type Place = "orders" | "customers" | "suppliers" | "products" | "more";
/** The kinds a place offers as tabs: only More has any (the four others open straight on their list). */
const MORE_KINDS = ["documents", "production", "lists", "all"];

function kindOf(key: string | null): Kind {
  return RECORD_KINDS.find((k) => k.key === key) ?? RECORD_KINDS[0];
}

/** The record type a saved record's key names ("o:<type>/<id>"). */
function savedType(key: string): string | null {
  return /^o:([^/]+)\//.exec(key)?.[1] ?? null;
}

/** Every business view as a list: one picker over the views the server offers, then the list. */
function AllLists({ view, onView }: { view: string; onView: (v: string) => void }) {
  const { data, error, retry, at } = useRead<RecordViews>("/api/records/views", getRecordViews);
  const views = data?.views ?? null;
  if (error) return <LoadProblem message={plainError(error)} onRetry={retry} />;
  if (!views) return <Pending label="Reading the lists" />;
  const current = views.find((v) => v.view === view) ?? views[0];
  if (!current) return <p className="text-[13px] text-muted">No list is available to you yet.</p>;
  return (
    <div>
      <StoredAge at={at} />
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

const BROWSE_CARDS: { key: string; label: string; description: string }[] = [
  { key: "orders", label: "Orders", description: "Purchase orders, customer orders, due dates and late status." },
  { key: "customers", label: "Customers", description: "Accounts, open balances, orders and invoice history." },
  { key: "suppliers", label: "Suppliers", description: "Supplier accounts, open purchase orders and delivery history." },
  { key: "products", label: "Products", description: "Items, SKUs, lots and pricing records." },
  { key: "documents", label: "Documents", description: "Sales invoices, receipts, shipments and credit notes." },
  { key: "production", label: "Production", description: "Manufacturing batches, quality holds and lot status." },
  { key: "lists", label: "All lists", description: "Browse every table and business view available to you." },
];

function Records({ place }: { place?: Place }) {
  const params = useSearchParams();
  const router = useRouter();
  const path = usePathname() || "/records";
  const kind = kindOf(place && place !== "more" ? place : params.get("kind") ?? (place === "more" ? "documents" : null));
  const salesOrders = place === "orders" && params.get("list") === "sales_orders";
  const listView = salesOrders ? "sales_orders" : "list" in kind ? kind.list : "";
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
    return opened.filter((r) => (kind.key === "all" || (kind.types as readonly string[]).includes(r.type)) && !savedHrefs.has(r.href));
  }, [opened, kind, mine]);
  const [allRecent, setAllRecent] = useState(false);
  const recentShown = allRecent ? recent : recent.slice(0, 10);  // guard-ok: the count and a Show all are drawn beside the list
  const query = q.trim();

  useEffect(() => {
    setProblem(null);
    setSlow(false);
    if (!query) {
      setFound(null);
      return;
    }
    const ctl = new AbortController();
    // a search says it is slow and keeps waiting (it never gives up); the timer ends the moment
    // the answer (or its failure) arrives
    let slowT: number | undefined;
    const settled = () => window.clearTimeout(slowT);
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
    router.replace(`${path}?${next.toString()}`);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar heading title={place ? PLACE_TITLE[place] : "Data"} pin={`${place ? PLACE_TITLE[place] : "Data"}: ${kind.label}`} />
      <PageIntro path={place ? `/${place}` : "/records"} />
      <div className={cn("w-full overflow-y-auto py-5", "list" in kind || kind.key === "lists" ? "max-w-[1440px] px-5" : "max-w-[880px] px-6")}>
        {(!place || place === "more") && <div role="tablist" aria-label="Kind of record" className="flex h-9 gap-5 overflow-x-auto whitespace-nowrap border-b border-border">
          {RECORD_KINDS.filter((k) => !place || MORE_KINDS.includes(k.key)).map((k) => (
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
        </div>}
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
            {kind.key === "all" && (
              <section aria-labelledby="records-browse-heading" className="mb-6" data-testid="records-browse">
                <h2 id="records-browse-heading" className="text-[13px] font-semibold">
                  Browse by record type
                </h2>
                <div className="mt-2 grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
                  {BROWSE_CARDS.map((card) => (
                    <button
                      key={card.key}
                      type="button"
                      onClick={() => go(card.key)}
                      className="flex flex-col items-start rounded-md border border-border bg-bg p-3 text-left transition-colors hover:border-border-strong hover:bg-hover cursor-pointer"
                    >
                      <span className="text-[13.5px] font-medium text-fg">{card.label}</span>
                      <span className="mt-0.5 text-[12px] leading-4 text-muted">{card.description}</span>
                    </button>
                  ))}
                </div>
              </section>
            )}
            {recent.length > 0 && (
              <section aria-labelledby="records-recent" className="mb-6">
                <h2 id="records-recent" className="text-[13px] font-semibold">
                  Opened lately{recent.length > recentShown.length ? ` (${recentShown.length} of ${recent.length})` : ""}
                </h2>
                <ul className="mt-2 divide-y divide-border border-y border-border" data-testid="records-recent-list">
                  {recentShown.map((r) => (
                    <li key={r.href}>
                      <Link href={r.href} className="flex items-baseline justify-between gap-4 py-2 text-[13px] hover:bg-hover">
                        <span className="truncate">{r.label}</span>
                        <span className="shrink-0 text-[12px] text-muted">{formatDateTime(r.at)}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
                {recent.length > recentShown.length && (
                  <button type="button" className="mt-2 text-[12px] text-muted underline hover:text-fg" onClick={() => setAllRecent(true)}>
                    Show all {recent.length}
                  </button>
                )}
              </section>
            )}
            {(mine.length > 0 || !listView) && <section aria-labelledby="records-saved" className="mb-6">
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
            </section>}
            {place === "orders" && (
              <div role="group" aria-label="Which orders" className="mb-3 flex gap-4 text-[13px]">
                {[["purchase_orders", "Purchase orders (to suppliers)"], ["sales_orders", "Sales orders (from customers)"]].map(([v, label]) => (
                  <Link key={v} href={v === "sales_orders" ? `${path}?list=sales_orders` : path} replace aria-current={listView === v ? "page" : undefined} className={cn("pb-0.5", listView === v ? "border-b-2 border-accent font-medium text-fg" : "text-muted hover:text-fg")}>{label}</Link>
                ))}
              </div>
            )}
            {listView && (
              <div className="mb-6">
                <RecordList
                  key={listView}
                  view={listView}
                  initialSort={listView === "purchase_orders" && "sort" in kind ? kind.sort : undefined}
                  externalQuery={q}
                  onClearExternalQuery={() => setQ("")}
                />
              </div>
            )}
            {kind.key === "lists" && <AllLists view={params.get("view") ?? ""} onView={(v) => { const next = new URLSearchParams(params.toString()); next.set("view", v); router.replace(`${path}?${next.toString()}`); }} />}
            </>
          )}
          {query && listView && (
            <div className="mt-5">
              <RecordList
                key={listView}
                view={listView}
                initialSort={listView === "purchase_orders" && "sort" in kind ? kind.sort : undefined}
                externalQuery={q}
                onClearExternalQuery={() => setQ("")}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Records: customers, orders, suppliers and products, found by name or number, as this reader may see them. */
const PLACE_TITLE: Record<Place, string> = { orders: "Orders", customers: "Customers", suppliers: "Suppliers", products: "Products", more: "More" };

export function RecordsView({ place }: { place?: Place }) {
  return (
    <Suspense>
      <Records place={place} />
    </Suspense>
  );
}
