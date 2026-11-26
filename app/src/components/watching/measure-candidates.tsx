"use client";

import { useEffect, useState } from "react";
import { QueryToggle } from "@/components/query-work";
import { approveMeasureCandidate, dismissMeasureCandidate, getMeasureCandidates } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { MeasureCandidate, MeasureCandidateList, MeasureCandidateStatus } from "@/lib/types";

const btn = "h-7 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg hover:border-border-strong hover:bg-hover cursor-pointer disabled:cursor-default disabled:opacity-50";
const primaryBtn = "h-7 rounded-md bg-fg px-2.5 text-[12px] font-medium text-bg hover:opacity-90 cursor-pointer disabled:cursor-default disabled:opacity-50";

/** Plain words for where a candidate definition came from. */
export function sourceWords(kind: MeasureCandidate["source_kind"]): string {
  if (kind === "accepted_answer") return "From accepted answer";
  if (kind === "saved_tile") return "From pinned chart";
  return "Proposed";
}

/** Plain words for how many times a candidate definition was used. */
export function usageWords(count: number): string {
  return count === 1 ? "Used once" : `Used ${count} times`;
}

type Filter = "all" | MeasureCandidateStatus;

/**
 * Owner review queue for candidate shared measure definitions captured from accepted answers,
 * pinned charts, and direct proposals. Non-owner admins see the list read only; only the owner
 * sees Approve and Dismiss controls.
 */
