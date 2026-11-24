"use client";

import { useState } from "react";
import { eastern, opsLabel, opsLine, SectionTabs } from "@/components/shell";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { LoadProblem } from "@/components/ui/notice";
import { getOpsAlarms, type OpsAlarms, type OpsCheck, type OpsIncident, type OpsQueue } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { cn } from "@/lib/cn";
import { problemParts } from "@/lib/format";
import { noticeKind, plainError } from "@/lib/errors";

type Area = "Data freshness" | "Services" | "Postgres" | "Machine" | "Backups" | "Other";
const AREAS: Area[] = ["Data freshness", "Services", "Postgres", "Machine", "Backups", "Other"];

/** Which part of the system a check or incident key belongs to. */
function areaOf(key: string): Area {
  if (key.startsWith("unit:") || key.startsWith("units:") || key === "ingest_start" || key.startsWith("jobq_")) return "Services";
  if (key.startsWith("capture:") || key.startsWith("delta_") || key === "pg_follow" || key.endsWith("_last_pass") || key === "search_follow_last_pass") return "Data freshness";
  if (key.startsWith("pg_")) return "Postgres";
  if (key.startsWith("disk_") || key.startsWith("memory_")) return "Machine";
  if (key === "backup" || key === "restore_test") return "Backups";
  return "Other";
}

/** What the operator does first for an open problem in each area. */
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

