"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { dataHealthExportHref, getAlertKinds, getBriefing, getBriefingItems, getDataHealth, getDataHealthPage, readPath } from "@/lib/api";
import { cn } from "@/lib/cn";
import { eastern, isBeforeToday, objectHref, readable, recordLabel, safeHref, typeLabel } from "@/lib/format";
import { noticeKind, plainError } from "@/lib/errors";
import type { AlertKinds, AlertType, Briefing, BriefingItem, DataHealth } from "@/lib/types";

/** How a type's default reads: on, or off with its measurement. */
export function standing(t: Pick<AlertType, "default" | "precision" | "home">): string {
  const p = t.precision;
  const measured = p ? `${Math.round(p.value * 100)}% of ${p.checked} checked` : "not measured";
  if (t.default === "on") return t.home ? `On, on Home · ${measured}` : `On · ${measured}`;
  return `Off · ${measured}`;
}

/** Whether a kind is on (any of its types on by default) and, for a kind that shows on a
 * page of its own, that page (not repeated when the kind's own name is that page). */
export function surfaceWord(k: { label?: string; title?: string; kind?: string; surface: string; types: Pick<AlertType, "default">[] }): string {
  const on = (k.types ?? []).some((t) => t.default === "on") ? "On" : "Off";
  if (k.surface === "alert") return on;
  const where = k.surface === "weekly" ? "Weekly review" : "Data health";
  const label = (k?.label ?? k?.title ?? k?.kind ?? "").trim().toLowerCase();
  return where.toLowerCase() === label ? on : `${on} · ${where}`;
}

/** Where a kind with a page of its own is shown (null: an alert, shown here). */
export function surfaceHref(surface: string): string | null {
  return surface === "data_health" ? "/data-health" : surface === "weekly" ? "/meetings?view=weekly" : null;
}

/**
 * Data health: every check the registry files under data health (customers or suppliers
 * entered twice, key fields missing, orders without a customer PO, records out of step with
 * their other system) with the findings this reader may see; a check whose findings are on
 * records they may not open says how many are hidden, never "None". The checks that are off
 * are named once under them. A read that failed is said, never zeros.
 */
export function DataHealthSection({ titled = true }: { titled?: boolean }) {
  const [data, setData] = useState<DataHealth | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setData(null);
    getDataHealth(ctl.signal).then(setData, (e) => !ctl.signal.aborted && setError(e));
    return () => ctl.abort();
  }, [round]);
  const retry = () => setRound((n) => n + 1);
  const on = (data?.checks ?? []).filter((c) => c.on);
  const off = (data?.checks ?? []).filter((c) => !c.on);
  return (
    <section aria-labelledby="data-health-title" id="data-health" className="scroll-mt-16" data-testid="data-health">
      {/* on its own Admin page the page title and its one sentence say this already */}
      <h2 id="data-health-title" className={titled ? "text-[15px] font-semibold" : "sr-only"}>Data health</h2>
      {data?.status === "read" && on.some((c) => (c.read !== false ? c.count : c.open ?? 0) > 0) && (
        <p className="mt-1 text-[12px]">
          <a href={dataHealthExportHref()} className="text-accent hover:underline" download>Download all as a spreadsheet</a>
        </p>
      )}
      {titled && <p className="mt-0.5 text-[12px] text-muted">Records entered twice, missing a key field or out of step with their other system, as you may read them.</p>}
      {error ? (
        <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} className="mt-2" />
      ) : !data ? (
        <Pending label="Reading data health" />
      ) : data.status === "not_configured" ? (
        <p className="mt-2 text-[13px] text-muted">The data-health checks are not set up on this server.</p>
      ) : data.status !== "read" ? (
        <LoadProblem message="The checks couldn't be read just now. Try again in a few minutes." onRetry={retry} className="mt-2" />
      ) : on.length === 0 ? (
        <p className="mt-2 text-[13px] text-muted">No data-health check is on.</p>
      ) : (
        <ul className="mt-2 divide-y divide-border border-y border-border" data-testid="data-health-checks">
          {on.map((c) => {
            const checkLabel = c?.label ?? (c as { title?: string })?.title ?? c?.type ?? "";
            return (
              <li key={c.type} className="py-2 text-[13px]" data-testid={`data-health-${c.type}`}>
                <div className="flex items-baseline gap-3">
                  <span className="min-w-0 flex-1 font-medium">{checkLabel}</span>
                  <span className="shrink-0 tabular-nums text-muted" data-testid={`data-health-count-${c.type}`}>
                    {c.read === false ? (c.open === null || c.open === undefined ? "Not checked" : c.open ? c.open.toLocaleString("en-US") : "None") : c.count > 0 ? `${c.count.toLocaleString("en-US")}${data.partial ? "+" : ""}` : c.hidden > 0 ? "Hidden from you" : "None"}
                  </span>
                </div>
                {c.read === false && (c.open === null || c.open === undefined) && (
                  <p className="mt-0.5 text-[12px] text-muted" data-testid={`data-health-unchecked-${c.type}`}>This check has not run yet, so there is no count.</p>
                )}
                {c.read === false && isBeforeToday(c.checked_at) && (
                  <p className="mt-0.5 text-[12px] text-muted">Last checked {eastern(c.checked_at)}.</p>
                )}
                {c.hidden > 0 && (
                  <p className="mt-0.5 text-[12px] text-muted" data-testid={`data-health-hidden-${c.type}`}>
                    {c.count > 0 ? "Also " : ""}{c.hidden} on records your access doesn&rsquo;t open.
                  </p>
                )}
                {(c.read !== false ? (c.items ?? []).length > 0 : (c.open ?? 0) > 0) && <CheckItems check={c} />}
              </li>
            );
          })}
        </ul>
      )}
      {off.length > 0 && (
        <p className="mt-2 text-[12px] text-faint" data-testid="data-health-off">
          Not shown until they are checked: {off.map((c) => (c?.label ?? (c as { title?: string })?.title ?? c?.type ?? "").toLowerCase()).filter(Boolean).join("; ")}.
        </p>
      )}
    </section>
  );
}

