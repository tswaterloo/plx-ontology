"use client";

import { useMemo } from "react";
import { eastern, SectionTabs, sourceName } from "@/components/shell";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { getTrust } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { countdownText, roleLabel, rowStateShown, workflowLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import type { AutopilotWorkflowRow, TrustAlert, TrustAnswerScore, TrustAnswerSet, TrustFreshness, TrustPage } from "@/lib/types";

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
const dayText = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "America/New_York" }).replace("Sept", "Sep");

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
      <span className="num text-[14px] font-semibold">{s.correct} of {s.answered} right</span>
      <span className="num ml-2 text-[12px] text-muted">{pctText(s.accuracy)} · answered {s.answered} of {s.runs}</span>
      <span className="num block text-[11px] text-faint">
        {s.items} questions, {s.runs} runs · {dayText(s.at)} · {s.evidence_label}
      </span>
    </td>
  );
}

/** "Email, File and Teams": names as a person lists them. */
function listed(names: string[]): string {
  return names.length < 2 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** One verdict per source: "Up to date", "Behind" or "Not read yet"; the reason, if any, beside it. */
function verdictOf(f: TrustFreshness): { word: string; tone: string; why: string } {
  const words = (f.state_words ?? "").trim();
  const why = words.replace(/^(Up to date|Behind)(:\s*|\.\s*)/, "").replace(/^everything read is in the app\.?\s*/i, "").trim();
  if (!f.captured_at) return { word: "Not read yet", tone: "text-late", why: why === "Never read yet." ? "" : why };
  if (f.stale) return { word: "Behind", tone: "text-late", why };
  return { word: "Up to date", tone: "text-ok", why: f.capture === "failed" ? why : "" };
}

/** One sentence: is the app's data keeping up with the systems it reads? */
function sourcesSentence(fresh: TrustFreshness[]): string {
  if (fresh.length === 0) return "No source system is read on this server.";
  const behind = fresh.filter((f) => f.stale).map((f) => sourceName(f.system));
  const failed = fresh.filter((f) => !f.stale && f.capture === "failed").map((f) => sourceName(f.system));
  const head = behind.length === 0
    ? `The app is up to date with all ${plural(fresh.length, "system", "systems")} it reads`
    : behind.length === fresh.length
      ? `The app is behind on every system it reads (${listed(behind)})`
      : `The app is behind on ${listed(behind)}; the other ${plural(fresh.length - behind.length, "system is", "systems are")} up to date`;
  return failed.length ? `${head}, though the last read of ${listed(failed)} failed (see below).` : `${head}.`;
}

/** One sentence: has answer accuracy on questions never used for tuning been measured? */
function heldOutSentence(sets: TrustAnswerSet[]): string {
  const measured = sets.filter((s) => s.held_out);
  if (measured.length === 0) return "Answer accuracy on questions never used for tuning has not been measured on this server yet.";
  const missing = sets.length - measured.length;
  return `Answer accuracy on questions never used for tuning is measured for ${plural(measured.length, "question set", "question sets")}${missing ? `; ${missing} not yet` : ""} (scores below).`;
}

/** One sentence: which alert types reach people, and why the rest do not. */
function alertsSentence(on: number, off: number, unmeasured: number, total: number): string {
  if (total === 0) return "No alert types are set up.";
  const rest: string[] = [];
  if (off) rest.push(`${off} ${off === 1 ? "was" : "were"} checked by hand but ${off === 1 ? "has" : "have"} not passed yet`);
  if (unmeasured) rest.push(`${unmeasured} ${unmeasured === 1 ? "has" : "have"} not been checked yet`);
  return `${on} of the ${total} alert types ${on === 1 ? "is" : "are"} switched on${rest.length ? `; the rest stay off (${rest.join(", ")})` : ""}.`;
}

/** One sentence: what the autopilot does today. */
function autopilotSentence(rows: AutopilotWorkflowRow[]): string {
  if (rows.length === 0) return "No autopilot workflow is set up on this server yet.";
  const drafted = rows.filter((r) => r.draft).length;
  const live = rows.filter((r) => r.state === "live").length;
  if (!drafted && !live) return "The autopilot sends and changes nothing yet.";
  const parts: string[] = [];
  if (drafted) parts.push(`prepares drafts for a person to confirm in ${plural(drafted, "kind of work", "kinds of work")}`);
  if (live) parts.push(`acts on a person's approval in ${live}`);
  return `The autopilot ${parts.join(" and ")}; nothing goes out without that person's click.`;
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
      <td className={cn("py-1 text-[12px]", a.on ? "font-medium text-fg" : "text-muted")}>{a.on ? (a.home ? "On, Home" : "On") : "Off"}</td>
    </tr>
  );
}