/** The host job queue: what runs, what waits and why, refusals and dead letters. */
function QueueSection({ queue }: { queue?: OpsQueue | null }) {
  if (!queue || !queue.configured) return null;
  const running = queue.running ?? [];
  const waiting = queue.waiting ?? [];
  const dead = queue.dead ?? [];
  const mem = queue.memory;
  const hour = queue.last_hour ?? {};
  return (
    <section data-testid="ops-queue">
      <h2 className="text-[13px] font-semibold">Job queue</h2>
      {!queue.fresh ? (
        <p className="mt-1 text-[13px] font-semibold text-late" data-testid="ops-queue-stale">
          The queue worker has stopped{queue.written_at ? `: last report ${eastern(queue.written_at)}` : ""}. Queued jobs do not start until it runs again.
        </p>
      ) : null}
      <p className="mt-1 text-[12px] text-muted">
        {queue.queued ?? 0} queued · {running.length} running · {hour.refused ?? 0} refusals and {hour.dead ?? 0} dead in the last hour
        {mem ? ` · ${gb(mem.available_mb)} free, ${gb(mem.promised_mb)} promised to running jobs, ${gb(mem.headroom_mb)} headroom above a ${gb(mem.floor_mb)} floor` : ""}
      </p>
      <table className="mt-1 w-full text-left text-[13px]">
        <tbody className="divide-y divide-border">
          {running.map((j) => (
            <tr key={`r${j.id}`}>
              <td className="py-1 pr-3">{j.class}</td>
              <td className="py-1 pr-3 text-muted">{j.state === "preempting" ? "stopping (preempted)" : "running"} {secs(j.running_s)}</td>
              <td className="py-1 pr-3 tabular-nums text-muted">{gb(j.current_mb)} of {gb(j.mem_mb)}</td>
              <td className="py-1 text-[12px] text-muted">waited {secs(j.waited_s) || "0 s"}</td>
            </tr>
          ))}
          {(queue.unmanaged ?? []).map((u) => (
            <tr key={`u${u.unit}`}>
              <td className="py-1 pr-3">{u.class}</td>
              <td className="py-1 pr-3 text-muted">running outside the queue</td>
              <td className="py-1 pr-3 tabular-nums text-muted">{gb(u.current_mb)} of {gb(u.mem_mb)}</td>
              <td />
            </tr>
          ))}
          {waiting.map((j) => (
            <tr key={`w${j.id}`} className="align-top">
              <td className="py-1 pr-3">{j.class}</td>
              <td className="py-1 pr-3 text-muted">waiting {secs(j.waiting_s)}{j.not_before_s ? `, retry in ${secs(j.not_before_s)}` : ""}</td>
              <td className="py-1 pr-3 tabular-nums text-muted">{j.refusals} refused</td>
              <td className="py-1 text-[12px] text-muted">{j.last_refusal ?? ""}</td>
            </tr>
          ))}
          {dead.map((j) => (
            <tr key={`d${j.id}`} className="align-top">
              <td className="py-1 pr-3 text-late">{j.class}</td>
              <td className="py-1 pr-3 text-late">gave up after {j.attempts} tries</td>
              <td className="py-1 pr-3 text-[12px] text-muted" colSpan={2}>{j.last_error ?? j.result ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {(queue.events ?? []).length > 0 ? (
        <p className="mt-1 text-[12px] text-muted" data-testid="ops-queue-events">
          Events: {(queue.events ?? []).map((e) => `${e.topic} ${e.n} ${e.state === "ready" ? "waiting" : e.state === "inflight" ? "being handled" : "dead"}${e.state === "ready" && e.oldest_s ? ` (oldest ${secs(e.oldest_s)})` : ""}`).join(" · ")}
        </p>
      ) : null}
      {running.length + waiting.length + dead.length === 0 ? <p className="mt-1 text-[13px] text-muted">Nothing queued or running.</p> : null}
    </section>
  );
}

const STATE_WORD: Record<OpsCheck["state"], string> = { breach: "Problem", ok: "OK", unknown: "Unknown" };

// The reliability page (operating/ops_alarms.py, every 5 minutes): what is broken now and
// what to do about it, what is paused on purpose, what recovered, and every check of the
// last pass by area. Alarms show here and in the red line on every page, and are mailed
// when ops mail is on (operating/ops_notify.py). The owner and the Team readers read it;
// anyone else gets the route's 403.
export default function OpsPage() {
  const { data: alarms, error, retry } = useRead<OpsAlarms>("ops", (signal) => getOpsAlarms(signal));
  const checks = alarms?.checks ?? [];
  const count = (s: OpsCheck["state"]) => checks.filter((c) => c.state === s).length;
  // the plain words come first; the checks, units and history are IT's and open on request
  const [detail, setDetail] = useState(false);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Reliability" />
      <SectionTabs section="admin" />
      <PageIntro path="/ops" />
      <div className="w-full max-w-5xl space-y-5 overflow-y-auto px-4 py-4 sm:px-6" data-testid="ops-page">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : !alarms ? (
          <Pending page label="Reading the reliability checks" />
        ) : !alarms.configured ? (
          <p className="text-[13px] text-muted">This server has no live state to check.</p>
        ) : (
          <>
            {alarms.maintenance?.state === "in_window" && (
              <p className="text-[13px]" data-testid="ops-maintenance">
                Paused for maintenance until {eastern(alarms.maintenance.until)}: {alarms.maintenance.reason}.
                <span className="ml-1 text-muted">What it stops is listed under Paused on purpose; past that time it opens again and pages.</span>
              </p>
            )}
            <section className="border-b border-border pb-3" data-testid="ops-plain">
              <h2 className="text-[15px] font-semibold">
                {alarms.open.length === 0 ? "Nothing needs attention." : `${alarms.open.length} ${alarms.open.length === 1 ? "thing needs" : "things need"} attention.`}
              </h2>
              <p className="mt-1 text-[13px] text-muted">
                {alarms.running
                  ? `Checked ${alarms.checked_at ? eastern(alarms.checked_at) : "just now"}, and again every ${alarms.every_minutes ?? 5} minutes.`
                  : "The checks have stopped, so a new problem would not show here."}
              </p>
              {alarms.open.length > 0 && (
                <ul className="mt-2 space-y-1 text-[13px]" data-testid="ops-plain-list">
                  {alarms.open.slice(0, 6).map((i: OpsIncident) => (
                    <li key={i.key}>
                      <span className="text-muted">{areaOf(i.key)}: </span>
                      {problemParts(opsLine(i.key, i.line)).head}
                      <span className="text-muted"> (open {span(i.opened_at)})</span>
                    </li>
                  ))}
                  {alarms.open.length > 6 && <li className="text-muted">The other {alarms.open.length - 6} are listed in the technical detail.</li>}
                </ul>
              )}
              <button type="button" onClick={() => setDetail((v) => !v)} aria-expanded={detail} data-testid="ops-detail-toggle" className="mt-3 text-[12px] font-medium text-fg underline-offset-2 hover:underline cursor-pointer">
                {detail ? "Hide the technical detail" : "Show the technical detail"}
              </button>
            </section>
            {detail && (
              <>
            <div className="border-b border-border pb-3">
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-4" data-testid="ops-summary">
                <div>
                  <dt className="text-[12px] text-muted">Open problems</dt>
                  <dd className={cn("text-[20px] font-semibold tabular-nums", alarms.open.length ? "text-late" : "text-fg")}>{alarms.open.length}</dd>
                </div>
                <div>
                  <dt className="text-[12px] text-muted">Checks</dt>
                  <dd className="tabular-nums">
                    <span className="text-[20px] font-semibold">{checks.length}</span>
                    <span className="ml-2 text-[12px] text-muted">
                      {count("ok")} OK · <span className={count("breach") ? "text-late" : ""}>{count("breach")} problem</span> · {count("unknown")} unknown
                    </span>
                  </dd>
                </div>
                <div>
                  <dt className="text-[12px] text-muted">Last pass</dt>
                  <dd className={cn(alarms.running ? "" : "font-semibold text-late")} data-testid="ops-checked">
                    {alarms.checked_at ? eastern(alarms.checked_at) : "never"}
                    <span className="block text-[12px] font-normal text-muted">
                      {alarms.running ? `every ${alarms.every_minutes ?? 5} minutes` : "the checks have stopped: a new problem would not show"}
                    </span>
                  </dd>
                </div>
                <div>
                  <dt className="text-[12px] text-muted">Recovered lately</dt>
                  <dd className="text-[20px] font-semibold tabular-nums">{alarms.recovered.length}</dd>
                </div>
              </dl>
              {alarms.open.length > 0 && (
                <p className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[12px] text-muted" data-testid="ops-by-area">
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
              <p className="mt-2 text-[12px] text-muted">
                Shown to the owner and the Team readers, here and in the red line at the top of every page. Nothing is paged or mailed.
              </p>
            </div>

            <section>
              <h2 className="text-[13px] font-semibold">Open problems</h2>
              {alarms.open.length === 0 ? (
                <p className="mt-1 text-[13px] text-muted">None. Every check that can say so passed on the last pass.</p>
              ) : (
                <table className="mt-1 w-full text-left text-[13px]" data-testid="ops-open">
                  <thead className="text-[12px] text-muted">
                    <tr className="border-b border-border">
                      <th className="py-1 pr-3 font-normal">Problem</th>
                      <th className="py-1 pr-3 font-normal max-sm:hidden">Area</th>
                      <th className="py-1 pr-3 font-normal">Open for</th>
                      <th className="py-1 font-normal max-md:hidden">Next step</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {alarms.open.map((i: OpsIncident) => (
                      <tr key={i.key} className="align-top">
                        <td className="py-1.5 pr-3 text-late" title={i.key}>
                          {(() => {
                            const { head, more } = problemParts(opsLine(i.key, i.line));
                            return (
                              <>
                                {head}
                                {more && (
                                  <details className="mt-0.5 text-[12px] text-muted">
                                    <summary className="cursor-pointer">Details</summary>
                                    <p className="mt-0.5">{more}</p>
                                  </details>
                                )}
                              </>
                            );
                          })()}
                        </td>
                        <td className="py-1.5 pr-3 text-muted max-sm:hidden">{areaOf(i.key)}</td>
                        <td className="py-1.5 pr-3 whitespace-nowrap tabular-nums" title={`since ${eastern(i.opened_at)}`}>
                          {span(i.opened_at)}
                          <span className="block text-[12px] text-muted">since {eastern(i.opened_at)}</span>
                        </td>
                        <td className="py-1.5 text-[12px] text-muted max-md:hidden">{NEXT[areaOf(i.key)]}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            {(alarms.paused?.length ?? 0) > 0 && (
              <section>
                <h2 className="text-[13px] font-semibold">Paused on purpose</h2>
                <p className="mt-0.5 text-[12px] text-muted">Not mailed until the pause ends; then open again if still broken.</p>
                <table className="mt-1 w-full text-left text-[13px]" data-testid="ops-paused">
                  <tbody className="divide-y divide-border">
                    {alarms.paused!.map((i) => (
                      <tr key={i.key} className="align-top">
                        <td className="py-1.5 pr-3" title={i.key}>{opsLine(i.key, i.line)}</td>
                        <td className="py-1.5 pr-3 whitespace-nowrap text-[12px] text-muted">until {eastern(i.paused?.until)}</td>
                        <td className="py-1.5 text-[12px] text-muted max-sm:hidden">{i.paused?.why || "no reason given"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            )}

            {alarms.notify && (
              <p className="text-[12px] text-muted" data-testid="ops-mail">
                {alarms.notify.sending
                  ? `Problems are mailed to ${alarms.notify.recipients ?? 0} ${alarms.notify.recipients === 1 ? "person" : "people"}; a daily mail at 8 AM ET says the checks run.`
                  : `Problems are not mailed (${alarms.notify.why_not || "off"}): they show only here.`}
                {(alarms.notify.pending ?? 0) > 0 && ` ${alarms.notify.pending} mail(s) not sent yet, retried every pass.`}
              </p>
            )}

            {alarms.recovered.length > 0 && (
              <section>
                <h2 className="text-[13px] font-semibold">Recovered</h2>
                <table className="mt-1 w-full text-left text-[13px]" data-testid="ops-recovered">
                  <tbody className="divide-y divide-border">
                    {alarms.recovered.map((i) => (
                      <tr key={`${i.key}-${i.opened_at}`} className="align-top">
                        <td className="py-1.5 pr-3" title={i.key}>{opsLine(i.key, i.line)}</td>
                        <td className="py-1.5 pr-3 whitespace-nowrap text-[12px] text-muted">
                          {eastern(i.opened_at)} to {eastern(i.closed_at)}
                        </td>
                        <td className="py-1.5 whitespace-nowrap text-[12px] tabular-nums text-muted">{span(i.opened_at, i.closed_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            )}

            <QueueSection queue={alarms.queue} />

            <section data-testid="ops-checks">
              <h2 className="text-[13px] font-semibold">Every check, last pass</h2>
              <div className="mt-1 grid gap-x-8 gap-y-4 md:grid-cols-2">
                {AREAS.map((area) => {
                  const rows = checks
                    .filter((c) => areaOf(c.name) === area)
                    .sort((a, b) => Number(b.state === "breach") - Number(a.state === "breach"));
                  if (rows.length === 0) return null;
                  const bad = rows.filter((c) => c.state === "breach").length;
                  return (
                    <div key={area}>
                      <h3 className="flex items-baseline justify-between border-b border-border pb-1 text-[12px] font-medium text-muted">
                        <span>{area}</span>
                        <span className={cn("tabular-nums", bad ? "text-late" : "")}>{bad ? `${bad} of ${rows.length} failing` : `${rows.length} OK`}</span>
                      </h3>
                      <ul className="divide-y divide-border text-[13px]">
                        {rows.map((c) => (
                          <li key={c.name} className="flex gap-3 py-1">
                            <span className={cn("w-16 shrink-0 text-[12px]", c.state === "breach" ? "font-semibold text-late" : c.state === "ok" ? "text-muted" : "text-faint")}>
                              {STATE_WORD[c.state] ?? "Unknown"}
                            </span>
                            <span className="min-w-0 flex-1">{c.line ?? opsLabel(c.name)}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  );
                })}
              </div>
            </section>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
