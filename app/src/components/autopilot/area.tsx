"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AssistBox } from "@/components/autopilot/assist";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorNotice, LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StatusBadge, StatusWord } from "@/components/ui/status-badge";
import { decideAutopilotItem, getAutopilotAudit, setAutopilotGlobal, setAutopilotWorkflow, undoAutopilotAction } from "@/lib/api";
import {
  ITEM_STATE,
  NO_FILTERS,
  type Filters,
  type Split,
  filterItems,
  filterWorkflows,
  itemLive,
  pct,
  personName,
  roleLabel,
  rowRole,
  splitItems,
  stateShown,
  systemLabel,
  workflowLabel,
} from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDateTime, objectHref, plural, sourceHref } from "@/lib/format";
import type { AutopilotArea as Area, AutopilotAudit, AutopilotAuditRow, AutopilotItem, AutopilotWorkflowRow } from "@/lib/types";

// The autopilot area: today's prepared items with their evidence and drafted action (accept,
// edit, reject, snooze), the workflows with their scores, state and the owner's switches, and
// the audit log of the write path with undo. An accept is a recorded dry run until its
// workflow passed the fresh gate and the owner switched it live; the server decides both.

type Tab = "today" | "workflows" | "audit";

const chip = (on: boolean) =>
  cn(
    "inline-flex h-7 shrink-0 cursor-pointer items-center gap-1.5 rounded-full px-3 text-[12px]",
    on ? "bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover",
  );

const selectCls = "h-8 min-w-0 rounded-lg border border-border bg-bg px-2 text-[13px] text-fg";

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <label className="flex min-w-0 flex-col gap-0.5 text-[11px] text-muted">
      {label}
      <select className={selectCls} value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        {options.map(([v, l]) => (
          <option key={v} value={v}>
            {l}
          </option>
        ))}
      </select>
    </label>
  );
}

const uniq = (xs: string[]) => [...new Set(xs.filter(Boolean))].sort();

/** Snooze choices: in an hour, tomorrow 8 AM Toronto, next Monday 8 AM. */
export function snoozeChoices(now: Date = new Date()): [string, string][] {
  const at8 = (days: number) => {
    const d = new Date(now.getTime() + days * 86_400_000);
    const day = d.toLocaleDateString("en-CA", { timeZone: "America/Toronto" });
    // 8 AM Toronto is 12:00 or 13:00 UTC; take the one that reads 8 in Toronto
    for (const h of [12, 13]) {
      const t = new Date(`${day}T${String(h).padStart(2, "0")}:00:00Z`);
      if (t.toLocaleTimeString("en-CA", { hour: "numeric", hour12: false, timeZone: "America/Toronto" }).startsWith("8")) return t;
    }
    return new Date(`${day}T13:00:00Z`);
  };
  const dow = new Date(now.toLocaleString("en-US", { timeZone: "America/Toronto" })).getDay();
  const toMonday = ((8 - dow) % 7) || 7;
  const iso = (d: Date) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
  return [
    [iso(new Date(now.getTime() + 3_600_000)), "In an hour"],
    [iso(at8(1)), "Tomorrow morning"],
    [iso(at8(toMonday)), "Next Monday"],
  ];
}

function happenedLine(h: Record<string, unknown>): string {
  const parts = [h.sender, h.subject].filter((x) => typeof x === "string" && x) as string[];
  return parts.join(" · ");
}

