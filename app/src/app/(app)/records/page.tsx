"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { suggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { safeHref, typeLabel } from "@/lib/format";
import { useSaved } from "@/lib/saved";
import type { SuggestResponse } from "@/lib/types";

/** The four kinds of record people look for, and the record types each one holds. */
const RECORD_KINDS = [
  { key: "customers", label: "Customers", types: ["Customer", "customer"], hint: "a customer's name or number" },
  { key: "orders", label: "Orders", types: ["PurchaseOrder", "SalesOrder", "purchase_order", "sales_order", "po", "order"], hint: "a PO or customer order number" },
  { key: "suppliers", label: "Suppliers", types: ["Supplier", "Vendor", "supplier"], hint: "a supplier's name or number" },
  { key: "products", label: "Products", types: ["Item", "Lot", "item", "lot"], hint: "an item number, name or lot" },
] as const;
type Kind = (typeof RECORD_KINDS)[number];

function kindOf(key: string | null): Kind {
  return RECORD_KINDS.find((k) => k.key === key) ?? RECORD_KINDS[0];
}

/** The record type a saved record's key names ("o:<type>/<id>"). */
function savedType(key: string): string | null {
  return /^o:([^/]+)\//.exec(key)?.[1] ?? null;
}

function Records() {
  const params = useSearchParams();
  const router = useRouter();
  const kind = kindOf(params.get("kind"));
  const [q, setQ] = useState(params.get("q") ?? "");
  const [found, setFound] = useState<SuggestResponse | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const saved = useSaved();
  const mine = useMemo(() => saved.filter((s) => s.kind === "object" && (kind.types as readonly string[]).includes(savedType(s.key) ?? "")), [saved, kind]);
  const query = q.trim();

  useEffect(() => {
    setProblem(null);
    if (!query) {
      setFound(null);
      return;
    }
    const ctl = new AbortController();
    const t = window.setTimeout(() => {
      suggest(query, ctl.signal, 20).then(setFound, (e) => !ctl.signal.aborted && setProblem(e));
    }, 200);
    return () => {
      window.clearTimeout(t);
      ctl.abort();
    };
  }, [query, round]);

  const rows = (found?.query === query ? found.suggestions : []).filter((s) => (kind.types as readonly string[]).includes(s.type));
  const elsewhere = (found?.query === query ? found.suggestions : []).length - rows.length;
  const go = (key: string) => {
    const next = new URLSearchParams(params.toString());
    next.set("kind", key);
    router.replace(`/records?${next.toString()}`);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar heading title="Records" pin={`Records: ${kind.label}`} />
      <div className="w-full max-w-[880px] overflow-y-auto px-6 py-5">
        <div role="tablist" aria-label="Kind of record" className="flex h-9 gap-5 border-b border-border">
          {RECORD_KINDS.map((k) => (
            <button
              key={k.key}
              type="button"
              role="tab"
              aria-selected={k.key === kind.key}
              onClick={() => go(k.key)}
              className={cn("-mb-px border-b-2 text-[13px] cursor-pointer", k.key === kind.key ? "border-accent font-medium text-fg" : "border-transparent text-muted hover:text-fg")}
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
          autoComplete="off"
          spellCheck={false}
          maxLength={100}
          className="mt-1 h-9 w-full rounded-md border border-border bg-bg px-3 text-[14px] outline-none focus-visible:border-accent"
          data-testid="records-search"
        />
        <div className="mt-4" role="tabpanel" aria-label={kind.label}>
          {problem ? (
            <LoadProblem message={plainError(problem)} onRetry={() => setRound((n) => n + 1)} />
          ) : query && found?.query !== query ? (
            <Pending label={`Looking for ${kind.label.toLowerCase()}`} />
          ) : query ? (
            rows.length ? (
              <ul className="divide-y divide-border border-y border-border" data-testid="records-found">
                {rows.map((r) => (
                  <li key={`${r.type}/${r.object_id}`}>
                    <Link href={safeHref(r.open_url) ?? "/records"} className="flex items-baseline gap-3 py-2 text-[13px] hover:bg-hover">
                      <span className="w-32 shrink-0 text-[12px] text-muted">{typeLabel(r.type)}</span>
                      <span className="min-w-0 flex-1 truncate font-medium">{r.label}</span>
                      {r.sublabel && <span className="min-w-0 max-w-[40%] truncate text-[12px] text-muted">{r.sublabel}</span>}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-[13px] text-muted" data-testid="records-none">
                No {kind.label.toLowerCase()} you can see match &ldquo;{query}&rdquo;.
                {elsewhere > 0 && ` ${elsewhere} other ${elsewhere === 1 ? "record matches" : "records match"} under another tab.`}
              </p>
            )
          ) : (
            <section aria-labelledby="records-saved">
              <h2 id="records-saved" className="text-[13px] font-semibold">
                Your saved {kind.label.toLowerCase()}
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
