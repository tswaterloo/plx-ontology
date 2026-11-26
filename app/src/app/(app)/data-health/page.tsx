"use client";

import { CheckCircle2, CircleAlert, FileEdit, PauseCircle, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { eastern, SectionTabs, sourceName } from "@/components/shell";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { StoredAge } from "@/components/ui/stored-age";
import { getTrust } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { countdownText, roleLabel, rowStateShown, workflowLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { AnswersRight } from "@/components/watching/answers-right";
import { DataHealthSection } from "@/components/watching/alert-kinds";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import type { AutopilotWorkflowRow, TrustAlert, TrustAnswerScore, TrustAnswerSet, TrustFreshness, TrustPage } from "@/lib/types";

function lagText(seconds: number | null | undefined): string {
  if (seconds == null) return "-";
  if (seconds < 90) return "under 2 min";
  const m = Math.round(seconds / 60);
  if (m < 120) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 36) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  return `${Math.round(h / 24)} days`;
}

const pctText = (p: number | null | undefined) => (p == null ? "–" : `${p.toFixed(1)}%`);
const dayText = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "America/New_York" }).replace("Sept", "Sep");

function whoOf(row: AutopilotWorkflowRow): string {
  const role = row.role ?? "";
  if (!role.startsWith("person:")) return roleLabel(role);
  const local = role.slice("person:".length).split("@")[0] ?? "";
  return local ? local.charAt(0).toUpperCase() + local.slice(1) : "One person";
}

function questionSetLabel(name: string): string {
  const raw = name ?? "";
  const words = raw.replace(/_/g, " ").trim();
  if (!words) return raw;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function Score({ s, label }: { s: TrustAnswerScore | null | undefined; label: string }) {
  if (!s) return <td className="px-3 py-2.5 text-[12.5px] text-muted">Not measured yet</td>;
  return (
    <td className="px-3 py-2.5" aria-label={`${label} score`}>
      <span className="num text-[14px] font-semibold text-fg">{s.correct} of {s.answered} right</span>
      <span className="num ml-2.5 text-[12.5px] text-muted">{pctText(s.accuracy)} (answered {s.answered} of {s.runs})</span>
      <span className="num mt-0.5 flex flex-wrap items-center gap-x-2.5 text-[12px] text-muted">
        <span>{s.items} questions, {s.runs} runs</span>{" "}
        <span>{dayText(s.at)}</span>{" "}
        <span>{s.evidence_label}</span>
      </span>
    </td>
  );
}

function listed(names: string[]): string {
  return names.length < 2 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function verdictOf(f: TrustFreshness): { word: string; tone: string; ok: boolean; why: string } {
  const words = (f.state_words ?? "").trim();
  const why = words.replace(/^(Up to date|Behind)(:\s*|\.\s*)/, "").replace(/^everything read is in the app\.?\s*/i, "").trim();
  if (!f.captured_at) {
    return { word: "Not read yet", tone: "text-late", ok: false, why: why === "Never read yet." ? "" : why };
  }
  if (f.stale) {
    return { word: "Behind", tone: "text-late", ok: false, why };
  }
  return { word: "Up to date", tone: "text-ok", ok: true, why: f.capture === "failed" ? why : "" };
}

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

function heldOutSentence(sets: TrustAnswerSet[]): string {
  const measured = sets.filter((s) => s.held_out);
  if (measured.length === 0) return "Answer accuracy on questions never used for tuning has not been measured on this server yet.";
  const missing = sets.length - measured.length;
  return `Answer accuracy on questions never used for tuning is measured for ${plural(measured.length, "question set", "question sets")}${missing ? `; ${missing} not yet` : ""} (scores below).`;
}

function alertsSentence(on: number, off: number, unmeasured: number, total: number): string {
  if (total === 0) return "No alert types are set up.";
  const rest: string[] = [];
  if (off) rest.push(`${off} ${off === 1 ? "was" : "were"} checked by hand but ${off === 1 ? "has" : "have"} not passed yet`);
  if (unmeasured) rest.push(`${unmeasured} ${unmeasured === 1 ? "has" : "have"} not been checked yet`);
  return `${on} of the ${total} alert types ${on === 1 ? "is" : "are"} switched on${rest.length ? `; the rest stay off (${rest.join(", ")})` : ""}.`;
}

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

function AlertRow({ a }: { a: TrustAlert }) {
  const under = a.percent != null && a.percent < 90;
  return (
    <tr className="align-top transition-colors duration-150 hover:bg-row-on/60">
      <td className="py-2.5 pr-4 text-fg">
        <span className="font-medium">{a.label}</span>
        {a.note && <span className="mt-0.5 block text-[12px] leading-4 text-muted">{a.note}</span>}
      </td>
      <td className="num px-3 py-2.5 text-right text-fg">{a.judged ?? "–"}</td>
      <td className="num px-3 py-2.5 text-right text-fg">{a.correct ?? "–"}</td>
      <td className={cn("num px-3 py-2.5 text-right font-medium", a.percent == null ? "text-muted" : under ? "text-risk" : "text-ok")}>
        {pctText(a.percent)}
      </td>
      <td className="num px-3 py-2.5 whitespace-nowrap text-muted max-sm:hidden">{a.measured_on ?? ""}</td>
      <td className="py-2.5 pl-3 whitespace-nowrap">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 text-[12px] font-medium",
            a.on ? "text-ok" : "text-muted",
          )}
        >
          {a.on ? <CheckCircle2 className="size-3.5 shrink-0" aria-hidden /> : <PauseCircle className="size-3.5 shrink-0" aria-hidden />}
          {a.on ? (a.home ? "On, Home" : "On") : "Off"}
        </span>
      </td>
    </tr>
  );
}