// One prepared item: what happened, the evidence, the drafted action and your decision.
function Item({
  area,
  item,
  itemKey,
  onDecided,
  register,
}: {
  area: Area;
  item: AutopilotItem;
  itemKey: string;
  onDecided: (i: AutopilotItem) => void;
  register: (el: HTMLLIElement | null, api: { act: (k: string) => void } | null) => void;
}) {
  const prepared = (item.prepared ?? {}) as Record<string, unknown>;
  const happened = (item.happened ?? {}) as Record<string, unknown>;
  const draft = prepared.draft as { subject?: string; body?: string; to?: { address?: string }[]; channel?: string } | undefined;
  const write = prepared.write as { system?: string; operation?: string; reason?: string } | undefined;
  const gaps = (prepared.gaps as string[] | undefined) ?? [];
  const objectId = typeof happened.object_id === "string" ? happened.object_id : null;
  const live = itemLive(area, item);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(item.decision?.text ?? draft?.body ?? "");
  const [confirmLive, setConfirmLive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snoozeOpen, setSnoozeOpen] = useState(false);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const decide = useCallback(
    async (decision: "accept" | "edit" | "reject" | "snooze", extra: { text?: string; until?: string } = {}) => {
      if (decision === "accept" && live && !confirmLive) {
        setConfirmLive(true);
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const out = await decideAutopilotItem(item.item_id, { decision, ...extra });
        setEditing(false);
        setConfirmLive(false);
        setSnoozeOpen(false);
        onDecided(out);
      } catch (e) {
        setError(plainError(e));
      } finally {
        setBusy(false);
      }
    },
    [item.item_id, live, confirmLive, onDecided],
  );
  const open = item.state === "waiting" || item.state === "edited";
  const act = useCallback(
    (k: string) => {
      if (!item.mine || busy || !open) return;
      if (k === "a") void decide("accept", editing ? { text } : {});
      else if (k === "e" && draft?.body) {
        setEditing(true);
        setTimeout(() => textRef.current?.focus(), 0);
      } else if (k === "r") void decide("reject");
      else if (k === "s") setSnoozeOpen((v) => !v);
    },
    [item.mine, busy, open, decide, editing, text, draft?.body],
  );
  const liRef = useRef<HTMLLIElement | null>(null);
  useEffect(() => {
    register(liRef.current, { act });
    return () => register(null, null);
  }, [register, act]);
  const outcome = item.decision?.outcome;
  return (
    <li
      ref={liRef}
      tabIndex={0}
      data-key={itemKey}
      aria-label={item.title}
      data-testid="autopilot-item"
      className={cn("space-y-2 rounded-lg border border-border p-3 text-[13px] outline-none focus-visible:border-border-strong focus-visible:ring-2 focus-visible:ring-accent")}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <p className="min-w-0 flex-1 break-words font-medium">{item.title}</p>
        <StatusWord status={item.state === "accepted" ? "ok" : item.state === "rejected" ? "neutral" : item.state === "snoozed" ? "risk" : "new"} label={ITEM_STATE[item.state]} />
      </div>
      <p className="break-words text-[12px] text-muted">
        {[personName(area, item.owner), workflowLabel(item.workflow), systemLabel(item.system), `due ${formatDateTime(item.due_at)}`].join(" · ")}
        {!item.mine && " · view only"}
      </p>
      {(happenedLine(happened) || objectId) && (
        <div className="rounded-lg bg-surface p-2.5">
          <p className="mb-0.5 text-[12px] text-muted">What happened</p>
          {happenedLine(happened) && <p className="break-words">{happenedLine(happened)}</p>}
          {objectId && (
            <Link className="text-[12px] font-medium hover:underline" href={objectHref(objectId.split(":")[0], objectId)}>
              Open the record
            </Link>
          )}
        </div>
      )}
      {typeof prepared.summary === "string" && <p className="break-words">{prepared.summary}</p>}
      {write?.operation && (
        <p className="break-words text-[12px]">
          <span className="text-muted">Prepared entry: </span>
          {systemLabel(String(write.system ?? item.system))}, {write.operation.replace(/_/g, " ")}
          {write.reason ? ` (${write.reason})` : ""}
        </p>
      )}
      {draft?.body && !editing && (
        <div className="rounded-lg bg-surface p-2.5">
          <p className="mb-1 text-[12px] text-muted">
            {item.decision?.text ? "Your edited draft" : "Drafted reply"}
            {draft.to?.[0]?.address ? ` to ${draft.to[0].address}` : ""}
            {draft.subject ? ` · ${draft.subject}` : ""} · not sent
          </p>
          <p className="whitespace-pre-wrap break-words leading-5">{item.decision?.text ?? draft.body}</p>
        </div>
      )}
      {editing && (
        <label className="block space-y-1">
          <span className="text-[12px] text-muted">Edit the draft</span>
          <textarea
            ref={textRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={6}
            className="w-full rounded-lg border border-border bg-bg p-2 text-[13px] leading-5 text-fg"
            onKeyDown={(e) => {
              if (e.key === "Escape") setEditing(false);
              e.stopPropagation();
            }}
          />
        </label>
      )}
      {gaps.length > 0 && <p className="text-[12px] text-muted">Missing: {gaps.join(", ").replaceAll("_", " ")}</p>}
      {(item.citations ?? []).length > 0 ? (
        <p className="flex flex-wrap gap-x-3 gap-y-1 text-[12px]" aria-label="Evidence">
          <span className="text-muted">Evidence</span>
          {(item.citations ?? []).slice(0, 8).map((h, n) => (
            <Link key={h} href={sourceHref(h)} className="font-medium hover:underline" data-tap="inline">
              Source {n + 1}
            </Link>
          ))}
          {(item.citations ?? []).length > 8 && <span className="text-muted">and {(item.citations ?? []).length - 8} more</span>}
        </p>
      ) : (
        <p className="text-[12px] text-muted">No evidence cited.</p>
      )}
      {item.decision && (
        <p className="text-[12px] text-muted" data-testid="autopilot-decision">
          {ITEM_STATE[item.state]} {formatDateTime(item.decision.at)}
          {item.decision.by_you ? " by you" : ""}
          {item.decision.until ? `, until ${formatDateTime(item.decision.until)}` : ""}
          {outcome === "dry_run" && ". Dry run: your decision is recorded, nothing was sent or written."}
          {outcome === "done" && ". Done live through the write path."}
          {outcome === "killed" && ". Recorded; switched off, so nothing was performed."}
          {outcome && !["dry_run", "done", "killed"].includes(outcome) &&
            `. Recorded; the write path would not perform it (${[outcome, ...(item.decision.reasons ?? []).slice(0, 2)].join(", ").replace(/_/g, " ")}), so nothing was sent or written.`}
        </p>
      )}
      {error && <ErrorNotice message={error} />}
      {item.mine && open && (
        <div className="space-y-2">
          {confirmLive && (
            <p className="rounded-lg bg-risk-soft px-3 py-2" role="status">
              This workflow is live: accepting performs it in {systemLabel(item.system)}. Accept again to confirm.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={busy} onClick={() => void decide("accept", editing ? { text } : {})} aria-keyshortcuts="a">
              {confirmLive ? "Confirm and perform" : editing ? "Accept with edit" : "Accept"}
            </Button>
            {draft?.body &&
              (editing ? (
                <Button size="sm" variant="outline" disabled={busy || !text.trim()} onClick={() => void decide("edit", { text })}>
                  Save edit
                </Button>
              ) : (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => act("e")} aria-keyshortcuts="e">
                  Edit
                </Button>
              ))}
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => void decide("reject")} aria-keyshortcuts="r">
              Reject
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} aria-expanded={snoozeOpen} onClick={() => setSnoozeOpen((v) => !v)} aria-keyshortcuts="s">
              Snooze
            </Button>
            {(editing || confirmLive) && (
              <Button size="sm" variant="ghost" onClick={() => (setEditing(false), setConfirmLive(false))}>
                Cancel
              </Button>
            )}
          </div>
          {snoozeOpen && (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Snooze until">
              {snoozeChoices().map(([until, label]) => (
                <button key={label} type="button" className={chip(false)} onClick={() => void decide("snooze", { until })}>
                  {label}
                </button>
              ))}
            </div>
          )}
          <p className="text-[11px] text-muted">{live ? "Live: accept performs it through the write path after your confirm." : "Dry run: accept records your decision only. Nothing is sent or written."}</p>
        </div>
      )}
    </li>
  );
}

