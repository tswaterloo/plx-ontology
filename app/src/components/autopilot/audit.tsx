"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ShowMore } from "@/components/ui/show-more";
import { ErrorNotice, LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StatusWord } from "@/components/ui/status-badge";
import { getAutopilotAudit, undoAutopilotAction } from "@/lib/api";
import { auditWhat, filterWorkflows, personName, roleLabel, rowRole, systemLabel, workflowLabel, type Filters } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDateTime, plural } from "@/lib/format";
import type { AutopilotArea as Area, AutopilotAudit } from "@/lib/types";

const chip = (on: boolean) =>
  cn(
    "inline-flex h-7 shrink-0 cursor-pointer items-center gap-1.5 rounded-[3px] px-3 text-[12px]",
    on ? "bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover",
  );

const UNDO_STATUS: Record<string, string> = {
  pending: "Undo sent: waiting for the system to confirm it.",
  failed: "Undo failed: the change is still in place.",
  unknown: "Undo outcome not known: check the system before trying again.",
};


export function AuditView({ area, filters }: { area: Area; filters: Filters }) {
  const roleName = (role?: string | null) => {
    if (!role) return null;
    const row = (area.workflows ?? []).find((w) => w.role === role);
    return row ? rowRole(area, row) : role.startsWith("person:") ? "One person" : roleLabel(role);
  };
  const [audit, setAudit] = useState<AutopilotAudit | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [liveOnly, setLiveOnly] = useState(true);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [undoError, setUndoError] = useState<string | null>(null);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getAutopilotAudit(ctl.signal).then(setAudit, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!audit) return <Pending label="Reading the audit log" />;
  const roles = new Set(filterWorkflows(area, filters).map((w) => w.role));
  const scoped = !!(filters.person || filters.role || filters.workflow);
  const rows = (audit.rows ?? [])
    .filter((r) => !liveOnly || r.mode === "live" || r.event === "switch" || r.event === "reconciled")
    .filter((r) => !scoped || (r.role ? roles.has(r.role) : !filters.person && !filters.role) && (!filters.workflow || r.workflow === filters.workflow));
  const undo = async (key: string) => {
    setUndoError(null);
    try {
      setAudit(await undoAutopilotAction(key));
      setConfirm(null);
    } catch (e) {
      setUndoError(plainError(e));
    }
  };
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="flex-1 text-[12px] text-muted">{plural(audit.live, "live action")} done</p>
        <button type="button" className={chip(liveOnly)} aria-pressed={liveOnly} onClick={() => setLiveOnly((v) => !v)}>
          Live actions and switches only
        </button>
      </div>
      {undoError && <ErrorNotice message={undoError} />}
      {rows.length === 0 ? (
        <EmptyState title={liveOnly ? "No live actions yet." : "Nothing on the audit log yet."} line={liveOnly ? "Every accept so far was a dry run. Show them with the button above." : undefined} />
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border">
          {rows.map((r, n) => (
            <li key={`${r.at}/${r.key ?? r.event}/${n}`} className="space-y-1 p-3 text-[13px]" data-testid="autopilot-audit-row">
              <p className="break-words">
                <span className="font-medium">{auditWhat(r)}</span>
                {r.title ? ` · ${r.title}` : r.why ? ` · ${r.why}` : ""}
              </p>
              <p className="break-words text-[12px] text-muted">
                {[formatDateTime(r.at), r.event.startsWith("opt_in") && r.by && r.by !== "autopilot" ? personName(area, r.by) : null, roleName(r.role), r.system ? systemLabel(r.system) : null, r.workflow ? workflowLabel(r.workflow) : null, r.operation?.replace(/_/g, " "), r.reasons?.length ? r.reasons.join(", ").replace(/_/g, " ") : null]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              {r.undone && <p className="text-[12px] text-muted">Undone.</p>}
              {r.undo_status && <p className="text-[12px] text-muted">{UNDO_STATUS[r.undo_status] ?? "Undo outcome not known: check the system before trying again."}</p>}
              {r.undoable && r.key &&
                (confirm === r.key ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[12px]">Undo performs the recorded reverse in {systemLabel(r.system ?? "other")}.</span>
                    <Button size="sm" variant="outline" onClick={() => void undo(r.key as string)}>
                      Confirm undo
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>
                      Cancel
                    </Button>
                  </div>
                ) : (
                  <Button size="sm" variant="outline" onClick={() => setConfirm(r.key as string)}>
                    Undo
                  </Button>
                ))}
              {!r.undoable && !r.undone && r.status === "done" && r.mode === "live" && r.undo_note && <p className="text-[12px] text-muted">{r.undo_note}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
