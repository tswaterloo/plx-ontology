"use client";

import { AlertTriangle, CheckCircle2, ChevronDown, CircleAlert, HelpCircle, RefreshCw } from "lucide-react";
import { useState } from "react";
import { eastern, opsLabel, opsLine, SectionTabs } from "@/components/shell";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { StoredAge } from "@/components/ui/stored-age";
import { LoadProblem } from "@/components/ui/notice";
import { getOpsAlarms, type OpsAlarms, type OpsCheck, type OpsIncident, type OpsQueue } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { cn } from "@/lib/cn";
import { problemParts } from "@/lib/format";
import { noticeKind, plainError } from "@/lib/errors";

type Area = "Data freshness" | "Services" | "Postgres" | "Machine" | "Backups" | "Other";
const AREAS: Area[] = ["Data freshness", "Services", "Postgres", "Machine", "Backups", "Other"];

function areaOf(key: string): Area {
  const k = String(key ?? "");
  if (k.startsWith("unit:") || k.startsWith("units:") || k === "ingest_start" || k.startsWith("jobq_")) return "Services";
  if (k.startsWith("capture:") || k.startsWith("delta_") || k === "pg_follow" || k.endsWith("_last_pass") || k === "search_follow_last_pass") return "Data freshness";
  if (k.startsWith("pg_")) return "Postgres";
  if (k.startsWith("disk_") || k.startsWith("memory_")) return "Machine";
  if (k === "backup" || k === "restore_test") return "Backups";
  return "Other";
}

const NEXT: Record<Area, string> = {
  "Data freshness": "Ask IT to check the data refresh and the capture it names",
  Services: "Ask IT to read the job's log and restart it once the cause is fixed",
  Postgres: "Ask IT to close the idle database sessions",
  Machine: "Ask IT to free disk space or stop the largest job",
  Backups: "Ask IT to check the backup log and run the backup again",
  Other: "Ask IT to look at it; the details say what stopped",
};

function span(fromIso?: string | null, toIso?: string | null): string {
  const from = fromIso ? Date.parse(fromIso) : NaN;
  const to = toIso ? Date.parse(toIso) : Date.now();
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return "";
  const m = (to - from) / 60_000;
  if (m < 120) return `${Math.max(1, Math.round(m))} min`;
  if (m < 36 * 60) return `${Math.round(m / 60)} h`;
  return `${Math.round(m / 1440)} days`;
}

function gb(mb?: number | null): string {
  return mb == null ? "?" : `${(mb / 1024).toFixed(1)} GB`;
}