function TodayView({ area, filters, split, decided, onDecided }: { area: Area; filters: Filters; split: Split; decided: Set<string>; onDecided: (i: AutopilotItem) => void }) {
  // an item decided here stays in view with its outcome until the view changes
  const items = useMemo(() => {
    const shown = new Set(filterItems(area, filters).map((i) => i.item_id));
    const relaxed = filterItems(area, { ...filters, state: "" });
    return relaxed.filter((i) => shown.has(i.item_id) || decided.has(i.item_id));
  }, [area, filters, decided]);
  const groups = useMemo(() => splitItems(area, items, split), [area, items, split]);
  const flat = groups.flatMap((g) => g.items.map((i) => `${g.key}/${i.item_id}`));
  const els = useRef(new Map<string, { el: HTMLLIElement; act: (k: string) => void }>());
  const onKey = (e: React.KeyboardEvent) => {
    const t = e.target as HTMLElement;
    if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    const focus = (t.closest("li[data-key]") as HTMLElement | null)?.dataset.key ?? null;
    const at = focus ? flat.indexOf(focus) : -1;
    if (e.key === "j" || e.key === "ArrowDown" || e.key === "k" || e.key === "ArrowUp") {
      if (t.tagName === "BUTTON" && e.key.startsWith("Arrow")) return;
      const next = flat[Math.max(0, Math.min(flat.length - 1, at + (e.key === "j" || e.key === "ArrowDown" ? 1 : -1)))];
      if (next) {
        e.preventDefault();
        els.current.get(next)?.el.focus();
      }
    } else if (focus && t.tagName === "LI" && ["a", "e", "r", "s"].includes(e.key)) {
      e.preventDefault();
      els.current.get(focus)?.act(e.key);
    }
  };
  if ((area.items ?? []).length === 0)
    return <EmptyState title="Nothing prepared yet." line={(area.notes ?? [])[0] ?? "Prepared items appear here as mail, Teams, MC and records change."} />;
  if (items.length === 0) return <EmptyState title="Nothing matches this view." line="Clear a filter or show every open item, not only today's." />;
  return (
    <div onKeyDown={onKey} className="space-y-5">
      <p className="text-[12px] text-muted">
        {plural(items.length, "item")} · keys: j and k move, a accept, e edit, r reject, s snooze
      </p>
      {groups.map((g) => (
        <section key={g.key || "all"} aria-label={g.label || "Prepared items"} className="space-y-2">
          {g.label && (
            <h3 className="text-[13px] font-medium">
              {g.label} <span className="font-normal text-muted">· {g.items.length}</span>
            </h3>
          )}
          <ul className="space-y-3">
            {g.items.map((i) => {
              const k = `${g.key}/${i.item_id}`;
              return (
                <Item
                  key={k}
                  area={area}
                  item={i}
                  itemKey={k}
                  onDecided={onDecided}
                  register={(el, api) => (el && api ? els.current.set(k, { el, act: api.act }) : els.current.delete(k))}
                />
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}

// A switch: the whole row (track and words) is the target, so a phone tap needs no aim.
function Toggle({ on, label, text, disabled, onChange }: { on: boolean; label: string; text: string; disabled?: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className="group inline-flex cursor-pointer items-center gap-2 rounded-md text-[12px] text-fg disabled:cursor-not-allowed"
    >
      <span className={cn("relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors group-disabled:opacity-40", on ? "bg-accent" : "bg-[color-mix(in_srgb,var(--fg)_18%,transparent)]")}>
        <span className={cn("inline-block size-4 rounded-full bg-bg shadow transition-transform", on ? "translate-x-[18px]" : "translate-x-0.5")} />
      </span>
      <span className="group-disabled:text-muted">{text}</span>
    </button>
  );
}

function WorkflowsView({ area, filters, onArea }: { area: Area; filters: Filters; onArea: (a: Area) => void }) {
  const rows = useMemo(() => filterWorkflows(area, filters), [area, filters]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const flip = async (row: AutopilotWorkflowRow, body: { on?: boolean; live?: boolean; role?: string }) => {
    setBusy(`${row.role}/${row.workflow}`);
    setError(null);
    try {
      onArea(await setAutopilotWorkflow(row.workflow, body));
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(null);
    }
  };
  if ((area.workflows ?? []).length === 0) return <EmptyState title="No workflow has been scored yet." line="Scores appear after the first replay of a role's days." />;
  if (rows.length === 0) return <EmptyState title="No workflow matches this view." />;
  return (
    <div className="space-y-3">
      {error && <ErrorNotice message={error} />}
      <div className="hidden grid-cols-[minmax(0,2fr)_minmax(0,1.3fr)_4rem_4rem_3rem_minmax(0,1.6fr)_minmax(0,1.2fr)] gap-3 px-3 text-[11px] text-muted md:grid" aria-hidden>
        <span>Workflow</span>
        <span>Person or role</span>
        <span className="text-right">Dev</span>
        <span className="text-right">Fresh</span>
        <span className="text-right">n</span>
        <span>State</span>
        <span>Last run</span>
      </div>
      <ul className="divide-y divide-border rounded-lg border border-border" aria-label="Workflows">
        {rows.map((r) => {
          const shown = stateShown(r.state, area.owner);
          const k = `${r.role}/${r.workflow}`;
          const canLive = area.owner && r.on && r.fresh != null && r.state !== "dry_run";
          return (
            <li key={k} className="grid grid-cols-2 gap-x-3 gap-y-1 p-3 text-[13px] md:grid-cols-[minmax(0,2fr)_minmax(0,1.3fr)_4rem_4rem_3rem_minmax(0,1.6fr)_minmax(0,1.2fr)] md:items-center" data-testid="autopilot-workflow">
              <span className="col-span-2 break-words font-medium md:col-span-1">{workflowLabel(r.workflow)}</span>
              <span className="col-span-2 text-muted md:col-span-1 md:text-fg">{rowRole(area, r)}</span>
              <span className="tabular-nums md:text-right">
                <span className="text-muted md:hidden">Dev </span>
                {pct(r.dev)}
              </span>
              <span className="tabular-nums md:text-right">
                <span className="text-muted md:hidden">Fresh </span>
                {pct(r.fresh)}
              </span>
              <span className="tabular-nums md:text-right">
                <span className="text-muted md:hidden">n </span>
                {r.n}
              </span>
              <span className="col-span-2 md:col-span-1">
                <StatusBadge status={shown.status} label={shown.label} />
              </span>
              <span className="col-span-2 text-[12px] text-muted md:col-span-1">{r.last_run ? formatDateTime(r.last_run) : "Not run yet"}</span>
              {area.owner && (
                <div className="col-span-2 flex flex-wrap items-center gap-x-4 gap-y-2 pt-1 md:col-span-7">
                  <Toggle on={r.on} label={`${workflowLabel(r.workflow)} on`} text={r.on ? "On" : "Off"} disabled={busy === k || area.switches.global_off} onChange={(v) => void flip(r, { on: v })} />
                  <Toggle
                    on={r.live_flag}
                    label={`${workflowLabel(r.workflow)} live for ${rowRole(area, r)}`}
                    text={r.live_flag ? "Live" : r.state === "dry_run" ? "Live after it passes (90% on 15 fresh)" : "Switch live"}
                    disabled={busy === k || (!r.live_flag && !canLive)}
                    onChange={(v) => void flip(r, { live: v, role: r.role })}
                  />
                  {r.open_items > 0 && <span className="text-[12px] text-muted">{plural(r.open_items, "open item")}</span>}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function AuditView({ area }: { area: Area }) {
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
  const rows = (audit.rows ?? []).filter((r) => !liveOnly || r.mode === "live" || r.event === "switch" || r.event === "reconciled");
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
                {[formatDateTime(r.at), roleName(r.role), r.system ? systemLabel(r.system) : null, r.workflow ? workflowLabel(r.workflow) : null, r.operation?.replace(/_/g, " "), r.reasons?.length ? r.reasons.join(", ").replace(/_/g, " ") : null]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              {r.undone && <p className="text-[12px] text-muted">Undone.</p>}
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

function auditWhat(r: AutopilotAuditRow): string {
  if (r.event === "switch") return "Switch changed";
  if (r.event === "gate_passed") return "Gate passed";
  if (r.event === "gate_lost") return "Gate lost: back to dry run";
  if (r.event === "owner_notified") return "Owner told of the pass";
  if (r.event === "reconciled") return `Checked by a person: ${r.status}`;
  const s = (r.status ?? "").replace(/_/g, " ");
  if (r.mode === "live") return r.event === "attempt" ? "Live attempt" : `Live: ${s}`;
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : r.event;
}

export function AutopilotView({ area: initial, onReload }: { area: Area; onReload: () => void }) {
  const [area, setArea] = useState(initial);
  useEffect(() => setArea(initial), [initial]);
  const [tab, setTab] = useState<Tab>("today");
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [split, setSplit] = useState<Split>("");
  const [decided, setDecided] = useState<Set<string>>(new Set());
  useEffect(() => setDecided(new Set()), [filters, split]);
  const [killError, setKillError] = useState<string | null>(null);
  const [killConfirm, setKillConfirm] = useState(false);
  const set = (k: keyof Filters) => (v: string | boolean) => setFilters((f) => ({ ...f, [k]: v }));
  const onDecided = useCallback((i: AutopilotItem) => {
    setDecided((d) => new Set(d).add(i.item_id));
    setArea((a) => ({ ...a, items: (a.items ?? []).map((x) => (x.item_id === i.item_id ? i : x)) }));
  }, []);
  const tabs: [Tab, string][] = [["today", "Prepared items"], ["workflows", "Workflows"], ...(area.owner ? ([["audit", "Audit log"]] as [Tab, string][]) : [])];
  const kill = async (off: boolean) => {
    setKillError(null);
    try {
      const switches = await setAutopilotGlobal(off);
      setArea((a) => ({ ...a, switches }));
      setKillConfirm(false);
      onReload();
    } catch (e) {
      setKillError(plainError(e));
    }
  };
  const items = area.items ?? [];
  const people = area.people ?? [];
  const onTabKey = (e: React.KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length][0];
    setTab(next);
    (document.getElementById(`ap-tab-${next}`) as HTMLElement | null)?.focus();
  };
  return (
    <div className="space-y-4" data-testid="autopilot">
      <AssistBox />
      {area.switches.global_off && (
        <ErrorNotice tone="notice" message={area.owner ? "Every autopilot write is switched off. Accepts are refused until you switch it back on." : "The owner switched every autopilot write off. Accepts are refused for now."} />
      )}
      {area.switches.autopilot_off && <ErrorNotice tone="notice" message="The autopilot is switched off on this server: nothing new is prepared." />}
      {area.owner && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3 text-[13px]">
          <p className="min-w-0 flex-1">
            {area.switches.global_off ? "All writes stopped." : "Writes run only for workflows you switched live."}{" "}
            <span className="text-muted">{area.switches.master_switch ? "The host's live switch is on." : "The host's live switch is off: nothing goes live."}</span>
          </p>
          {area.switches.global_off ? (
            <Button size="sm" variant="outline" onClick={() => void kill(false)}>
              Switch writes back on
            </Button>
          ) : killConfirm ? (
            <>
              <Button size="sm" variant="outline" onClick={() => void kill(true)}>
                Confirm: stop all writes
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setKillConfirm(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <Button size="sm" variant="outline" onClick={() => setKillConfirm(true)}>
              Stop all writes
            </Button>
          )}
          {killError && <ErrorNotice message={killError} className="w-full" />}
        </div>
      )}
      <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Autopilot">
        {tabs.map(([t, label], i) => (
          <button key={t} id={`ap-tab-${t}`} type="button" role="tab" aria-selected={tab === t} tabIndex={tab === t ? 0 : -1} className={chip(tab === t)} onClick={() => setTab(t)} onKeyDown={(e) => onTabKey(e, i)}>
            {label}
          </button>
        ))}
      </div>
      {tab !== "audit" && (
        <div className="space-y-3">
          {people.length > 0 && (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Person">
              {[{ id: "", name: "Everyone", you: false }, ...people].map((p) => (
                <button key={p.id || "all"} type="button" aria-pressed={filters.person === p.id} className={chip(filters.person === p.id)} onClick={() => set("person")(p.id)}>
                  {p.id ? personName(area, p.id) : p.name}
                  {p.id && <span className="tabular-nums text-faint">{items.filter((i) => i.owner === p.id && (i.state === "waiting" || i.state === "edited")).length}</span>}
                </button>
              ))}
            </div>
          )}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Select label="Role" value={filters.role} onChange={set("role")} options={[["", "Every role"], ...(area.roles ?? []).map((r) => [r, roleLabel(r)] as [string, string])]} />
            <Select label="Workflow" value={filters.workflow} onChange={set("workflow")} options={[["", "Every workflow"], ...uniq([...items.map((i) => i.workflow), ...(area.workflows ?? []).map((w) => w.workflow)]).map((w) => [w, workflowLabel(w)] as [string, string])]} />
            {tab === "today" && (
              <>
                <Select label="System" value={filters.system} onChange={set("system")} options={[["", "Every system"], ...uniq(items.map((i) => i.system)).map((s) => [s, systemLabel(s)] as [string, string])]} />
                <Select label="State" value={filters.state} onChange={set("state")} options={[["open", "Waiting on a decision"], ["", "Any state"], ["accepted", "Accepted"], ["rejected", "Rejected"], ["snoozed", "Snoozed"]]} />
                <Select label="When" value={filters.today ? "today" : "all"} onChange={(v) => set("today")(v === "today")} options={[["today", "Today and past due"], ["all", "Every open item"]]} />
                <Select label="Split by" value={split} onChange={(v) => setSplit(v as Split)} options={[["", "No split"], ["person", "Person"], ["role", "Role"], ["workflow", "Workflow"], ["system", "System"], ["state", "State"]]} />
              </>
            )}
          </div>
        </div>
      )}
      <div role="tabpanel" aria-labelledby={`ap-tab-${tab}`}>
        {tab === "today" && <TodayView area={area} filters={filters} split={split} decided={decided} onDecided={onDecided} />}
        {tab === "workflows" && <WorkflowsView area={area} filters={filters} onArea={setArea} />}
        {tab === "audit" && area.owner && <AuditView area={area} />}
      </div>
    </div>
  );
}
