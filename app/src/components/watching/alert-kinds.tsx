"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { getAlertKinds, getBriefing } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { AlertKinds, AlertType, Briefing } from "@/lib/types";

/** How a type's default reads: on, or off with its measurement. */
export function standing(t: Pick<AlertType, "default" | "precision" | "home">): string {
  const p = t.precision;
  const measured = p ? `${Math.round(p.value * 100)}% of ${p.checked} checked` : "not measured";
  if (t.default === "on") return t.home ? `On, in My work · ${measured}` : `On · ${measured}`;
  return `Off · ${measured}`;
}

/**
 * Watching's alert list: every alert kind from the registry with what it holds for you
 * now. On kinds that are not on My work, and off kinds, wait here; a kind is on for
 * everyone only once a check of 15 live items finds 90% right.
 */
export function AlertKindsSection() {
  const [reg, setReg] = useState<AlertKinds | null>(null);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getAlertKinds(ctl.signal).then(setReg, (e) => !ctl.signal.aborted && setError(e));
    getBriefing(ctl.signal).then(setBriefing, () => undefined);
    return () => ctl.abort();
  }, [round]);
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
    <section aria-labelledby="alerts" id="alerts" className="scroll-mt-16 space-y-2">
      <h2 id="alerts-title" className="text-[15px] font-semibold">Alerts</h2>
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
                <summary className="flex cursor-pointer items-baseline gap-3 text-[13px]">
                  <span className="min-w-0 flex-1 font-medium text-fg">{k.label}</span>
                  <span className="shrink-0 tabular-nums text-muted">{k.n > 0 ? k.n : ""}</span>
                  <span className="shrink-0 text-[12px] text-faint">{k.surface === "alert" ? (k.types.some((t) => t.default === "on") ? "On" : "Off") : k.surface === "weekly" ? "Weekly review" : "Data health"}</span>
                </summary>
                <ul className="mt-1.5 space-y-1 pl-3 text-[12px]">
                  {k.types.map((t) => (
                    <li key={t.type} className="flex items-baseline gap-3">
                      <span className="min-w-0 flex-1 text-fg/80">{t.label}</span>
                      {counts.get(t.type) ? <span className="tabular-nums text-muted">{counts.get(t.type)}</span> : null}
                      <span className="shrink-0 text-faint" data-testid={`standing-${t.type}`}>{standing(t)}</span>
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
