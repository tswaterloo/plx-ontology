"use client";

import { Bookmark, ChevronDown, Clock3, Loader2, Search, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
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

const RECORD_KINDS = [
  { key: "orders", label: "Orders", types: ["PurchaseOrder", "SalesOrder", "purchase_order", "sales_order", "po", "order"], hint: "a PO or customer order number", placeholder: "Search POs or sales orders by number, supplier, or customer...", list: "purchase_orders" },
  { key: "customers", label: "Customers", types: ["Customer", "customer", "Chain"], hint: "a customer's name or number", placeholder: "Search customers by name or number...", list: "customers" },
  { key: "suppliers", label: "Suppliers", types: ["Supplier", "Vendor", "supplier"], hint: "a supplier's name or number", placeholder: "Search suppliers by name or code...", list: "suppliers" },
  { key: "products", label: "Products", types: ["Item", "Lot", "item", "lot", "Product", "Batch"], hint: "an item number, name or lot", placeholder: "Search products by item code, name, or lot...", list: "products" },
  { key: "documents", label: "Documents", types: ["SalesInvoice", "SupplierInvoice", "Shipment", "Receipt", "SalesCreditMemo", "PurchaseInvoice"], hint: "an invoice, shipment, receipt or credit note number", placeholder: "Search invoices, shipments, receipts, or credit notes...", list: "sales_invoices" },
  { key: "production", label: "Production", types: ["Batch"], hint: "a batch code, lot or product", placeholder: "Search batches by code, lot, or product...", list: "batches" },
  { key: "lists", label: "All lists", types: [], hint: "a name or any number", placeholder: "Search customers, POs, invoices, products, batches..." },
] as const;

type Kind = (typeof RECORD_KINDS)[number];

function kindOf(key: string | null): Kind {
  if (!key || key === "all") return RECORD_KINDS[0];
  return RECORD_KINDS.find((k) => k.key === key) ?? RECORD_KINDS[0];
}

function savedType(key: string): string | null {
  return /^o:([^/]+)\//.exec(key)?.[1] ?? null;
}

function AllLists({ view, onView }: { view: string; onView: (v: string) => void }) {
  const { data, error, retry, at } = useRead<RecordViews>("/api/records/views", getRecordViews);
  const views = data?.views ?? null;
  if (error) return <LoadProblem message={plainError(error)} onRetry={retry} />;
  if (!views) return <Pending label="Reading the lists" />;
  const current = views.find((v) => v.view === view) ?? views[0];
  if (!current) {
    return <p className="text-[13.5px] text-muted">No list is available to you yet.</p>;
  }
  return (
    <div className="space-y-4">
      <StoredAge at={at} />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex flex-wrap items-center gap-2.5 text-[12.5px] font-medium text-muted">
          <span>Business view</span>
          <span className="relative inline-block w-72 max-w-full">
            <select
              aria-label="List"
              value={current.view}
              onChange={(e) => onView(e.target.value)}
              className="h-8 w-full appearance-none cursor-pointer rounded-md border border-border bg-bg pl-3 pr-8 text-[13px] font-medium text-fg outline-none transition-colors hover:border-border-strong focus-visible:border-border-strong"
            >
              {views.map((v) => (
                <option key={v.view} value={v.view} className="bg-bg text-fg">
                  {v.title}
                </option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted" aria-hidden />
          </span>
        </label>
        <span className="num text-[12px] text-faint">{views.length} views available</span>
      </div>
      <RecordList key={current.view} view={current.view} />
    </div>
  );
}

function RecordsWorkspaceInner() {
  const params = useSearchParams();
  const router = useRouter();
  const path = usePathname() || "/records";
  const kind = kindOf(params.get("kind"));
  const ordersMode = kind.key === "orders" && params.get("list") === "sales_orders" ? "sales_orders" : "purchase_orders";
  const listView = kind.key === "orders" ? ordersMode : "list" in kind ? kind.list : "";
  const [q, setQ] = useState(params.get("q") ?? "");
  const searchRef = useRef<HTMLInputElement>(null);
  const [found, setFound] = useState<SuggestResponse | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [slow, setSlow] = useState(false);

  const saved = useSaved();
  const mine = useMemo(
    () => saved.filter((s) => s.kind === "object" && (kind.key === "lists" || (kind.types as readonly string[]).includes(savedType(s.key) ?? ""))),
    [saved, kind],
  );
  const opened = useRecentRecords();
  const recent = useMemo(() => {
    const savedHrefs = new Set(mine.map((s) => s.href));
    return opened.filter((r) => (kind.key === "lists" || (kind.types as readonly string[]).includes(r.type)) && !savedHrefs.has(r.href));
  }, [opened, kind, mine]);
  const hasRecentOrSaved = recent.length > 0 || mine.length > 0;
  const query = q.trim();

  useEffect(() => {
    setProblem(null);
    setSlow(false);
    if (!query) {
      setFound(null);
      return;
    }
    const ctl = new AbortController();
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

  const waiting = !!query && found?.query !== query;
  const rows = (found?.suggestions ?? []).filter(
    (s) => kind.key === "lists" || (kind.types as readonly string[]).includes(s.type),
  );
  const elsewhere = waiting ? 0 : (found?.suggestions ?? []).length - rows.length;

  const go = (key: string, extra?: Record<string, string | null>) => {
    const next = new URLSearchParams(params.toString());
    next.set("kind", key);
    if (key !== "orders") next.delete("list");
    if (extra) {
      for (const [k, v] of Object.entries(extra)) {
        if (v === null) next.delete(k);
        else next.set(k, v);
      }
    }
    router.replace(`${path}?${next.toString()}`);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg" data-testid="records-explorer">
      <Toolbar heading title="Data" pin={`Data: ${kind.label}`} />

      {/* Single Crisp Top Row: Liquid Glass Category Tabs + Purchase/Sales Orders Toggle */}
      <div className="sticky top-12 z-10 border-b border-border bg-bg/95 py-2 backdrop-blur-sm">
        <div className="mx-auto flex w-full max-w-[1440px] flex-wrap items-center justify-between gap-2.5 px-4 sm:px-6">
          <div
            role="tablist"
            aria-label="Kind of record"
            className="liquid-glass-pill flex items-center gap-0.5 overflow-x-auto rounded-lg p-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          >
            {RECORD_KINDS.map((k) => {
              const active = k.key === kind.key;
              return (
                <button
                  key={k.key}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => go(k.key)}
                  className={cn(
                    "inline-flex h-7 shrink-0 items-center rounded-md px-3 text-[12.5px] font-medium whitespace-nowrap transition-all cursor-pointer",
                    active ? "liquid-glass-active" : "text-muted hover:text-fg",
                  )}
                >
                  {k.label}
                </button>
              );
            })}
          </div>

          {kind.key === "orders" && (
            <div
              role="group"
              aria-label="Which orders"
              className="liquid-glass-pill inline-flex shrink-0 items-center gap-0.5 rounded-lg p-0.5 text-[12px]"
            >
              {([
                ["purchase_orders", "Purchase orders"],
                ["sales_orders", "Sales orders"],
              ] as const).map(([v, label]) => (
                <button
                  key={v}
                  type="button"
                  aria-pressed={ordersMode === v}
                  onClick={() => go("orders", { list: v === "sales_orders" ? "sales_orders" : null })}
                  className={cn(
                    "h-7 cursor-pointer rounded-md px-2.5 font-medium transition-all",
                    ordersMode === v ? "liquid-glass-active" : "text-muted hover:text-fg",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <PageIntro path="/records" className="mx-auto w-full max-w-[1440px] px-4 sm:px-6" />

      <div className="mx-auto w-full max-w-[1440px] flex-1 space-y-4 overflow-y-auto px-4 py-4 sm:px-6">
        {/* Streamlined Search Bar */}
        <div className="max-w-xl">
          <label className="sr-only" htmlFor="records-q">
            Find {kind.hint}
          </label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
            <input
              ref={searchRef}
              id="records-q"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                const top = !waiting && rows[0] && safeHref(rows[0].open_url);
                if (e.key === "Enter" && top) {
                  e.preventDefault();
                  router.push(top);
                } else if (e.key === "Escape" && q) {
                  e.preventDefault();
                  setQ("");
                }
              }}
              placeholder={kind.placeholder}
              autoComplete="off"
              spellCheck={false}
              maxLength={100}
              className="h-8.5 w-full rounded-md border border-border bg-bg pl-9 pr-8 text-[13px] text-fg outline-none transition-colors duration-150 hover:border-border-strong focus-visible:border-border-strong placeholder:text-faint"
              data-testid="records-search"
            />
            {waiting && (
              <Loader2 className={cn("pointer-events-none absolute top-1/2 size-3.5 -translate-y-1/2 animate-spin text-muted", q ? "right-8" : "right-2.5")} aria-hidden />
            )}
            {q && (
              <button
                type="button"
                onClick={() => {
                  setQ("");
                  searchRef.current?.focus();
                }}
                aria-label="Clear search"
                title="Clear search"
                data-testid="records-search-clear"
                className="absolute right-1.5 top-1/2 inline-flex size-5 -translate-y-1/2 cursor-pointer items-center justify-center rounded text-muted transition-colors hover:bg-hover hover:text-fg"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            )}
          </div>
        </div>

        {/* Compact Recently Opened & Saved Strip — rendered ONLY when non-empty */}
        {hasRecentOrSaved && !query && (
          <div
            data-testid="records-recent-saved-strip"
            className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-y border-border/70 py-2 text-[12px]"
          >
            {recent.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5" data-testid="records-recent-list">
                <span className="inline-flex items-center gap-1 font-medium text-faint">
                  <Clock3 className="size-3" aria-hidden />
                  Recent
                </span>
                {recent.slice(0, 5).map((r) => (
                  <Link
                    key={r.href}
                    href={r.href}
                    title={`${r.label} · ${formatDateTime(r.at)}`}
                    className="inline-flex h-6 max-w-[180px] items-center truncate rounded-md border border-border/80 bg-surface/50 px-2 font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover"
                  >
                    {r.label}
                  </Link>
                ))}
              </div>
            )}
            {mine.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5" data-testid="records-saved-list">
                <span className="inline-flex items-center gap-1 font-medium text-faint">
                  <Bookmark className="size-3" aria-hidden />
                  Saved
                </span>
                {mine.slice(0, 5).map((s) => (
                  <Link
                    key={s.key}
                    href={s.href}
                    title={s.label}
                    className="inline-flex h-6 max-w-[180px] items-center truncate rounded-md border border-border/80 bg-surface/50 px-2 font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover"
                  >
                    {s.label}
                  </Link>
                ))}
              </div>
            )}
          </div>
        )}

        <div role="tabpanel" aria-label={kind.label} className="space-y-4">
          {problem ? (
            <LoadProblem message={plainError(problem)} onRetry={() => setRound((n) => n + 1)} />
          ) : waiting && rows.length === 0 ? (
            <Pending label={slow ? "Still looking. This is taking longer than usual" : `Looking for ${kind.label.toLowerCase()}`} />
          ) : query ? (
            rows.length ? (
              <section className="overflow-hidden rounded-lg border border-border/80 bg-surface/50">
                <div className="flex items-center justify-between border-b border-border px-3.5 py-2 text-[12px]">
                  <span className="font-medium text-fg">
                    Direct matches <span className="num ml-1 font-normal text-muted">{rows.length}</span>
                  </span>
                  <span className="text-faint max-sm:hidden">Press Enter to open top match</span>
                </div>
                <ul className={cn("divide-y divide-border/70", waiting && "opacity-60")} aria-busy={waiting} data-testid="records-found">
                  {rows.map((r) => (
                    <li key={`${r.type}/${r.object_id}`}>
                      <Link
                        href={safeHref(r.open_url) ?? "/records"}
                        className="flex items-baseline gap-3 px-3.5 py-2 text-[13px] transition-colors duration-150 hover:bg-hover"
                      >
                        <span className="w-28 shrink-0 text-[12px] text-faint">{typeLabel(r.type)}</span>
                        <span className="min-w-0 flex-1 truncate font-medium text-fg" title={recordLabel(r.type, r.label)}>
                          {recordLabel(r.type, r.label)}
                        </span>
                        {r.sublabel && (
                          <span className="min-w-0 max-w-[40%] truncate text-[12px] text-muted" title={r.sublabel}>
                            {r.sublabel}
                          </span>
                        )}
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            ) : (
              <div className="flex flex-wrap items-center gap-3 py-2">
                <p className="text-[13px] text-muted" data-testid="records-none">
                  No {kind.label.toLowerCase()} match &ldquo;{query}&rdquo;. Try a different spelling, or a number.
                  {elsewhere > 0 && ` ${elsewhere} other ${elsewhere === 1 ? "record matches" : "records match"} under another tab.`}
                </p>
                {elsewhere > 0 && kind.key !== "lists" && (
                  <button
                    type="button"
                    onClick={() => go("lists")}
                    className="inline-flex h-7 cursor-pointer items-center rounded-md bg-fg px-2.5 text-[12px] font-medium text-bg transition-opacity hover:opacity-90"
                  >
                    Search all records
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    setQ("");
                    searchRef.current?.focus();
                  }}
                  className="inline-flex h-7 cursor-pointer items-center rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-hover"
                >
                  Clear search
                </button>
              </div>
            )
          ) : null}

          {/* Orders default to newest/active orders (server native newest date desc, NEVER days_late desc) */}
          {listView && (
            <RecordList
              key={listView}
              view={listView}
              externalQuery={q}
              onClearExternalQuery={() => setQ("")}
            />
          )}

          {kind.key === "lists" && !query && (
            <AllLists
              view={params.get("view") ?? ""}
              onView={(v) => {
                const next = new URLSearchParams(params.toString());
                next.set("view", v);
                router.replace(`${path}?${next.toString()}`);
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

export function RecordsWorkspace() {
  return (
    <Suspense>
      <RecordsWorkspaceInner />
    </Suspense>
  );
}