// The Trust page: what Vince, Stephen and the owner need to judge the Workbench without
// asking anyone. Each figure is read from where it is kept (capture ledger, served data,
// eval results, alert registry, autopilot scores, server switches); an unmeasured figure
// says so. The owner and the Team readers only; anyone else gets the route's 404.
export default function TrustRoute() {
  const { data: page, error, retry } = useRead<TrustPage>("trust", (signal) => getTrust(signal));
  const alerts = useMemo(() => {
    const all = page?.alerts ?? [];
    return { on: all.filter((a) => a.on), off: all.filter((a) => !a.on && a.judged != null), unmeasured: all.filter((a) => a.judged == null) };
  }, [page]);
  // the server answers 404 to a reader it does not open Trust for (it is left out of their
  // menu): said as the Reliability page says its 403, never "Page not found" for a page that is there
  const refused = error instanceof ApiError && error.status === 404;
  const fresh = page?.freshness ?? [];
  const rows = page?.autopilot ?? [];
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Trust" />
      <SectionTabs section="admin" />
      <PageIntro path="/trust" />
      <div className="w-full max-w-5xl space-y-6 overflow-y-auto px-4 py-4 sm:px-6" data-testid="trust-page">
        {refused ? (
          <p className="text-[13px]" data-testid="trust-refused">{plainError(new ApiError(403, "forbidden"))}</p>
        ) : error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : !page ? (
          <Pending page label="Reading freshness, scores and switches" />
        ) : (
          <>
            <div className="border-b border-border pb-3">
              <ul className="space-y-1 text-[13px]" data-testid="trust-summary">
                <li>{sourcesSentence(fresh)}</li>
                <li>{heldOutSentence(page.answers ?? [])}</li>
                <li>{alertsSentence(alerts.on.length, alerts.off.length, alerts.unmeasured.length, (page.alerts ?? []).length)}</li>
                <li>{autopilotSentence(rows)}</li>
              </ul>
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
                      <th className="py-1 pr-3 font-normal">Source</th>
                      <th className="py-1 pr-3 font-normal">State</th>
                      <th className="py-1 pr-3 font-normal">Last read</th>
                      <th className="py-1 font-normal max-sm:hidden">Why</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {fresh.map((f) => {
                      const v = verdictOf(f);
                      return (
                        <tr key={f.system} className="align-top">
                          <td className="py-1.5 pr-3 font-medium">{sourceName(f.system)}</td>
                          <td className={cn("py-1.5 pr-3 font-medium whitespace-nowrap", v.tone)}>{v.word}</td>
                          <td className="num py-1.5 pr-3 whitespace-nowrap">{f.captured_at ? `${eastern(f.captured_at)} · ${lagText(f.lag_seconds)} ago` : "—"}</td>
                          <td className="py-1.5 text-[12px] text-muted max-sm:hidden">{v.why}</td>
                        </tr>
                      );
                    })}
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
                  ? `Gate: at least ${page.autopilot_gate.need_judged} held-out items judged (fresh days, or past days never tuned on), ${page.autopilot_gate.need_percent}% or more right, before a workflow may act on approval.`
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
                        {page.autopilot_gate ? `Gate: held-out items at ≥${page.autopilot_gate.need_percent}%` : "Gate"}
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