export function MeasureCandidates() {
  const [list, setList] = useState<MeasureCandidateList | null>(null);
  const [filter, setFilter] = useState<Filter>("pending");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ name: string; description: string; owner: string }>({ name: "", description: "", owner: "" });
  const [busyId, setBusyId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    getMeasureCandidates(undefined, ctl.signal)
      .then((got) => {
        if (!ctl.signal.aborted) setList(got);
      })
      .catch((err) => {
        if (!ctl.signal.aborted) setProblem(plainError(err));
      });
    return () => ctl.abort();
  }, []);

  const updateOne = (next: MeasureCandidate) => {
    setList((prev) => {
      if (!prev) return prev;
      const candidates = (prev.candidates ?? []).map((c) => (c.id === next.id ? next : c));
      const counts = { pending: 0, approved: 0, dismissed: 0 };
      for (const c of candidates) counts[c.status] = (counts[c.status] ?? 0) + 1;
      return { ...prev, candidates, counts, total: candidates.length };
    });
  };

  const startEdit = (c: MeasureCandidate) => {
    setEditing(c.id);
    setDraft({ name: c.proposed_name, description: c.proposed_description ?? "", owner: c.proposed_owner });
    setProblem(null);
  };

  const approve = async (c: MeasureCandidate, withDraft = false) => {
    setBusyId(c.id);
    setProblem(null);
    try {
      const body = withDraft
        ? { name: draft.name.trim() || undefined, description: draft.description.trim() || undefined, owner: draft.owner.trim() || undefined }
        : undefined;
      const got = await approveMeasureCandidate(c.id, body);
      updateOne(got);
      if (editing === c.id) setEditing(null);
    } catch (err) {
      setProblem(plainError(err));
    } finally {
      setBusyId(null);
    }
  };

  const dismiss = async (c: MeasureCandidate) => {
    setBusyId(c.id);
    setProblem(null);
    try {
      const got = await dismissMeasureCandidate(c.id);
      updateOne(got);
      if (editing === c.id) setEditing(null);
    } catch (err) {
      setProblem(plainError(err));
    } finally {
      setBusyId(null);
    }
  };

  const pending = list?.counts?.pending ?? 0;
  const approved = list?.counts?.approved ?? 0;
  const dismissed = list?.counts?.dismissed ?? 0;
  const allCandidates = list?.candidates ?? [];
  const shown = allCandidates.filter((c) => filter === "all" || c.status === filter);

  const filterOptions: { id: Filter; label: string }[] = [
    { id: "pending", label: `Waiting (${pending})` },
    { id: "approved", label: `Approved (${approved})` },
    { id: "dismissed", label: `Dismissed (${dismissed})` },
    { id: "all", label: `All (${allCandidates.length})` },
  ];

  return (
    <div className="space-y-3" data-testid="measure-candidates">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h4 className="text-[14px] font-semibold text-fg">Proposed shared definitions</h4>
          <p className="text-[12px] text-muted">
            Definitions captured from accepted answers and pinned charts awaiting owner sign-off.
          </p>
        </div>
        {list && (
          <div
            className="inline-flex flex-wrap items-center gap-1 rounded-lg border border-border bg-bg p-0.5"
            role="group"
            aria-label="Filter proposed definitions"
          >
            {filterOptions.map((opt) => (
              <button
                key={opt.id}
                type="button"
                aria-pressed={filter === opt.id}
                onClick={() => setFilter(opt.id)}
                className={cn(
                  "h-6 rounded-md px-2 text-[11.5px] font-medium transition-colors cursor-pointer",
                  filter === opt.id
                    ? "bg-surface-2 text-fg"
                    : "text-muted hover:text-fg",
                )}
              >
                {opt.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {problem && <p className="text-[13px] text-late" role="alert">{problem}</p>}
      {list === null && !problem && <p className="text-[13px] text-muted">Reading proposed definitions</p>}
      {list && !list.can_review && pending > 0 && (
        <p className="text-[12px] text-muted" data-testid="candidates-owner-only">
          Only the owner can approve or dismiss shared definitions.
        </p>
      )}
      {list && shown.length === 0 && (
        <p className="py-4 text-[13px] text-muted" data-testid="candidates-empty">
          {filter === "pending" ? "No proposed definitions are waiting for review." : "No definitions match this filter."}
        </p>
      )}

      {list && shown.length > 0 && (
        <ul className="divide-y divide-border border-y border-border" data-testid="candidates-list">
          {shown.map((c) => {
            const busy = busyId === c.id;
            const isEditing = editing === c.id;
            return (
              <li key={c.id} className="space-y-2 py-3 text-[13px]" data-testid={`candidate-${c.id}`}>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <p className="font-medium text-fg">
                    {c.proposed_name}
                    <span className="ml-2 text-[12px] font-normal text-muted">{usageWords(c.usage_count)}</span>
                  </p>
                  <span className="text-[12px] text-muted">
                    {sourceWords(c.source_kind)} · Owner: {c.proposed_owner}
                  </span>
                </div>
                {c.proposed_description && <p className="text-muted">{c.proposed_description}</p>}
                {c.question && c.question !== c.proposed_name && (
                  <p className="text-[12px] text-muted">Question: {c.question}</p>
                )}
                <p className="text-[12px] text-faint">
                  {(c.views ?? []).length > 0
                    ? `Views: ${(c.views ?? []).join(", ")}`
                    : (c.withheld ?? []).length > 0
                      ? "Definition details withheld"
                      : "Custom statement"}
                  {(c.proposed_dimensions ?? []).length > 0 ? ` · Split by ${(c.proposed_dimensions ?? []).join(", ")}` : ""}
                  {c.proposer_name ? ` · Proposed by ${c.proposer_name}` : ""}
                  {` · Last used ${formatDateTime(c.last_seen_at)}`}
                </p>
                {(c.withheld ?? []).length > 0 && (
                  <p className="text-[12px] text-muted">Parts of this statement are withheld for your access.</p>
                )}
                {c.sql && <QueryToggle sql={c.sql} testId={`candidate-sql-${c.id}`} />}
                {c.status === "approved" && (
                  <p className="text-[12px] text-ok">
                    Approved as {c.approved_measure_id}{c.reviewed_by ? ` by ${c.reviewed_by}` : ""}{c.reviewed_at ? ` on ${formatDateTime(c.reviewed_at)}` : ""}.
                  </p>
                )}
                {c.status === "dismissed" && (
                  <p className="text-[12px] text-muted">
                    Dismissed{c.reviewed_by ? ` by ${c.reviewed_by}` : ""}{c.reviewed_at ? ` on ${formatDateTime(c.reviewed_at)}` : ""}.
                    {c.dismiss_reason ? ` Reason: ${c.dismiss_reason}` : ""}
                  </p>
                )}
                {list.can_review && c.status === "pending" && !isEditing && (
                  <div className="flex flex-wrap items-center gap-2 pt-0.5">
                    <button type="button" className={primaryBtn} disabled={busy} onClick={() => approve(c, false)}>
                      {busy ? "Saving" : "Approve"}
                    </button>
                    <button type="button" className={btn} disabled={busy} onClick={() => startEdit(c)}>
                      Edit details
                    </button>
                    <button type="button" className={btn} disabled={busy} onClick={() => dismiss(c)}>
                      Dismiss
                    </button>
                  </div>
                )}
                {list.can_review && c.status === "pending" && isEditing && (
                  <form
                    className="space-y-2 rounded-lg border border-border bg-bg p-3"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void approve(c, true);
                    }}
                  >
                    <label className="block text-[12px] text-muted">
                      Name
                      <input
                        className="mt-1 h-8 w-full rounded-md border border-border bg-surface px-2 text-[13px] text-fg"
                        value={draft.name}
                        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                        required
                      />
                    </label>
                    <label className="block text-[12px] text-muted">
                      Description
                      <textarea
                        className="mt-1 w-full rounded-md border border-border bg-surface p-2 text-[13px] text-fg"
                        rows={2}
                        value={draft.description}
                        onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                        required
                      />
                    </label>
                    <label className="block text-[12px] text-muted">
                      Owner
                      <input
                        className="mt-1 h-8 w-full rounded-md border border-border bg-surface px-2 text-[13px] text-fg"
                        value={draft.owner}
                        onChange={(e) => setDraft({ ...draft, owner: e.target.value })}
                        required
                      />
                    </label>
                    <div className="flex items-center gap-2">
                      <button type="submit" className={primaryBtn} disabled={busy}>
                        {busy ? "Saving" : "Save and approve"}
                      </button>
                      <button type="button" className={btn} disabled={busy} onClick={() => setEditing(null)}>
                        Cancel
                      </button>
                    </div>
                  </form>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