const problemFirst = (fresh: TrustFreshness[]) => {
  const bad = (f: TrustFreshness) => (!f.captured_at ? 0 : f.stale ? 1 : f.capture === "failed" ? 2 : 3);
  return [...fresh].sort((a, b) => bad(a) - bad(b));
};

type FreshFilter = "all" | "issues";

export default function DataHealthPage() {
  const { data: page, error, retry, at } = useRead<TrustPage>("/api/trust", (signal) => getTrust(signal));
  const [freshFilter, setFreshFilter] = useState<FreshFilter>("all");
  const [refreshing, setRefreshing] = useState(false);
  useEffect(() => {
    if (page || error) setRefreshing(false);
  }, [page, error]);
  const alerts = useMemo(() => {
    const all = page?.alerts ?? [];
    return { on: all.filter((a) => a.on), off: all.filter((a) => !a.on && a.judged != null), unmeasured: all.filter((a) => a.judged == null) };
  }, [page]);
  const refused = error instanceof ApiError && error.status === 404;
  const fresh = useMemo(() => page?.freshness ?? [], [page?.freshness]);
  const rows = page?.autopilot ?? [];
  const orderedFresh = useMemo(() => problemFirst(fresh), [fresh]);
  const issueFresh = useMemo(() => orderedFresh.filter((f) => !f.captured_at || f.stale || f.capture === "failed"), [orderedFresh]);
  const visibleFresh = freshFilter === "issues" ? issueFresh : orderedFresh;
  const upToDateCount = fresh.length - fresh.filter((f) => !f.captured_at || f.stale).length;

  const attention = useMemo(
    () =>
      issueFresh.map((f) => {
        const v = verdictOf(f);
        const name = sourceName(f.system);
        if (!f.captured_at) return `${name} has not been read yet.${v.why ? ` ${v.why}` : ""}`;
        if (f.stale) return `${name} is behind${v.why ? `: ${v.why}` : "."}`;
        return `${name}: ${v.why}`;
      }),
    [issueFresh],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg">
      <Toolbar
        heading
        title="Admin"
        actions={
          !refused ? (
            <ToolbarButton
              onClick={() => {
                setRefreshing(true);
                retry();
              }}
              title="Refresh data health"
              aria-label="Refresh data health"
            >
              <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} strokeWidth={1.9} aria-hidden />
              <span className="max-sm:sr-only">Refresh</span>
            </ToolbarButton>
          ) : undefined
        }
      />
      <SectionTabs section="admin" />
      <PageIntro path="/data-health" className="mx-auto w-full max-w-[1360px] px-4 sm:px-6 lg:px-6" />
      <StoredAge at={at} className="mx-auto w-full max-w-[1360px] px-4 pb-1 sm:px-6" />
      <div className="mx-auto flex w-full max-w-[1360px] flex-col gap-9 overflow-y-auto px-4 py-6 sm:px-6" data-testid="data-health-page">
        {refused ? (
          <p className="text-[14px] text-fg" data-testid="trust-refused">{plainError(new ApiError(403, "forbidden"))}</p>
        ) : error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : !page ? (
          <Pending page label="Reading freshness, scores and switches" />
        ) : (
          <>
            <dl className="order-1 grid grid-cols-2 divide-y divide-border border-y border-border sm:grid-cols-4 sm:divide-x sm:divide-y-0">
              <div className="py-3.5 pr-4 tabular-nums">
                <dt className="text-[12px] font-medium text-muted">Systems up to date</dt>
                <dd className="mt-1 flex items-baseline gap-1.5">
                  <span className={cn("text-[22px] leading-7 font-semibold tracking-[-0.02em]", issueFresh.length > 0 ? "text-late" : "text-ok")}>
                    {fresh.length > 0 ? upToDateCount : "-"}
                  </span>
                  {fresh.length > 0 && <span className="text-[12.5px] text-muted">of {fresh.length}</span>}
                </dd>
              </div>
              <div className="py-3.5 sm:px-5 tabular-nums">
                <dt className="text-[12px] font-medium text-muted">Alert types on</dt>
                <dd className="mt-1 flex items-baseline gap-1.5">
                  <span className="text-[22px] leading-7 font-semibold tracking-[-0.02em] text-fg">
                    {(page.alerts ?? []).length > 0 ? alerts.on.length : "-"}
                  </span>
                  {(page.alerts ?? []).length > 0 && <span className="text-[12.5px] text-muted">of {(page.alerts ?? []).length}</span>}
                </dd>
              </div>
              <div className="py-3.5 sm:px-5 tabular-nums">
                <dt className="text-[12px] font-medium text-muted">Autopilot workflows</dt>
                <dd className="mt-1 flex items-baseline gap-1.5">
                  <span className="text-[22px] leading-7 font-semibold tracking-[-0.02em] text-fg">{rows.length}</span>
                  <span className="text-[12.5px] text-muted">
                    {rows.filter((r) => r.state === "live").length > 0
                      ? `${rows.filter((r) => r.state === "live").length} live`
                      : `${rows.filter((r) => r.draft).length} drafting`}
                  </span>
                </dd>
              </div>
              <div className="py-3.5 sm:pl-5 tabular-nums">
                <dt className="text-[12px] font-medium text-muted">Last checked</dt>
                <dd className="mt-1 text-[15px] leading-7 font-semibold text-fg">{eastern(page.checked_at) || "Just now"}</dd>
              </div>
            </dl>

            <section
              aria-labelledby="trust-attention"
              className="order-1 space-y-2.5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
                <div className="flex items-center gap-2">
                  {attention.length > 0 ? (
                    <CircleAlert className="size-4 shrink-0 text-late" aria-hidden />
                  ) : (
                    <CheckCircle2 className="size-4 shrink-0 text-ok" aria-hidden />
                  )}
                  <h2 id="trust-attention" className="text-[14px] font-semibold tracking-[-0.01em] text-fg">Needs attention</h2>
                </div>
                {page.checked_at && (
                  <p className="text-[12px] text-muted">Checked {eastern(page.checked_at)}.</p>
                )}
              </div>
              <ul className="space-y-1.5 text-[13.5px] leading-6 text-fg" data-testid="trust-summary">
                {attention.length === 0 ? <li>{sourcesSentence(fresh)}</li> : attention.map((l) => <li key={l}>{l}</li>)}
                <li className="text-muted">{heldOutSentence(page.answers ?? [])}</li>
                <li className="text-muted">{alertsSentence(alerts.on.length, alerts.off.length, alerts.unmeasured.length, (page.alerts ?? []).length)}</li>
              </ul>
            </section>

            <section
              aria-labelledby="trust-fresh"
              className="order-2 space-y-2"
            >
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
                <h2 id="trust-fresh" className="text-[14px] font-semibold tracking-[-0.01em] text-fg">Is each system up to date?</h2>
                {issueFresh.length > 0 && (
                  <div role="group" aria-label="Filter systems" className="inline-flex items-center gap-3 text-[12.5px]">
                    <button
                      type="button"
                      aria-pressed={freshFilter === "all"}
                      onClick={() => setFreshFilter("all")}
                      className={cn(
                        "cursor-pointer transition-colors",
                        freshFilter === "all" ? "font-medium text-fg" : "text-muted hover:text-fg",
                      )}
                    >
                      All <span className="num text-muted">{fresh.length}</span>
                    </button>
                    <button
                      type="button"
                      aria-pressed={freshFilter === "issues"}
                      onClick={() => setFreshFilter("issues")}
                      className={cn(
                        "cursor-pointer transition-colors",
                        freshFilter === "issues" ? "font-medium text-fg" : "text-muted hover:text-fg",
                      )}
                    >
                      Needs look <span className="num text-muted">{issueFresh.length}</span>
                    </button>
                  </div>
                )}
              </div>
              {fresh.length === 0 ? (
                <p className="py-3 text-[13.5px] text-muted">This server holds no capture ledger or served data to read.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-[13.5px]" data-testid="trust-freshness">
                    <thead className="border-b border-border text-[12px] text-muted">
                      <tr>
                        <th className="py-2 pr-4 font-medium">Source</th>
                        <th className="px-3 py-2 font-medium">State</th>
                        <th className="px-3 py-2 font-medium">Last read</th>
                        <th className="py-2 pl-3 font-medium max-sm:hidden">Details</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/70">
                      {visibleFresh.map((f) => {
                        const v = verdictOf(f);
                        return (
                          <tr key={f.system} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                            <td className="py-2.5 pr-4 font-medium text-fg">{sourceName(f.system)}</td>
                            <td className="px-3 py-2.5 whitespace-nowrap">
                              <span className={cn("inline-flex items-center gap-1.5 text-[12.5px] font-medium", v.tone)}>
                                {v.ok ? <CheckCircle2 className="size-3.5 shrink-0" aria-hidden /> : <CircleAlert className="size-3.5 shrink-0" aria-hidden />}
                                {v.word}
                              </span>
                            </td>
                            <td className="num px-3 py-2.5 whitespace-nowrap text-fg">
                              {f.captured_at
                                ? f.lag_seconds != null
                                  ? `${eastern(f.captured_at)} (${lagText(f.lag_seconds)} ago)`
                                  : eastern(f.captured_at)
                                : "–"}
                            </td>
                            <td className="py-2.5 pl-3 text-[12.5px] text-muted max-sm:hidden">{v.why}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section
              aria-labelledby="trust-answers"
              className="order-4 space-y-3"
            >
              <div className="border-b border-border pb-2">
                <h2 id="trust-answers" className="text-[14px] font-semibold tracking-[-0.01em] text-fg">Are the answers right?</h2>
                <p className="mt-0.5 text-[12.5px] text-muted">Latest result for each set of questions. &ldquo;Held-out&rdquo; questions were never used to tune the answers.</p>
              </div>
              {(page.answers ?? []).length === 0 ? (
                <p className="py-2 text-[13.5px] text-muted" data-testid="trust-answers-empty">
                  Not measured yet.
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-[13.5px]" data-testid="trust-answers">
                    <thead className="border-b border-border text-[12px] text-muted">
                      <tr>
                        <th className="py-2 pr-4 font-medium">Question set</th>
                        <th className="px-3 py-2 font-medium">Development</th>
                        <th className="px-3 py-2 font-medium">Held-out</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/70">
                      {(page.answers ?? []).map((s) => (
                        <tr key={s.name} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                          <td className="py-2.5 pr-4 font-medium text-fg">{questionSetLabel(s.name)}</td>
                          <Score s={s.dev} label="Development" />
                          <Score s={s.held_out} label="Held-out" />
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="border-t border-border pt-4">
                <AnswersRight />
              </div>
            </section>

            <section
              aria-labelledby="trust-autopilot"
              className="order-4 space-y-2.5"
            >
              <div className="border-b border-border pb-2">
                <h2 id="trust-autopilot" className="text-[14px] font-semibold tracking-[-0.01em] text-fg">What the autopilot may do</h2>
                <p className="mt-0.5 text-[13.5px] text-fg">{autopilotSentence(rows)}</p>
                <p className="mt-0.5 text-[12.5px] text-muted">
                  Practice score: replayed days the rules were tuned on.{" "}
                  {page.autopilot_gate
                    ? `Gate: at least ${page.autopilot_gate.need_judged} held-out items judged (fresh days, or past days never tuned on), ${page.autopilot_gate.need_percent}% or more right, before a workflow may act on approval.`
                    : "Gate: the rules cannot be read, so no workflow acts."}
                </p>
              </div>
              {rows.length === 0 ? (
                <p className="py-2 text-[13.5px] text-muted">{page.autopilot_note}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-[13.5px]" data-testid="trust-autopilot">
                    <thead className="border-b border-border text-[12px] text-muted">
                      <tr>
                        <th className="py-2 pr-4 font-medium">Workflow</th>
                        <th className="px-3 py-2 font-medium">For</th>
                        <th className="px-3 py-2 font-medium">Mode</th>
                        <th className="px-3 py-2 text-right font-medium">Practice</th>
                        <th className="py-2 pl-3 font-medium">
                          {page.autopilot_gate ? `Gate: held-out items at ≥${page.autopilot_gate.need_percent}%` : "Gate"}
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/70">
                      {rows.map((r) => {
                        const shown = rowStateShown(r, false);
                        return (
                          <tr key={`${r.role}/${r.workflow}`} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                            <td className="py-2.5 pr-4 font-medium text-fg">{workflowLabel(r.workflow)}</td>
                            <td className="px-3 py-2.5 text-muted">{whoOf(r)}</td>
                            <td className="px-3 py-2.5 whitespace-nowrap">
                              <span
                                className={cn(
                                  "inline-flex items-center gap-1.5 text-[12px] font-medium",
                                  r.draft
                                    ? "text-new"
                                    : shown.status === "ok"
                                      ? "text-ok"
                                      : "text-muted",
                                )}
                              >
                                {r.draft ? (
                                  <FileEdit className="size-3.5 shrink-0" aria-hidden />
                                ) : shown.status === "ok" ? (
                                  <CheckCircle2 className="size-3.5 shrink-0" aria-hidden />
                                ) : (
                                  <PauseCircle className="size-3.5 shrink-0" aria-hidden />
                                )}
                                {shown.label}
                              </span>
                            </td>
                            <td className="num px-3 py-2.5 text-right text-fg">
                              {r.dev ? pctText(r.dev.percent) : "–"}
                              {r.dev && <span className="block text-[12px] text-muted">{r.dev.matched} of {r.dev.judged}</span>}
                            </td>
                            <td className="num py-2.5 pl-3 text-[12.5px] text-muted">
                              {countdownText(r.countdown) ?? <span className="text-muted">not scored yet</span>}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section
              aria-labelledby="trust-alerts"
              className="order-4 space-y-2.5"
            >
              <div className="border-b border-border pb-2">
                <h2 id="trust-alerts" className="text-[14px] font-semibold tracking-[-0.01em] text-fg">Do the alerts get it right?</h2>
                {page.alert_rule && <p className="mt-0.5 text-[12.5px] leading-5 text-muted">{page.alert_rule}</p>}
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-[13.5px]" data-testid="trust-alerts">
                  <thead className="border-b border-border text-[12px] text-muted">
                    <tr>
                      <th className="py-2 pr-4 font-medium">Alert</th>
                      <th className="px-3 py-2 text-right font-medium">Judged</th>
                      <th className="px-3 py-2 text-right font-medium">Right</th>
                      <th className="px-3 py-2 text-right font-medium">Right when shown</th>
                      <th className="px-3 py-2 font-medium max-sm:hidden">Checked</th>
                      <th className="py-2 pl-3 font-medium">Shown</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/70">
                    {[...alerts.on, ...alerts.off].map((a) => (
                      <AlertRow key={a.type} a={a} />
                    ))}
                  </tbody>
                </table>
              </div>
              {alerts.unmeasured.length > 0 && (
                <details className="border-t border-border pt-3 text-[13px]">
                  <summary className="cursor-pointer text-[12.5px] font-medium text-muted hover:text-fg">
                    {alerts.unmeasured.length} types not measured yet (off until hand-checked)
                  </summary>
                  <ul className="mt-2 divide-y divide-border/70">
                    {alerts.unmeasured.map((a) => (
                      <li key={a.type} className="py-2">
                        <span className="font-medium text-fg">{a.label}</span>
                        {a.note && <span className="mt-0.5 block text-[12px] text-muted">{a.note}</span>}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </section>

            <section
              aria-labelledby="trust-switches"
              className="order-4 space-y-2.5"
            >
              <div className="border-b border-border pb-2">
                <h2 id="trust-switches" className="text-[14px] font-semibold tracking-[-0.01em] text-fg">Switches for changing other systems</h2>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-left text-[13.5px]" data-testid="trust-switches">
                  <tbody className="divide-y divide-border/70">
                    {(page.switches ?? []).map((s) => (
                      <tr key={s.system} className="transition-colors duration-150 hover:bg-row-on/60">
                        <td className="w-52 py-2.5 pr-4 font-medium text-fg">{s.system}</td>
                        <td className="w-28 px-3 py-2.5 whitespace-nowrap">
                          <span
                            className={cn(
                              "inline-flex items-center gap-1.5 text-[12px] font-medium",
                              s.state === "on" ? "text-ok" : "text-muted",
                            )}
                          >
                            {s.state === "on" ? <CheckCircle2 className="size-3.5 shrink-0" aria-hidden /> : <PauseCircle className="size-3.5 shrink-0" aria-hidden />}
                            {s.state === "on" ? "On" : s.state === "off" ? "Off" : "Read only"}
                          </span>
                        </td>
                        <td className="py-2.5 pl-3 text-[12.5px] text-muted">{s.words}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}
        {!refused && (
          <section
            aria-labelledby="data-health-title"
            className="order-3 space-y-2"
            data-testid="data-health-records"
          >
            <DataHealthSection />
          </section>
        )}
      </div>
    </div>
  );
}
