"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { NotFoundBody } from "@/components/not-found-body";
import { eastern } from "@/components/shell";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { getTrust } from "@/lib/api";
import { countdownText, roleLabel, rowStateShown, workflowLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import type { AutopilotWorkflowRow, TrustAlert, TrustAnswerScore, TrustPage } from "@/lib/types";

/** "2 h 10 min", "3 days": a lag as a person reads it. */
function lagText(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 90) return "under 2 min";
  const m = Math.round(seconds / 60);
  if (m < 120) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 36) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  return `${Math.round(h / 24)} days`;
}

const pctText = (p: number | null | undefined) => (p == null ? "—" : `${p.toFixed(1)}%`);
const dayText = (iso: string) => new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "America/New_York" });

/** "person:vince@x" -> "Vince"; a role by its label. */
function whoOf(row: AutopilotWorkflowRow): string {
  if (!row.role.startsWith("person:")) return roleLabel(row.role);
  const local = row.role.slice("person:".length).split("@")[0] ?? "";
  return local ? local.charAt(0).toUpperCase() + local.slice(1) : "One person";
}

function Score({ s, label }: { s: TrustAnswerScore | null | undefined; label: string }) {
  if (!s) return <td className="py-1.5 pr-3 text-[12px] text-faint">Not measured yet</td>;
  return (
    <td className="py-1.5 pr-3" aria-label={`${label} score`}>
      <span className="num text-[14px] font-semibold">{pctText(s.accuracy)}</span>
      <span className="num ml-2 text-[12px] text-muted">answered {pctText(s.coverage)}</span>
      <span className="num block text-[11px] text-faint">
        {s.correct} of {s.answered} right · {s.items} questions, {s.runs} runs · {dayText(s.at)} · {s.evidence_label}
      </span>
    </td>
  );
}

const STATE_TONE = { live: "text-ok", gated: "text-risk", pending: "text-muted" } as const;
const STATE_WORD = { live: "Live", gated: "Gated", pending: "Pending" } as const;

function AlertRow({ a }: { a: TrustAlert }) {
  const under = a.percent != null && a.percent < 90;
  return (
    <tr className="align-top">
      <td className="py-1 pr-3">
        {a.label}
        {a.note && <span className="block text-[11px] leading-4 text-faint">{a.note}</span>}
      </td>
      <td className="num py-1 pr-3 text-right">{a.judged ?? "—"}</td>
      <td className="num py-1 pr-3 text-right">{a.correct ?? "—"}</td>
      <td className={cn("num py-1 pr-3 text-right", a.percent == null ? "text-faint" : under ? "text-risk" : "text-ok")}>{pctText(a.percent)}</td>
      <td className="num py-1 pr-3 whitespace-nowrap text-muted max-sm:hidden">{a.measured_on ?? ""}</td>
      <td className={cn("py-1 text-[12px]", a.on ? "font-medium text-fg" : "text-muted")}>{a.on ? (a.home ? "On, My work" : "On") : "Off"}</td>
    </tr>
  );
}

