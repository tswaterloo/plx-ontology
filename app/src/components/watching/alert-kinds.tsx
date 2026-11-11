"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { getAlertKinds, getBriefing, getBriefingItems } from "@/lib/api";
import { cn } from "@/lib/cn";
import { objectHref, recordLabel, safeHref } from "@/lib/format";
import { noticeKind, plainError } from "@/lib/errors";
import type { AlertKinds, AlertType, Briefing, BriefingItem } from "@/lib/types";

/** How a type's default reads: on, or off with its measurement. */
export function standing(t: Pick<AlertType, "default" | "precision" | "home">): string {
  const p = t.precision;
  const measured = p ? `${Math.round(p.value * 100)}% of ${p.checked} checked` : "not measured";
  if (t.default === "on") return t.home ? `On, in My work · ${measured}` : `On · ${measured}`;
  return `Off · ${measured}`;
}

/** Whether a kind is on (any of its types on by default) and, for a kind that shows on a
 * page of its own, that page (not repeated when the kind's own name is that page). */
export function surfaceWord(k: { label: string; surface: string; types: Pick<AlertType, "default">[] }): string {
  const on = k.types.some((t) => t.default === "on") ? "On" : "Off";
  if (k.surface === "alert") return on;
  const where = k.surface === "weekly" ? "Weekly review" : "Data health";
  return where.toLowerCase() === k.label.trim().toLowerCase() ? on : `${on} · ${where}`;
}

/** One alert type's items, read when asked for: a count on Watching opens what it counts. */
export function TypeItems({ type, count, more: label }: { type: string; count: number; more?: string }) {
  const [items, setItems] = useState<BriefingItem[] | null>(null);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const read = (offset: number) => {
    setBusy(true);
    setError(null);
    getBriefingItems(type, offset)
      .then((r) => {
        setItems((cur) => [...(offset ? (cur ?? []) : []), ...(r.items ?? [])]);
        setMore(!!r.more);
      }, setError)
      .finally(() => setBusy(false));
  };
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={() => read(items?.length ?? 0)} className="mt-1" />;
  if (!items)
    return (
      <button type="button" onClick={() => read(0)} disabled={busy} aria-busy={busy || undefined} className="text-[12px] font-medium text-fg underline underline-offset-2 hover:no-underline disabled:text-muted cursor-pointer">
        {busy ? "Reading" : (label ?? `Show ${count}`)}
      </button>
    );
  return (
    <div className="mt-1 w-full">
      {items.length === 0 ? (
        <p className="text-muted">None of these are yours to see now.</p>
      ) : (
        <ul className="space-y-1" data-testid={`items-${type}`}>
          {items.map((it) => (
            <li key={it.id} className="min-w-0">
              <Link href={safeHref(it.open_url) ?? objectHref(it.object_type, it.object_id)} className="block truncate text-fg hover:underline" title={it.summary}>
                {it.subject_label ? recordLabel(it.object_type, it.subject_label) : it.label}
                {it.summary ? <span className="text-muted"> · {it.summary}</span> : null}
              </Link>
            </li>
          ))}
        </ul>
      )}
      {more && (
        <button type="button" onClick={() => read(items.length)} disabled={busy} className="mt-1 text-[12px] font-medium text-fg underline underline-offset-2 hover:no-underline disabled:text-muted cursor-pointer">
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
                  <span className="shrink-0 text-[12px] text-faint">{surfaceWord(k)}</span>
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