/** A check's findings: the first read's, then a page at a time while there are more (each page
 * is the next findings of that check only, as this reader may see them); supports search,
 * record-type and assignment filters across all rows, exact Showing X-Y of N counts, and
 * spreadsheet export. Nothing is listed twice and a failed page says so and can be retried. */
type Check = NonNullable<DataHealth["checks"]>[number];
type Finding = NonNullable<Check["items"]>[number];
function CheckItems({ check, onRead }: { check: Check; onRead?: (c: Check) => void }) {
  const [items, setItems] = useState<Finding[]>(check.items ?? []);
  const [more, setMore] = useState(check.more);
  const [total, setTotal] = useState<number | null>(check.read !== false ? check.count : (check.open ?? null));
  const [read, setRead] = useState(check.read !== false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [assigned, setAssigned] = useState<Record<string, string>>({});
  const [queryInput, setQueryInput] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [objectType, setObjectType] = useState("");
  const [assigneeFilter, setAssigneeFilter] = useState<"all" | "unassigned" | "assigned">("all");
  const [knownTypes, setKnownTypes] = useState<string[]>(() =>
    Array.from(new Set((check.items ?? []).map((i) => i.object_type).filter(Boolean))).sort(),
  );

  // the page's first read counts only; a check's findings are read when it is opened
  const load = (from: number, qStr = appliedQuery, typeStr = objectType) => {
    setBusy(true);
    setFailed(false);
    const trimmedQ = qStr.trim();
    const trimmedType = typeStr.trim();
    const req = trimmedQ || trimmedType
      ? (() => {
          const qs = new URLSearchParams({
            check: check.type,
            offset: String(Math.max(0, Math.floor(from))),
            limit: "50",
          });
          if (trimmedQ) qs.set("q", trimmedQ);
          if (trimmedType) qs.set("object_type", trimmedType);
          return readPath<DataHealth>(`/api/data-health?${qs.toString()}`);
        })()
      : getDataHealthPage(check.type, from);
    req.then(
      (r) => {
        const c = (r.checks ?? []).find((x) => x.type === check.type);
        const seen = new Set(from === 0 ? [] : items.map((i) => i.id));
        const fresh = (c?.items ?? []).filter((i) => !seen.has(i.id));
        const nextItems = from === 0 ? fresh : [...items, ...fresh];
        setItems(nextItems);
        setMore(fresh.length === 0 ? 0 : (c?.more ?? 0));
        if (c) {
          setTotal(c.count);
          setRead(true);
          onRead?.(c);
          const types = Array.from(new Set([...knownTypes, ...nextItems.map((i) => i.object_type).filter(Boolean)])).sort();
          if (types.length !== knownTypes.length) setKnownTypes(types);
        }
      },
      () => setFailed(true),
    ).finally(() => setBusy(false));
  };

  const applyFilters = (nextQ: string, nextType: string) => {
    setAppliedQuery(nextQ);
    setObjectType(nextType);
    load(0, nextQ, nextType);
  };

  const visibleItems = useMemo(() => {
    if (assigneeFilter === "assigned") return items.filter((it) => Boolean(assigned[it.id]));
    if (assigneeFilter === "unassigned") return items.filter((it) => !assigned[it.id]);
    return items;
  }, [items, assigned, assigneeFilter]);

  const exportHref = useMemo(() => {
    const base = dataHealthExportHref(check.type);
    const trimmedQ = appliedQuery.trim();
    const trimmedType = objectType.trim();
    if (!trimmedQ && !trimmedType) return base;
    const qs = new URLSearchParams({ check: check.type });
    if (trimmedQ) qs.set("q", trimmedQ);
    if (trimmedType) qs.set("object_type", trimmedType);
    return `/api/data-health/export?${qs.toString()}`;
  }, [check.type, appliedQuery, objectType]);

  const hasFilter = Boolean(appliedQuery.trim() || objectType.trim() || assigneeFilter !== "all");
  const checkLabel = check?.label ?? (check as { title?: string })?.title ?? check?.type ?? "data health";
  const exactTotal = total ?? items.length;
  const seen = check.read !== false ? check.count : null;
  return (
    <details className="mt-1 text-[12px]" onToggle={(e) => { if ((e.currentTarget as HTMLDetailsElement).open && !read && !busy) load(0); }}>
      <summary className="cursor-pointer font-medium">{!read ? "Show them" : check.count === 1 ? "Show it" : `Show ${more > 0 ? `the first ${items.length}` : `the ${seen ?? items.length}`}`}</summary>
      {busy && !read && <p className="mt-1 text-muted">Reading</p>}
      {read && (
        <div className="mt-2 flex flex-wrap items-center gap-2" data-testid={`data-health-filters-${check.type}`}>
          <form
            className="flex items-center gap-1.5"
            onSubmit={(e) => {
              e.preventDefault();
              applyFilters(queryInput, objectType);
            }}
          >
            <input
              type="search"
              value={queryInput}
              onChange={(e) => setQueryInput(e.target.value)}
              placeholder="Filter findings"
              aria-label={`Filter ${checkLabel} findings`}
              className="h-7 w-44 rounded-md border border-border bg-bg px-2 text-[12px] text-fg placeholder:text-faint focus:border-border-strong focus:outline-none"
              data-testid={`data-health-search-${check.type}`}
            />
            <button
              type="submit"
              disabled={busy}
              className="h-7 cursor-pointer rounded-md border border-border bg-surface px-2.5 text-[12px] font-medium text-fg transition-[background-color,border-color,transform] duration-150 hover:border-border-strong hover:bg-hover active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60"
            >
              Filter
            </button>
          </form>
          {(knownTypes.length > 1 || objectType) && (
            <select
              value={objectType}
              aria-label={`Filter ${checkLabel} by record type`}
              onChange={(e) => applyFilters(queryInput, e.target.value)}
              className="h-7 cursor-pointer rounded-md border border-border bg-bg px-2 text-[12px] text-fg outline-none transition-colors hover:border-border-strong focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-ring/40"
              data-testid={`data-health-type-${check.type}`}
            >
              <option value="">All record types</option>
              {knownTypes.map((t) => (
                <option key={t} value={t}>{typeLabel(t)}</option>
              ))}
            </select>
          )}
          <select
            value={assigneeFilter}
            aria-label={`Filter ${checkLabel} by assignment`}
            onChange={(e) => setAssigneeFilter(e.target.value as "all" | "unassigned" | "assigned")}
            className="h-7 cursor-pointer rounded-md border border-border bg-bg px-2 text-[12px] text-fg outline-none transition-colors hover:border-border-strong focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-ring/40"
            data-testid={`data-health-assignee-${check.type}`}
          >
            <option value="all">All items</option>
            <option value="unassigned">Unassigned</option>
            <option value="assigned">Assigned</option>
          </select>
          {hasFilter && (
            <button
              type="button"
              onClick={() => {
                setQueryInput("");
                setAssigneeFilter("all");
                applyFilters("", "");
              }}
              className="cursor-pointer text-[12px] text-muted underline underline-offset-2 hover:text-fg"
            >
              Clear
            </button>
          )}
          <span className="ml-auto tabular-nums text-muted" data-testid={`data-health-showing-${check.type}`}>
            {visibleItems.length > 0
              ? `Showing 1-${visibleItems.length.toLocaleString("en-US")} of ${exactTotal.toLocaleString("en-US")}`
              : exactTotal === 0
                ? "Showing 0 of 0"
                : `Showing 0 of ${exactTotal.toLocaleString("en-US")}`}
          </span>
        </div>
      )}
      <ul className="mt-1.5 space-y-1">
        {visibleItems.map((it) => (
          <li key={it.id} className="flex min-w-0 items-baseline gap-2">
            <Link href={safeHref(it.open_url) ?? objectHref(it.object_type, it.object_id)} className="block min-w-0 flex-1 line-clamp-2 text-fg hover:underline" title={it.summary}>
              {readable(it.summary)}
            </Link>
            {assigned[it.id] ? (
              <span className="shrink-0 text-muted" data-testid={`finding-assigned-${it.id}`}>{assigned[it.id]}</span>
            ) : (
              <CardActions
                card={{ id: `dh:${check.type}:${it.id}`, subject: { label: it.summary }, actions: [{ kind: "assign", label: "Assign", outbound: false }] }}
                onDone={(r) => setAssigned((a) => ({ ...a, [it.id]: r.status === "assigned" ? "Assigned" : "Done" }))}
              />
            )}
          </li>
        ))}
      </ul>
      {read && !busy && visibleItems.length === 0 && hasFilter && (
        <p className="mt-1 text-muted">No findings match this filter.</p>
      )}
      {more > 0 && (
        <p className="mt-1.5 text-muted">
          <button type="button" className="cursor-pointer font-medium text-fg underline decoration-border-strong underline-offset-2 disabled:cursor-default disabled:opacity-60" disabled={busy} onClick={() => load(items.length)}>
            {busy ? "Reading" : `Show ${Math.min(50, more)} more`}
          </button>{" "}
          · {more.toLocaleString("en-US")} not shown
        </p>
      )}
      {failed && <p role="alert" className="mt-1 text-late">That page couldn&rsquo;t be read. Try again.</p>}
      <p className="mt-1.5">
        <a href={exportHref} className="text-accent hover:underline" download>Download this check as a spreadsheet</a>
      </p>
    </details>
  );
}

/** One alert type's items, read when asked for: a count on Watching opens what it counts. */
export function TypeItems({ type, count, more: label, skip = [] }: { type: string; count: number; more?: string; skip?: string[] }) {
  const [items, setItems] = useState<BriefingItem[] | null>(null);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // how many rows the server has given so far (the skipped ones too): where the next page starts
  const [served, setServed] = useState(0);
  const read = (offset: number) => {
    setBusy(true);
    setError(null);
    getBriefingItems(type, offset)
      .then((r) => {
        const got = r.items ?? [];
        // the rows already shown above ("2 shown, 65 more") are not listed again
        setItems((cur) => [...(offset ? (cur ?? []) : []), ...got.filter((it) => !skip.includes(it.id))]);
        setServed(offset + got.length);
        setMore(!!r.more);
      }, setError)
      .finally(() => setBusy(false));
  };
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={() => read(items ? served : 0)} className="mt-1" />;
  if (!items)
    return (
      <button type="button" onClick={() => read(0)} disabled={busy} aria-busy={busy || undefined} className="text-[12px] font-medium text-fg underline underline-offset-2 hover:no-underline disabled:text-muted cursor-pointer">
        {busy ? "Reading" : (label ?? `Show ${count}`)}
      </button>
    );
  return (
    <div className="mt-1 w-full">
      {items.length === 0 ? (
        !more && <p className="text-muted">None of these are yours to see now.</p>
      ) : (
        <ul className="space-y-1" data-testid={`items-${type}`}>
          {items.map((it) => {
            const href = safeHref(it.open_url) ?? (it.object_type && it.object_id ? objectHref(it.object_type, it.object_id) : null);
            const title = it.subject_label ? recordLabel(it.object_type, it.subject_label) : it.label;
            return (
              <li key={it.id} className="min-w-0">
                {href ? (
                  <Link href={href} className="block line-clamp-2 text-fg hover:underline" title={readable(it.summary)}>
                    <span className="font-medium text-accent">{title}</span>
                    {it.summary ? <span className="text-muted"> · {readable(it.summary)}</span> : null}
                  </Link>
                ) : (
                  <span className="block line-clamp-2 text-fg" title={readable(it.summary)}>
                    <span className="font-medium">{title}</span>
                    {it.summary ? <span className="text-muted"> · {readable(it.summary)}</span> : null}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {more && (
        <button type="button" onClick={() => read(served)} disabled={busy} className="mt-1 text-[12px] font-medium text-fg underline underline-offset-2 hover:no-underline disabled:text-muted cursor-pointer">
          {busy ? "Reading" : "Show more"}
        </button>
      )}
    </div>
  );
}

/**
 * Watching's alert list: every alert kind from the registry with what it holds for you
 * now. On kinds that are not on My work, and off kinds, wait here; a kind is on for
 * everyone only once a check of 15 live items finds 90% right.
 */
export function AlertKindsSection({ data, compact = false }: { data?: { reg: AlertKinds | null; briefing: Briefing | null; error: unknown; retry: () => void }; compact?: boolean } = {}) {
  const [ownReg, setReg] = useState<AlertKinds | null>(null);
  const [ownBriefing, setBriefing] = useState<Briefing | null>(null);
  const [ownError, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const ownRetry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    if (data) return;
    const ctl = new AbortController();
    setError(null);
    getAlertKinds(ctl.signal).then(setReg, (e) => !ctl.signal.aborted && setError(e));
    getBriefing(ctl.signal).then(setBriefing, () => undefined);
    return () => ctl.abort();
  }, [round, data]);
  const reg = data ? data.reg : ownReg;
  const briefing = data ? data.briefing : ownBriefing;
  const error = data ? data.error : ownError;
  const retry = data ? data.retry : ownRetry;
  const counts = useMemo(() => {
    const out = new Map<string, number>();
    for (const g of briefing?.status === "read" ? (briefing.groups ?? []) : []) out.set(g.event_type, g.count);
    return out;
  }, [briefing]);
  const groups = useMemo(() => {
    if (!reg) return [];
    return reg.kinds.map((k) => {
      const types = reg.types.filter((t) => t.kind === k.kind);
      const n = types.reduce((sum, t) => sum + (counts.get(t.type) ?? 0), 0);
      return { ...k, types, n };
    });
  }, [reg, counts]);
  return (
    <section aria-labelledby="alerts-title" id="alerts" className="scroll-mt-16 space-y-2">
      <h2 id="alerts-title" className={compact ? "text-[11px] font-semibold tracking-[0.04em] text-muted uppercase" : "text-[15px] font-semibold"}>{compact ? "Alert kinds" : "Alerts"}</h2>
      {error ? (
        <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : !reg ? (
        <Pending label="Reading the alert list" />
      ) : groups.length === 0 ? (
        <EmptyState title="No alert kinds." />
      ) : (
        <ul className="divide-y divide-border" data-testid="alert-kinds">
          {groups.map((k) => (
            <li key={k.kind} className="py-2">
              <details>
                <summary className={cn("flex cursor-pointer items-baseline gap-3", compact ? "text-[12px]" : "text-[13px]")}>
                  <span className="min-w-0 flex-1 font-medium text-fg">{k.label}</span>
                  <span className="shrink-0 tabular-nums text-muted">{k.n > 0 ? k.n : ""}</span>
                  {surfaceHref(k.surface) ? (
                    <Link href={surfaceHref(k.surface)!} className="shrink-0 text-[12px] text-faint underline-offset-2 hover:text-fg hover:underline" data-testid={`surface-${k.kind}`}>
                      {surfaceWord(k)}
                    </Link>
                  ) : (
                    <span className="shrink-0 text-[12px] text-faint">{surfaceWord(k)}</span>
                  )}
                </summary>
                <ul className="mt-1.5 space-y-1 pl-3 text-[12px]">
                  {k.types.map((t) => (
                    <li key={t.type} className="flex flex-wrap items-baseline gap-x-3">
                      <span className="min-w-0 flex-1 text-fg/80">{t.label}</span>
                      {counts.get(t.type) ? <span className="tabular-nums text-muted">{counts.get(t.type)}</span> : null}
                      <span className="shrink-0 text-faint" data-testid={`standing-${t.type}`}>{standing(t)}</span>
                      {counts.get(t.type) ? (
                        <div className="basis-full pb-1">
                          <TypeItems type={t.type} count={counts.get(t.type)!} />
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </details>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
