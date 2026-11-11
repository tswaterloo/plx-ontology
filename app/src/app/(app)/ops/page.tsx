"use client";

import { useCallback, useEffect, useState } from "react";
import { eastern, opsLabel } from "@/components/shell";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { getOpsAlarms, type OpsAlarms, type OpsIncident } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";

const PAGE_WORDS: Record<string, string> = {
  sent: "paged",
  unknown: "page may have gone out",
  failed: "page failed",
  dry_run: "not paged (no page address set)",
  not_sent: "no recovery notice (opening page not sent)",
};

function pageWords(p?: OpsIncident["page"]): string {
  if (!p) return "";
  if (p.status === "dry_run" && !(p.missing ?? []).some((m) => m.startsWith("PLX_OPS_PAGE_TO"))) return "not paged (sending is off)";
  return PAGE_WORDS[p.status] ?? p.status;
}

// The owner's reliability page (operating/ops_alarms.py, every 5 minutes): what is broken now,
// what recovered, and every check of the last pass. Other readers get the route's 403.
export default function OpsPage() {
  const [alarms, setAlarms] = useState<OpsAlarms | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getOpsAlarms(ctl.signal).then(setAlarms, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  const checks = [...(alarms?.checks ?? [])].sort((a, b) => Number(b.state === "breach") - Number(a.state === "breach"));
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Reliability" />
      <div className="mx-auto w-full max-w-4xl space-y-4 overflow-y-auto px-4 py-4 sm:px-6" data-testid="ops-page">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : !alarms ? (
          <Pending page label="Reading the reliability checks" />
        ) : !alarms.configured ? (
          <p className="text-[13px] text-muted">This server has no live state to check.</p>
        ) : (
          <>
            <p className={cn("text-[13px]", alarms.running ? "text-muted" : "font-semibold text-late")} data-testid="ops-checked">
              {alarms.running
                ? `Checked ${eastern(alarms.checked_at)}, every ${alarms.every_minutes ?? 5} minutes.`
                : alarms.checked_at
                  ? `The checks have not run since ${eastern(alarms.checked_at)}: nothing would page you.`
                  : "The checks have not run yet: nothing would page you."}
              {alarms.paging && !alarms.paging.configured ? " Pages are recorded, not sent, until a page address is set." : ""}
            </p>
            <section>
              <h2 className="text-[13px] font-semibold">Open problems</h2>
              {alarms.open.length === 0 ? (
                <p className="mt-1 text-[13px] text-muted">None.</p>
              ) : (
                <ul className="mt-1 divide-y divide-border text-[13px]" data-testid="ops-open">
                  {alarms.open.map((i) => (
                    <li key={i.key} className="flex gap-3 py-1.5">
                      <span className="min-w-0 flex-1 text-late">{i.line}</span>
                      <span className="shrink-0 text-[12px] text-muted">
                        since {eastern(i.opened_at)}
                        {i.page ? ` · ${pageWords(i.page)}` : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            {alarms.recovered.length > 0 && (
              <section>
                <h2 className="text-[13px] font-semibold">Recovered</h2>
                <ul className="mt-1 space-y-1 text-[13px]" data-testid="ops-recovered">
                  {alarms.recovered.map((i) => (
                    <li key={`${i.key}-${i.opened_at}`}>
                      {i.line}
                      <span className="text-[12px] text-muted"> · {eastern(i.opened_at)} to {eastern(i.closed_at)}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <section>
              <h2 className="text-[13px] font-semibold">Every check</h2>
              <ul className="mt-1 divide-y divide-border text-[13px]" data-testid="ops-checks">
                {checks.map((c) => (
                  <li key={c.name} className="flex gap-3 py-1.5">
                    <span className={cn("w-16 shrink-0 text-[12px]", c.state === "breach" ? "font-semibold text-late" : "text-muted")}>
                      {c.state === "breach" ? "Problem" : c.state === "ok" ? "OK" : "Unknown"}
                    </span>
                    <span className="min-w-0 flex-1">{c.line ?? opsLabel(c.name)}</span>
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