// The Trust page: what Vince, Stephen and the owner need to judge the Workbench without
// asking anyone. Each figure is read from where it is kept (capture ledger, served data,
// eval results, alert registry, autopilot scores, server switches); an unmeasured figure
// says so. The owner and the Team readers only; anyone else gets the route's 404.
export default function TrustRoute() {
  const [page, setPage] = useState<TrustPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getTrust(ctl.signal).then(setPage, (e) => !ctl.signal.aborted && setError(e));
    return () => ctl.abort();
  }, [round]);
  const alerts = useMemo(() => {
    const all = page?.alerts ?? [];
    return { on: all.filter((a) => a.on), off: all.filter((a) => !a.on && a.judged != null), unmeasured: all.filter((a) => a.judged == null) };
  }, [page]);
  if (error instanceof ApiError && error.status === 404) return <NotFoundBody />;
  const fresh = page?.freshness ?? [];
  const staleCount = fresh.filter((f) => f.stale).length;
  const rows = page?.autopilot ?? [];
  const drafted = rows.filter((r) => r.draft).length;
  const measuredSets = (page?.answers ?? []).filter((s) => s.held_out).length;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Trust" />
      <div className="mx-auto w-full max-w-5xl space-y-6 overflow-y-auto px-4 py-4 sm:px-6" data-testid="trust-page">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : !page ? (
          <Pending page label="Reading freshness, scores and switches" />
        ) : (
          <>
            <div className="border-b border-border pb-3">
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-4" data-testid="trust-summary">
                <div>
                  <dt className="text-[12px] text-muted">Source systems</dt>
                  <dd className="num">
                    <span className="text-[20px] font-semibold">{fresh.length}</span>
                    <span className={cn("ml-2 text-[12px]", staleCount ? "text-late" : "text-muted")}>{staleCount ? `${staleCount} behind` : "all current"}</span>
                  </dd>
                </div>
                <div>
                  <dt className="text-[12px] text-muted">Held-out answer scores</dt>
                  <dd className="num">
                    <span className="text-[20px] font-semibold">{measuredSets}</span>
                    <span className="ml-2 text-[12px] text-muted">of {(page.answers ?? []).length} question sets</span>
                  </dd>
                </div>
                <div>
                  <dt className="text-[12px] text-muted">Alert types on</dt>
                  <dd className="num">
                    <span className="text-[20px] font-semibold">{alerts.on.length}</span>
                    <span className="ml-2 text-[12px] text-muted">of {(page.alerts ?? []).length}, {alerts.unmeasured.length} unmeasured</span>
                  </dd>
                </div>
                <div>
                  <dt className="text-[12px] text-muted">Autopilot in Draft</dt>
                  <dd className="num">
                    <span className="text-[20px] font-semibold">{drafted}</span>
                    <span className="ml-2 text-[12px] text-muted">of {rows.length} workflows scored</span>
                  </dd>
                </div>
              </dl>
              <p className="mt-2 text-[12px] text-muted">Checked {eastern(page.checked_at)}. Counts and times only; each figure comes from where it is kept.</p>
            </div>

            <section aria-labelledby="trust-state">
              <h2 id="trust-state" className="text-[13px] font-semibold">Live, gated and pending</h2>
              <ul className="mt-1 divide-y divide-border text-[13px]" data-testid="trust-lines">
                {(page.lines ?? []).map((l) => (
                  <li key={l.words} className="flex gap-3 py-1.5">
                    <span className={cn("w-16 shrink-0 text-[12px] font-medium", STATE_TONE[l.state])}>{STATE_WORD[l.state]}</span>
                    <span>{l.words}</span>
                  </li>
                ))}
              </ul>
            </section>

            <section aria-labelledby="trust-fresh">
              <h2 id="trust-fresh" className="text-[13px] font-semibold">Freshness by source system</h2>
              {fresh.length === 0 ? (
                <p className="mt-1 text-[13px] text-muted">This server holds no capture ledger or served data to read.</p>
              ) : (
                <table className="mt-1 w-full text-left text-[13px]" data-testid="trust-freshness">
                  <thead className="text-[12px] text-muted">
                    <tr className="border-b border-border">
                      <th className="py-1 pr-3 font-normal">System</th>
                      <th className="py-1 pr-3 font-normal">Last captured</th>
                      <th className="py-1 pr-3 font-normal">In the served data</th>
                      <th className="py-1 pr-3 font-normal">Lag</th>
                      <th className="py-1 font-normal max-sm:hidden">State</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {fresh.map((f) => (
                      <tr key={f.system} className="align-top">
                        <td className="py-1.5 pr-3 font-medium">{f.system}</td>
                        <td className="num py-1.5 pr-3">{f.captured_at ? eastern(f.captured_at) : <span className="text-faint">not in the ledger</span>}</td>
                        <td className={cn("num py-1.5 pr-3", f.stale && "text-late")}>{f.served_at ? eastern(f.served_at) : <span className="text-faint">not served yet</span>}</td>
                        <td className={cn("num py-1.5 pr-3", (f.lag_seconds ?? 0) > 6 * 3600 && "text-risk")}>{lagText(f.lag_seconds)}</td>
                        <td className="py-1.5 text-[12px] text-muted max-sm:hidden">
                          {f.stale ? "Behind" : "Current"}
                          {f.capture && f.capture !== "complete" ? `, last capture ${f.capture.replace("_", " ")}` : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section aria-labelledby="trust-answers">
              <h2 id="trust-answers" className="text-[13px] font-semibold">Answer quality</h2>
              <p className="text-[12px] text-muted">Latest run per question set. Held-out questions were never used to tune the answers.</p>
              {(page.answers ?? []).length === 0 ? (
                <p className="mt-1 text-[13px] text-muted" data-testid="trust-answers-empty">
                  Not measured yet: no evaluation run has recorded its score on this server.
                </p>
              ) : (
                <table className="mt-1 w-full text-left text-[13px]" data-testid="trust-answers">
                  <thead className="text-[12px] text-muted">
                    <tr className="border-b border-border">
                      <th className="py-1 pr-3 font-normal">Question set</th>
                      <th className="py-1 pr-3 font-normal">Development</th>
                      <th className="py-1 pr-3 font-normal">Held-out</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {(page.answers ?? []).map((s) => (
                      <tr key={s.name} className="align-top">
                        <td className="py-1.5 pr-3 font-medium">{s.name.replace(/_/g, " ")}</td>
                        <Score s={s.dev} label="Development" />
                        <Score s={s.held_out} label="Held-out" />
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section aria-labelledby="trust-autopilot">
              <h2 id="trust-autopilot" className="text-[13px] font-semibold">Autopilot workflows</h2>
              <p className="text-[12px] text-muted">
                Practice score: replayed days the rules were tuned on.{" "}
                {page.autopilot_gate
                  ? `Gate: at least ${page.autopilot_gate.need_judged} items judged on fresh days, ${page.autopilot_gate.need_percent}% or more right, before a workflow may act on approval.`
                  : "Gate: the rules cannot be read, so no workflow acts."}
              </p>
              {rows.length === 0 ? (
                <p className="mt-1 text-[13px] text-muted">{page.autopilot_note}</p>
              ) : (
                <table className="mt-1 w-full text-left text-[13px]" data-testid="trust-autopilot">
                  <thead className="text-[12px] text-muted">
                    <tr className="border-b border-border">
                      <th className="py-1 pr-3 font-normal">Workflow</th>
                      <th className="py-1 pr-3 font-normal">For</th>
                      <th className="py-1 pr-3 font-normal">Mode</th>
                      <th className="py-1 pr-3 text-right font-normal">Practice</th>
                      <th className="py-1 font-normal">
                        {page.autopilot_gate ? `Gate: fresh items at ≥${page.autopilot_gate.need_percent}%` : "Gate"}
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {rows.map((r) => {
                      const shown = rowStateShown(r, false);
                      return (
                        <tr key={`${r.role}/${r.workflow}`} className="align-top">
                          <td className="py-1.5 pr-3">{workflowLabel(r.workflow)}</td>
                          <td className="py-1.5 pr-3 text-muted">{whoOf(r)}</td>
                          <td className={cn("py-1.5 pr-3 text-[12px]", r.draft ? "font-medium text-accent" : shown.status === "ok" ? "text-ok" : "text-muted")}>{shown.label}</td>
                          <td className="num py-1.5 pr-3 text-right">
                            {r.dev ? pctText(r.dev.percent) : "—"}
                            {r.dev && <span className="block text-[11px] text-faint">{r.dev.matched} of {r.dev.judged}</span>}
                          </td>
                          <td className="num py-1.5 text-[12px]">{countdownText(r.countdown) ?? <span className="text-faint">not scored yet</span>}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </section>

            <section aria-labelledby="trust-alerts">
              <h2 id="trust-alerts" className="text-[13px] font-semibold">Alert precision</h2>
              {page.alert_rule && <p className="text-[12px] leading-5 text-muted">{page.alert_rule}</p>}
              <table className="mt-1 w-full text-left text-[13px]" data-testid="trust-alerts">
                <thead className="text-[12px] text-muted">
                  <tr className="border-b border-border">
                    <th className="py-1 pr-3 font-normal">Alert</th>
                    <th className="py-1 pr-3 text-right font-normal">Judged</th>
                    <th className="py-1 pr-3 text-right font-normal">Right</th>
                    <th className="py-1 pr-3 text-right font-normal">Precision</th>
                    <th className="py-1 pr-3 font-normal max-sm:hidden">Checked</th>
                    <th className="py-1 font-normal">Shown</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {[...alerts.on, ...alerts.off].map((a) => (
                    <AlertRow key={a.type} a={a} />
                  ))}
                </tbody>
              </table>
              {alerts.unmeasured.length > 0 && (
                <details className="mt-2 text-[13px]">
                  <summary className="cursor-pointer text-[12px] text-muted">{alerts.unmeasured.length} types not measured yet (off until hand-checked)</summary>
                  <ul className="mt-1 divide-y divide-border">
                    {alerts.unmeasured.map((a) => (
                      <li key={a.type} className="py-1">
                        {a.label}
                        {a.note && <span className="block text-[11px] text-faint">{a.note}</span>}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </section>

            <section aria-labelledby="trust-switches">
              <h2 id="trust-switches" className="text-[13px] font-semibold">Write switches</h2>
              <table className="mt-1 w-full text-left text-[13px]" data-testid="trust-switches">
                <tbody className="divide-y divide-border">
                  {(page.switches ?? []).map((s) => (
                    <tr key={s.system}>
                      <td className="w-48 py-1.5 pr-3 font-medium">{s.system}</td>
                      <td className={cn("w-16 py-1.5 pr-3 text-[12px] font-medium", s.state === "on" ? "text-ok" : "text-muted")}>{s.state === "on" ? "On" : s.state === "off" ? "Off" : "None"}</td>
                      <td className="py-1.5 text-[12px] text-muted">{s.words}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