function secs(s?: number | null): string {
  if (s == null) return "";
  if (s < 120) return `${Math.round(s)} s`;
  if (s < 7200) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

function QueueSection({ queue }: { queue?: OpsQueue | null }) {
  if (!queue || !queue.configured) return null;
  const running = queue.running ?? [];
  const waiting = queue.waiting ?? [];
  const dead = queue.dead ?? [];
  const mem = queue.memory;
  const hour = queue.last_hour ?? {};
  const hasRows = running.length + (queue.unmanaged ?? []).length + waiting.length + dead.length > 0;
  return (
    <section
      data-testid="ops-queue"
      className="space-y-2"
    >
      <div className="border-b border-border pb-2">
        <h2 className="text-[13.5px] font-semibold text-fg">Job queue</h2>
        {!queue.fresh ? (
          <p className="mt-1 text-[13px] font-semibold text-late" data-testid="ops-queue-stale">
            The queue worker has stopped{queue.written_at ? `: last report ${eastern(queue.written_at)}` : ""}. Queued jobs do not start until it runs again.
          </p>
        ) : null}
        <p className="mt-0.5 text-[12.5px] text-muted">
          {queue.queued ?? 0} queued · {running.length} running · {hour.refused ?? 0} refusals and {hour.dead ?? 0} dead in the last hour
          {mem ? ` · ${gb(mem.available_mb)} free, ${gb(mem.promised_mb)} promised to running jobs, ${gb(mem.headroom_mb)} headroom above a ${gb(mem.floor_mb)} floor` : ""}
        </p>
      </div>
      {hasRows ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[13.5px]">
            <tbody className="divide-y divide-border/70">
              {running.map((j) => (
                <tr key={`r${j.id}`} className="transition-colors duration-150 hover:bg-row-on/60">
                  <td className="py-2.5 pr-4 font-medium text-fg">{j.class}</td>
                  <td className="px-3 py-2.5 text-muted">{j.state === "preempting" ? "stopping (preempted)" : "running"} {secs(j.running_s)}</td>
                  <td className="px-3 py-2.5 tabular-nums text-muted">{gb(j.current_mb)} of {gb(j.mem_mb)}</td>
                  <td className="py-2.5 pl-3 text-[12.5px] text-muted">waited {secs(j.waited_s) || "0 s"}</td>
                </tr>
              ))}
              {(queue.unmanaged ?? []).map((u) => (
                <tr key={`u${u.unit}`} className="transition-colors duration-150 hover:bg-row-on/60">
                  <td className="py-2.5 pr-4 font-medium text-fg">{u.class}</td>
                  <td className="px-3 py-2.5 text-muted">running outside the queue</td>
                  <td className="px-3 py-2.5 tabular-nums text-muted">{gb(u.current_mb)} of {gb(u.mem_mb)}</td>
                  <td className="py-2.5 pl-3" />
                </tr>
              ))}
              {waiting.map((j) => (
                <tr key={`w${j.id}`} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                  <td className="py-2.5 pr-4 font-medium text-fg">{j.class}</td>
                  <td className="px-3 py-2.5 text-muted">waiting {secs(j.waiting_s)}{j.not_before_s ? `, retry in ${secs(j.not_before_s)}` : ""}</td>
                  <td className="px-3 py-2.5 tabular-nums text-muted">{j.refusals} refused</td>
                  <td className="py-2.5 pl-3 text-[12.5px] text-muted">{j.last_refusal ?? ""}</td>
                </tr>
              ))}
              {dead.map((j) => (
                <tr key={`d${j.id}`} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                  <td className="py-2.5 pr-4 font-medium text-late">{j.class}</td>
                  <td className="px-3 py-2.5 text-late">gave up after {j.attempts} tries</td>
                  <td className="py-2.5 pl-3 text-[12.5px] text-muted" colSpan={2}>{j.last_error ?? j.result ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {(queue.events ?? []).length > 0 ? (
        <p className="border-t border-border pt-2.5 text-[12.5px] text-muted" data-testid="ops-queue-events">
          Events: {(queue.events ?? []).map((e) => `${e.topic} ${e.n} ${e.state === "ready" ? "waiting" : e.state === "inflight" ? "being handled" : "dead"}${e.state === "ready" && e.oldest_s ? ` (oldest ${secs(e.oldest_s)})` : ""}`).join(" · ")}
        </p>
      ) : null}
      {running.length + waiting.length + dead.length === 0 ? (
        <p className="py-2 text-[13px] text-muted">Nothing queued or running.</p>
      ) : null}
    </section>
  );
}

const AREA_TONE: Record<Area, string> = {
  Machine: "text-late",
  Postgres: "text-late",
  Services: "text-risk",
  "Data freshness": "text-risk",
  Backups: "text-muted",
  Other: "text-muted",
};

function AreaTag({ area }: { area: Area }) {
  const Icon = area === "Machine" || area === "Postgres" ? CircleAlert : area === "Services" || area === "Data freshness" ? AlertTriangle : HelpCircle;
  return (
    <span className={cn("inline-flex items-center gap-1 text-[12px] font-medium", AREA_TONE[area])}>
      <Icon className="size-3 shrink-0" aria-hidden />
      {area}
    </span>
  );
}

function cleanOwner(owner?: string | null): string {
  if (!owner) return "";
  const cleaned = owner.replace(/\s*\([^)]*\)\s*/g, " ").replace(/\s+/g, " ").trim();
  return cleaned || owner.trim();
}

function isInternalDevNote(key: string, line: string): boolean {
  const k = String(key ?? "").toLowerCase();
  if (
    k.startsWith("code_") ||
    k.startsWith("file_") ||
    k.startsWith("line_limit") ||
    k.startsWith("lint") ||
    k.startsWith("build_")
  ) {
    return true;
  }
  return /\b(lines?\s+over|line\s+limit|\.(py|tsx?|jsx?|json)\b|disk\s+request\s*#\d+|request\s*#\d+|pr\s*#\d+|pull\s+request\s*#\d+)\b/i.test(line);
}

function plainCardCopy(i: OpsIncident): { headline: string; ownerLabel: string; techNotes: string[] } {
  const parts = problemParts(opsLine(i.key, i.line));
  const rawHead = parts.head;
  const ownerRaw = i.known?.owner?.trim() ?? "";
  const ownerLabel = cleanOwner(ownerRaw);
  const techNotes: string[] = [];

  let headline = rawHead;
  if (isInternalDevNote(i.key, rawHead)) {
    headline = /\b(lines?\s+over|line\s+limit|\.(py|tsx?|jsx?)\b|code_|file_|lint)\b/i.test(`${i.key} ${rawHead}`)
      ? "Internal code maintenance check needs review"
      : rawHead.replace(/\s*\([^)]*#\d+[^)]*\)/g, "").trim() || "Internal maintenance item";
    techNotes.push(rawHead);
  }
  if (parts.more) techNotes.push(parts.more);
  if (ownerRaw && ownerRaw !== ownerLabel) techNotes.push(`Owner note: ${ownerRaw}`);
  if (i.known?.why) techNotes.push(i.known.why);
  return { headline, ownerLabel, techNotes };
}

const STATE_WORD: Record<OpsCheck["state"], string> = { breach: "Problem", ok: "OK", unknown: "Unknown" };
function CheckStateTag({ state }: { state: OpsCheck["state"] }) {
  const word = STATE_WORD[state] ?? "Unknown";
  if (state === "breach") {
    return (
      <span className="mt-0.5 inline-flex w-[76px] shrink-0 items-center gap-1.5 text-[12px] font-semibold text-late">
        <CircleAlert className="size-3.5 shrink-0" aria-hidden />
        {word}
      </span>
    );
  }
  if (state === "ok") {
    return (
      <span className="mt-0.5 inline-flex w-[76px] shrink-0 items-center gap-1.5 text-[12px] font-medium text-ok">
        <CheckCircle2 className="size-3.5 shrink-0" aria-hidden />
        {word}
      </span>
    );
  }
  return (
    <span className="mt-0.5 inline-flex w-[76px] shrink-0 items-center gap-1.5 text-[12px] font-medium text-muted">
      <HelpCircle className="size-3.5 shrink-0" aria-hidden />
      {word}
    </span>
  );
}

export default function OpsPage() {
  const { data: alarms, error, retry, at } = useRead<OpsAlarms>("/api/ops/status", (signal) => getOpsAlarms(signal));
  const checks = alarms?.checks ?? [];
  const count = (s: OpsCheck["state"]) => checks.filter((c) => c.state === s).length;
  const [detail, setDetail] = useState(false);
  const [onlyFailingChecks, setOnlyFailingChecks] = useState(false);

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg">
      <Toolbar
        heading
        title="Admin"
        actions={
          <ToolbarButton onClick={() => retry()} title="Refresh reliability status" aria-label="Refresh reliability status">
            <RefreshCw className={cn("size-3.5", at !== null && "animate-spin")} strokeWidth={1.9} aria-hidden />
            <span className="max-sm:sr-only">Refresh</span>
          </ToolbarButton>
        }
      />
      <SectionTabs section="admin" />
      <PageIntro path="/ops" className="mx-auto w-full max-w-[1360px] px-4 sm:px-6 lg:px-6" />
      <StoredAge at={at} className="mx-auto w-full max-w-[1360px] px-4 pt-2 pb-1 sm:px-6" />
      <div className="mx-auto w-full max-w-[1360px] space-y-6 overflow-y-auto px-4 py-5 sm:px-6" data-testid="ops-page">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : !alarms ? (
          <Pending page label="Reading the reliability checks" />
        ) : !alarms.configured ? (
          <p className="text-[13.5px] text-muted">This server has no live state to check.</p>
        ) : (
          <>
            {alarms.maintenance?.state === "in_window" && (
              <div
                className="border-y border-border border-l-2 border-l-risk bg-bg-subtle/60 px-4 py-3 text-[13px] text-fg"
                data-testid="ops-maintenance"
              >
                <span className="font-medium">Paused for maintenance until {eastern(alarms.maintenance.until)}: {alarms.maintenance.reason}.</span>
                <span className="ml-1 text-muted">What it stops is listed under Paused on purpose; past that time it opens again and pages.</span>
              </div>
            )}
            <section className="space-y-8" data-testid="ops-plain">
              <dl className="grid grid-cols-2 divide-y divide-border border-y border-border sm:grid-cols-4 sm:divide-x sm:divide-y-0" data-testid="ops-kpi-strip">
                <div className="py-3.5 pr-4">
                  <dt className="text-[12px] font-medium text-muted">Status</dt>
                  <dd className="mt-1 flex items-center gap-2 text-[14px] font-semibold">
                    {!alarms.running || alarms.open.length > 0 ? (
                      <CircleAlert className="size-4 shrink-0 text-late" aria-hidden />
                    ) : (
                      <CheckCircle2 className="size-4 shrink-0 text-ok" aria-hidden />
                    )}
                    <span className={!alarms.running || alarms.open.length > 0 ? "text-late" : "text-ok"}>
                      {!alarms.running ? "Checks stopped" : alarms.open.length > 0 ? "Needs attention" : "Operational"}
                    </span>
                  </dd>
                </div>
                <div className="py-3.5 sm:px-5 tabular-nums">
                  <dt className="text-[12px] font-medium text-muted">Open issues</dt>
                  <dd className={cn("mt-1 text-[22px] leading-7 font-semibold tracking-[-0.02em]", alarms.open.length > 0 ? "text-late" : "text-fg")}>
                    {alarms.open.length}
                  </dd>
                </div>
                <div className="py-3.5 sm:px-5 tabular-nums">
                  <dt className="text-[12px] font-medium text-muted">Acknowledged / Parked</dt>
                  <dd className="mt-1 text-[22px] leading-7 font-semibold tracking-[-0.02em] text-fg">
                    {(alarms.known?.length ?? 0) + (alarms.paused?.length ?? 0)}
                  </dd>
                </div>
                <div className="py-3.5 sm:pl-5 tabular-nums">
                  <dt className="text-[12px] font-medium text-muted">Last checked</dt>
                  <dd className="mt-1 text-[14px] font-semibold text-fg">
                    {alarms.checked_at ? eastern(alarms.checked_at) : "Never"}
                    <span className="mt-0.5 block text-[12px] font-normal text-muted">
                      {alarms.running ? `Every ${alarms.every_minutes ?? 5} minutes` : "Not running"}
                    </span>
                  </dd>
                </div>
              </dl>

              <div className="space-y-2">
                {!alarms.running && alarms.open.length === 0 ? (
                  <div
                    className="flex flex-wrap items-start justify-between gap-4 border-y border-border border-l-2 border-l-late bg-bg-subtle/60 px-4 py-4"
                    data-testid="ops-checks-paused"
                  >
                    <div className="min-w-0 flex-1 space-y-1">
                      <h2 className="flex items-center gap-2 text-[14.5px] font-semibold text-fg">
                        <AlertTriangle className="size-4 shrink-0 text-late" aria-hidden />
                        <span>Monitoring checks are paused</span>
                      </h2>
                      <p className="text-[13px] leading-5 text-muted">
                        Checks are not currently running, so new issues will not appear until monitoring resumes.
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => retry()}
                      data-testid="ops-refresh-status"
                      className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border border-border-strong bg-surface px-3 text-[12.5px] font-medium text-fg transition-colors hover:bg-surface-2 cursor-pointer"
                    >
                      <RefreshCw className={cn("size-3.5 text-muted", at !== null && "animate-spin")} strokeWidth={1.9} aria-hidden />
                      <span>Refresh status</span>
                    </button>
                  </div>
                ) : (
                  <>
                    {!alarms.running && (
                      <div
                        className="flex flex-wrap items-center justify-between gap-3 border-y border-border border-l-2 border-l-late bg-bg-subtle/60 px-4 py-3"
                        data-testid="ops-checks-paused"
                      >
                        <div className="min-w-0 flex-1 space-y-0.5">
                          <p className="flex items-center gap-1.5 text-[13.5px] font-semibold text-fg">
                            <AlertTriangle className="size-3.5 shrink-0 text-late" aria-hidden />
                            <span>Monitoring checks are paused</span>
                          </p>
                          <p className="text-[12.5px] text-muted">
                            Checks are not currently running, so new issues will not appear until monitoring resumes.
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => retry()}
                          data-testid="ops-refresh-status"
                          className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md border border-border-strong bg-surface px-2.5 text-[12px] font-medium text-fg transition-colors hover:bg-surface-2 cursor-pointer"
                        >
                          <RefreshCw className={cn("size-3 text-muted", at !== null && "animate-spin")} strokeWidth={1.9} aria-hidden />
                          <span>Refresh status</span>
                        </button>
                      </div>
                    )}
                    <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border pb-2">
                      <h2 className="text-[14px] font-semibold text-fg">
                        {alarms.open.length === 0 ? "Nothing needs attention." : `${alarms.open.length} ${alarms.open.length === 1 ? "thing needs" : "things need"} attention.`}
                      </h2>
                      <p className="text-[12.5px] text-muted">
                        {alarms.running
                          ? `Checked ${alarms.checked_at ? eastern(alarms.checked_at) : "just now"}, and again every ${alarms.every_minutes ?? 5} minutes.`
                          : "Checks stopped; showing previously open issues."}
                      </p>
                    </div>
                  </>
                )}
                {alarms.open.length === 0 ? (
                  alarms.running ? (
                    <p className="py-3 text-[13.5px] text-muted">
                      Every check that can report passed on the last pass.
                    </p>
                  ) : null
                ) : (
                  <ul className="divide-y divide-border/70" data-testid="ops-plain-list">
                    {alarms.open.slice(0, 6).map((i: OpsIncident) => {
                      const area = areaOf(i.key);
                      const dur = span(i.opened_at);
                      const copy = plainCardCopy(i);
                      return (
                        <li key={i.key} className="py-3.5 transition-colors duration-150 hover:bg-row-on/60">
                          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                            <p className="min-w-0 flex-1 text-[14px] font-medium leading-5 text-fg">
                              {copy.headline}
                            </p>
                            <div className="flex shrink-0 flex-wrap items-center gap-x-2 text-[12.5px] text-muted">
                              <AreaTag area={area} />
                              {dur && (
                                <>
                                  <span aria-hidden>·</span>
                                  <span className="tabular-nums">open {dur}</span>
                                </>
                              )}
                              {copy.ownerLabel && (
                                <>
                                  <span aria-hidden>·</span>
                                  <span>With {copy.ownerLabel}</span>
                                </>
                              )}
                            </div>
                          </div>
                          <p className="mt-1 text-[12.5px] leading-5 text-muted">
                            {copy.ownerLabel ? `With ${copy.ownerLabel}.` : NEXT[area]}
                          </p>
                          {copy.techNotes.length > 0 && (
                            <details className="mt-2 text-[12.5px] text-muted" data-testid={`ops-card-tech-${i.key}`}>
                              <summary className="cursor-pointer font-medium text-muted hover:text-fg">Technical details</summary>
                              <ul className="mt-1.5 space-y-0.5 border-l border-border-strong pl-3">
                                {copy.techNotes.map((note, idx) => (
                                  <li key={idx}>{note}</li>
                                ))}
                              </ul>
                            </details>
                          )}
                        </li>
                      );
                    })}
                    {alarms.open.length > 6 && (
                      <li className="py-3 text-[13px] text-muted">
                        <button
                          type="button"
                          onClick={() => setDetail(true)}
                          className="cursor-pointer font-medium text-fg underline decoration-border-strong underline-offset-2 hover:decoration-fg"
                        >
                          Show the other {alarms.open.length - 6} in technical details
                        </button>
                      </li>
                    )}
                  </ul>
                )}
              </div>

              {(alarms.known?.length ?? 0) > 0 && (
                <div
                  className="space-y-2"
                  data-testid="ops-known"
                >
                  <div className="border-b border-border pb-2">
                    <h3 className="text-[14px] font-semibold text-fg">Already with someone ({alarms.known!.length})</h3>
                  </div>
                  <ul className="divide-y divide-border/70">
                    {alarms.known!.map((i: OpsIncident) => {
                      const area = areaOf(i.key);
                      const copy = plainCardCopy(i);
                      return (
                        <li key={i.key} className="py-3.5 transition-colors duration-150 hover:bg-row-on/60">
                          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                            <p className="min-w-0 flex-1 text-[14px] font-medium text-fg">{copy.headline}</p>
                            <div className="flex shrink-0 items-center gap-x-2 text-[12.5px] text-muted">
                              <AreaTag area={area} />
                              {copy.ownerLabel && (
                                <>
                                  <span aria-hidden>·</span>
                                  <span>With {copy.ownerLabel}</span>
                                </>
                              )}
                            </div>
                          </div>
                          <p className="mt-1 text-[12.5px] leading-5 text-muted">
                            With {copy.ownerLabel}; it shows as a problem again after {eastern(i.known?.until)} if still open.
                          </p>
                          {copy.techNotes.length > 0 && (
                            <details className="mt-2 text-[12.5px] text-muted" data-testid={`ops-known-tech-${i.key}`}>
                              <summary className="cursor-pointer font-medium text-muted hover:text-fg">Technical details</summary>
                              <ul className="mt-1.5 space-y-0.5 border-l border-border-strong pl-3">
                                {copy.techNotes.map((note, idx) => (
                                  <li key={idx}>{note}</li>
                                ))}
                              </ul>
                            </details>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}

              <div>
                <button
                  type="button"
                  onClick={() => setDetail((v) => !v)}
                  aria-expanded={detail}
                  data-testid="ops-detail-toggle"
                  className="inline-flex h-8 items-center gap-2 rounded-md border border-border-strong bg-surface px-3 text-[12.5px] font-medium text-fg transition-[background-color,border-color,transform] duration-150 hover:bg-surface-2 active:scale-[0.98] cursor-pointer"
                >
                  <span>{detail ? "Hide technical details" : "Technical details"}</span>
                  <ChevronDown className={cn("size-3.5 text-muted transition-transform", detail && "rotate-180")} strokeWidth={2} aria-hidden />
                </button>
              </div>
            </section>

            {detail && (
              <div className="space-y-8 border-t border-border pt-6">
                <div className="space-y-3">
                  <dl className="grid grid-cols-2 divide-y divide-border border-y border-border text-[13px] sm:grid-cols-4 sm:divide-x sm:divide-y-0" data-testid="ops-summary">
                    <div className="py-3 pr-4">
                      <dt className="text-[12px] font-medium text-muted">Open problems</dt>
                      <dd className={cn("mt-0.5 text-[20px] font-semibold tabular-nums", alarms.open.length ? "text-late" : "text-fg")}>{alarms.open.length}</dd>
                    </div>
                    <div className="py-3 sm:px-5">
                      <dt className="text-[12px] font-medium text-muted">Checks</dt>
                      <dd className="mt-0.5 tabular-nums">
                        <span className="text-[20px] font-semibold text-fg">{checks.length}</span>
                        <span className="ml-2 text-[12.5px] text-muted">
                          {count("ok")} OK · <span className={count("breach") ? "text-late" : ""}>{count("breach")} problem</span> · {count("unknown")} unknown
                        </span>
                      </dd>
                    </div>
                    <div className="py-3 sm:px-5">
                      <dt className="text-[12px] font-medium text-muted">Last pass</dt>
                      <dd className={cn("mt-0.5 font-medium", alarms.running ? "text-fg" : "font-semibold text-late")} data-testid="ops-checked">
                        {alarms.checked_at ? eastern(alarms.checked_at) : "never"}
                        <span className="block text-[12px] font-normal text-muted">
                          {alarms.running ? `every ${alarms.every_minutes ?? 5} minutes` : "the checks have stopped: a new problem would not show"}
                        </span>
                      </dd>
                    </div>
                    <div className="py-3 sm:pl-5">
                      <dt className="text-[12px] font-medium text-muted">Recovered lately</dt>
                      <dd className="mt-0.5 text-[20px] font-semibold tabular-nums text-fg">{alarms.recovered.length}</dd>
                    </div>
                  </dl>
                  {alarms.open.length > 0 && (
                    <p className="flex flex-wrap gap-x-5 gap-y-1 text-[12.5px] text-muted" data-testid="ops-by-area">
                      <span className="font-medium text-fg">Open by area</span>
                      {AREAS.map((a) => [a, alarms.open.filter((i) => areaOf(i.key) === a).length] as const)
                        .filter(([, n]) => n > 0)
                        .sort((x, y) => y[1] - x[1])
                        .map(([a, n]) => (
                          <span key={a} className="whitespace-nowrap">
                            {a} <span className="num font-semibold text-late">{n}</span>
                          </span>
                        ))}
                      {(() => {
                        const oldest = [...alarms.open].sort((x, y) => Date.parse(x.opened_at) - Date.parse(y.opened_at))[0];
                        return oldest ? (
                          <span className="min-w-0 truncate" title={oldest.line}>
                            Oldest open {span(oldest.opened_at)}: <span className="text-fg">{opsLine(oldest.key, oldest.line)}</span>
                          </span>
                        ) : null;
                      })()}
                    </p>
                  )}
                  <p className="text-[12.5px] text-muted">
                    Shown to the owner and the Team readers, here and in the red line at the top of every page. Nothing is paged or mailed.
                  </p>
                </div>

                <section className="space-y-2">
                  <div className="border-b border-border pb-2">
                    <h2 className="text-[13.5px] font-semibold text-fg">Open problems</h2>
                  </div>
                  {alarms.open.length === 0 ? (
                    <p className="py-2 text-[13.5px] text-muted">
                      {alarms.running
                        ? "None. Every check that can say so passed on the last pass."
                        : "Monitoring checks are paused. No open problems were recorded before checks stopped."}
                    </p>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-[13.5px]" data-testid="ops-open">
                        <thead className="border-b border-border text-[12px] text-muted">
                          <tr>
                            <th className="py-2 pr-4 font-medium">Problem</th>
                            <th className="px-3 py-2 font-medium max-sm:hidden">Area</th>
                            <th className="px-3 py-2 font-medium">Open for</th>
                            <th className="py-2 pl-3 font-medium max-md:hidden">Next step</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border/70">
                          {alarms.open.map((i: OpsIncident) => (
                            <tr key={i.key} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                              <td className="py-2.5 pr-4 text-late" title={i.key}>
                                {(() => {
                                  const { head, more } = problemParts(opsLine(i.key, i.line));
                                  return (
                                    <>
                                      <span className="font-medium">{head}</span>
                                      {more && (
                                        <details className="mt-1 text-[12.5px] text-muted">
                                          <summary className="cursor-pointer font-medium hover:text-fg">Details</summary>
                                          <p className="mt-1 border-l border-border-strong pl-2.5">{more}</p>
                                        </details>
                                      )}
                                    </>
                                  );
                                })()}
                              </td>
                              <td className="px-3 py-2.5 text-muted max-sm:hidden">{areaOf(i.key)}</td>
                              <td className="px-3 py-2.5 whitespace-nowrap tabular-nums text-fg" title={`since ${eastern(i.opened_at)}`}>
                                {span(i.opened_at)}
                                <span className="block text-[12px] text-muted">since {eastern(i.opened_at)}</span>
                              </td>
                              <td className="py-2.5 pl-3 text-[12.5px] text-muted max-md:hidden">
                                {i.known ? `${i.known.why} With ${i.known.owner}.` : NEXT[areaOf(i.key)]}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>

                {(alarms.paused?.length ?? 0) > 0 && (
                  <section className="space-y-2">
                    <div className="border-b border-border pb-2">
                      <h2 className="text-[13.5px] font-semibold text-fg">Paused on purpose</h2>
                      <p className="mt-0.5 text-[12.5px] text-muted">Not mailed until the pause ends; then open again if still broken.</p>
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-[13.5px]" data-testid="ops-paused">
                        <tbody className="divide-y divide-border/70">
                          {alarms.paused!.map((i) => (
                            <tr key={i.key} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                              <td className="py-2.5 pr-4 font-medium text-fg" title={i.key}>{opsLine(i.key, i.line)}</td>
                              <td className="px-3 py-2.5 whitespace-nowrap text-[12.5px] text-muted">until {eastern(i.paused?.until)}</td>
                              <td className="py-2.5 pl-3 text-[12.5px] text-muted max-sm:hidden">{i.paused?.why || "no reason given"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                )}

                {alarms.notify && (
                  <p className="text-[12.5px] text-muted" data-testid="ops-mail">
                    {alarms.notify.sending
                      ? `Problems are mailed to ${alarms.notify.recipients ?? 0} ${alarms.notify.recipients === 1 ? "person" : "people"}; a daily mail at 8 AM ET says the checks run.`
                      : `Problems are not mailed (${alarms.notify.why_not || "off"}): they show only here.`}
                    {(alarms.notify.pending ?? 0) > 0 && ` ${alarms.notify.pending} ${alarms.notify.pending === 1 ? "mail" : "mails"} not sent yet, retried every pass.`}
                  </p>
                )}

                {alarms.recovered.length > 0 && (
                  <section className="space-y-2">
                    <div className="border-b border-border pb-2">
                      <h2 className="text-[13.5px] font-semibold text-fg">Recovered</h2>
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-[13.5px]" data-testid="ops-recovered">
                        <tbody className="divide-y divide-border/70">
                          {alarms.recovered.map((i) => (
                            <tr key={`${i.key}-${i.opened_at}`} className="align-top transition-colors duration-150 hover:bg-row-on/60">
                              <td className="py-2.5 pr-4 font-medium text-fg" title={i.key}>{opsLine(i.key, i.line)}</td>
                              <td className="px-3 py-2.5 whitespace-nowrap text-[12.5px] text-muted">
                                {eastern(i.opened_at)} to {eastern(i.closed_at)}
                              </td>
                              <td className="py-2.5 pl-3 whitespace-nowrap text-[12.5px] tabular-nums text-muted">{span(i.opened_at, i.closed_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                )}

                <QueueSection queue={alarms.queue} />

                <section data-testid="ops-checks" className="space-y-4">
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
                    <h2 className="text-[13.5px] font-semibold text-fg">Every check, last pass</h2>
                    {count("breach") > 0 && (
                      <div role="group" aria-label="Filter checks" className="inline-flex items-center gap-3 text-[12.5px]">
                        <button
                          type="button"
                          aria-pressed={!onlyFailingChecks}
                          onClick={() => setOnlyFailingChecks(false)}
                          className={cn(
                            "cursor-pointer transition-colors",
                            !onlyFailingChecks ? "font-medium text-fg" : "text-muted hover:text-fg",
                          )}
                        >
                          All <span className="num text-muted">{checks.length}</span>
                        </button>
                        <button
                          type="button"
                          aria-pressed={onlyFailingChecks}
                          onClick={() => setOnlyFailingChecks(true)}
                          className={cn(
                            "cursor-pointer transition-colors",
                            onlyFailingChecks ? "font-medium text-fg" : "text-muted hover:text-fg",
                          )}
                        >
                          Problems <span className="num text-muted">{count("breach")}</span>
                        </button>
                      </div>
                    )}
                  </div>
                  <div className="grid gap-x-8 gap-y-6 md:grid-cols-2">
                    {AREAS.map((area) => {
                      const allInArea = checks
                        .filter((c) => areaOf(c.name) === area)
                        .sort((a, b) => Number(b.state === "breach") - Number(a.state === "breach"));
                      const rows = onlyFailingChecks ? allInArea.filter((c) => c.state === "breach") : allInArea;
                      if (rows.length === 0) return null;
                      const bad = allInArea.filter((c) => c.state === "breach").length;
                      return (
                        <div key={area}>
                          <h3 className="flex items-baseline justify-between border-b border-border pb-2 text-[12.5px] font-semibold text-fg">
                            <span>{area}</span>
                            <span className={cn("tabular-nums text-[12px] font-medium", bad ? "text-late" : "text-ok")}>
                              {bad ? `${bad} of ${allInArea.length} failing` : `${allInArea.length} OK`}
                            </span>
                          </h3>
                          <ul className="divide-y divide-border/70 text-[13px]">
                            {rows.map((c) => (
                              <li key={c.name} className="flex items-start gap-3 py-2 transition-colors duration-150 hover:bg-row-on/60">
                                <CheckStateTag state={c.state} />
                                <span className="min-w-0 flex-1 text-fg">{c.line ? opsLine(c.name, c.line) : opsLabel(c.name)}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      );
                    })}
                  </div>
                </section>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
