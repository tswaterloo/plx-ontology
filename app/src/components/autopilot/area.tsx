"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BuilderView, SchedulesView } from "@/components/autopilot/builder";
import { WorkflowsView } from "@/components/autopilot/workflows";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorNotice, LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StatusBadge, StatusWord } from "@/components/ui/status-badge";
import { decideAutopilotItem, getAutopilotAudit, getAutopilotCatalog, setAutopilotGlobal, undoAutopilotAction } from "@/lib/api";
import {
  ITEM_STATE,
  NO_FILTERS,
  type Filters,
  type Split,
  dueWords,
  filterItems,
  filterWorkflows,
  itemLive,
  personName,
  roleLabel,
  rowRole,
  splitItems,
  systemLabel,
  workflowLabel,
} from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDateTime, objectHref, plural, sourceHref } from "@/lib/format";
import type { AutopilotArea as Area, AutopilotAudit, AutopilotAuditRow, AutopilotCatalog, AutopilotItem } from "@/lib/types";

// The autopilot studio: the review queue (prepared items with their evidence and drafted
// action: accept, edit, reject, snooze, one by one or in bulk), the workflows with their
// gate and mode, the builder, the schedules and the audit log with undo. An accept is a
// recorded dry run until its workflow passed the fresh gate and the owner set it to Auto;
// the server decides both. A bulk accept never performs: a live item is accepted alone,
// after its own confirm.

type Tab = "queue" | "workflows" | "builder" | "schedules" | "audit";

const chip = (on: boolean) =>
  cn(
    "inline-flex h-7 shrink-0 cursor-pointer items-center gap-1.5 rounded-[3px] px-3 text-[12px]",
    on ? "bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover",
  );

const selectCls = "h-8 min-w-0 rounded-lg border border-border bg-bg px-2 text-[13px] text-fg";

/** A filter select; one that has nothing to choose (only its "every" option, already
 * chosen) is not shown at all. */
function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: [string, string][] }) {
  if (options.length < 2 && options[0]?.[0] === value) return null;
  return (
    <label className="flex max-w-full flex-col gap-0.5 text-[11px] text-muted">
      {label}
      <select className={cn(selectCls, "max-w-[20rem]")} value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
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

// One prepared item in the queue's detail pane: what happened, the evidence, the drafted
// action and your decision.
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
  register: (el: HTMLElement | null, api: { act: (k: string) => void } | null) => void;
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
  // the server's confirm token: set when an accept would perform the item live
  const [confirmLive, setConfirmLive] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [snoozeOpen, setSnoozeOpen] = useState(false);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const decide = useCallback(
    async (decision: "accept" | "edit" | "reject" | "snooze", extra: { text?: string; until?: string } = {}) => {
      setBusy(true);
      setError(null);
      try {
        const token = decision === "accept" ? confirmLive : null;
        const out = await decideAutopilotItem(item.item_id, { decision, ...extra, ...(token ? { confirm_token: token } : {}) });
        if (decision === "accept" && out.confirm_token) {
          // live: nothing was performed; the server asks for this accept once more
          setConfirmLive(out.confirm_token);
          return;
        }
        setEditing(false);
        setConfirmLive(null);
        setSnoozeOpen(false);
        onDecided(out);
      } catch (e) {
        setError(plainError(e));
      } finally {
        setBusy(false);
      }
    },
    [item.item_id, confirmLive, onDecided],
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
  const liRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    register(liRef.current, { act });
    return () => register(null, null);
  }, [register, act]);
  const outcome = item.decision?.outcome;
  return (
    <article
      ref={liRef}
      tabIndex={0}
      data-key={itemKey}
      aria-label={item.title}
      data-testid="autopilot-item"
      className="space-y-4 text-[13px] outline-none"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <h2 className="min-w-0 flex-1 break-words text-[15px] font-semibold tracking-[-0.01em]">{item.title}</h2>
        <StatusWord status={item.state === "accepted" ? "ok" : item.state === "rejected" ? "neutral" : item.state === "snoozed" ? "risk" : "new"} label={ITEM_STATE[item.state]} />
      </div>
      <p className="-mt-3 break-words text-[12px] text-muted">
        {[personName(area, item.owner), workflowLabel(item.workflow), systemLabel(item.system), `due ${formatDateTime(item.due_at)}`].join(" · ")}
        {!item.mine && " · view only"}
      </p>
      {(happenedLine(happened) || objectId) && (
        <div className="border-l-[3px] border-border-strong pl-3">
          <p className="mb-0.5 text-[11px] font-semibold tracking-[0.04em] text-muted uppercase">What happened</p>
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
        <div className="rounded-[4px] border border-border p-3">
          <p className="mb-1.5 text-[12px] text-muted">
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
            <p className="border-l-[3px] border-risk pl-3" role="status">
              This item is live: accepting performs it in {systemLabel(item.system)}. Accept again to confirm.
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
              <Button size="sm" variant="ghost" onClick={() => (setEditing(false), setConfirmLive(null))}>
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
    </article>
  );
}

/** The review queue: the items as rows on the left (select for bulk), the selected item's
 * evidence, draft and decision on the right. j/k move, a/e/r/s act on the selected one. */
function QueueView({ area, filters, split, decided, onDecided }: { area: Area; filters: Filters; split: Split; decided: Set<string>; onDecided: (i: AutopilotItem) => void }) {
  // an item decided here stays in view with its outcome until the view changes
  const items = useMemo(() => {
    const shown = new Set(filterItems(area, filters).map((i) => i.item_id));
    const relaxed = filterItems(area, { ...filters, state: "" });
    return relaxed.filter((i) => shown.has(i.item_id) || decided.has(i.item_id));
  }, [area, filters, decided]);
  const groups = useMemo(() => splitItems(area, items, split), [area, items, split]);
  const flat = useMemo(() => groups.flatMap((g) => g.items.map((i) => `${g.key}/${i.item_id}`)), [groups]);
  const [sel, setSel] = useState<string | null>(null);
  const current = sel && flat.includes(sel) ? sel : (flat[0] ?? null);
  const currentItem = current ? items.find((i) => i.item_id === current.split("/").slice(1).join("/")) ?? null : null;
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkNote, setBulkNote] = useState<string | null>(null);
  const actRef = useRef<((k: string) => void) | null>(null);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());
  const now = new Date();
  const open = (i: AutopilotItem) => i.state === "waiting" || i.state === "edited";
  const pickedItems = items.filter((i) => picked.has(i.item_id) && i.mine && open(i));
  const liveInPick = pickedItems.filter((i) => itemLive(area, i));
  const bulk = async (decision: "accept" | "reject" | "snooze") => {
    const list = decision === "accept" ? pickedItems.filter((i) => !itemLive(area, i)) : pickedItems;
    setBulkBusy(true);
    setBulkNote(null);
    let failed = 0;
    for (const i of list) {
      try {
        // bulk: the server refuses a live item (409) instead of performing it
        onDecided(await decideAutopilotItem(i.item_id, decision === "snooze" ? { decision, until: snoozeChoices()[1][0], bulk: true } : { decision, bulk: true }));
      } catch {
        failed += 1;
      }
    }
    setBulkBusy(false);
    setPicked(new Set());
    setBulkNote(failed ? `${plural(list.length - failed, "item")} done; ${failed} refused, left as they were.` : `${plural(list.length, "item")} ${decision === "accept" ? "accepted" : decision === "reject" ? "rejected" : "snoozed until tomorrow morning"}.`);
  };
  const move = (by: number) => {
    const at = current ? flat.indexOf(current) : -1;
    const next = flat[Math.max(0, Math.min(flat.length - 1, at + by))];
    if (next) {
      setSel(next);
      rowRefs.current.get(next)?.focus();
    }
  };
  const onKey = (e: React.KeyboardEvent) => {
    const t = e.target as HTMLElement;
    if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (e.key === "j" || e.key === "ArrowDown") {
      e.preventDefault();
      move(1);
    } else if (e.key === "k" || e.key === "ArrowUp") {
      e.preventDefault();
      move(-1);
    } else if (["a", "e", "r", "s"].includes(e.key) && (t.dataset.queueRow !== undefined || t.dataset.key !== undefined)) {
      e.preventDefault();
      actRef.current?.(e.key);
    }
  };
  if ((area.items ?? []).length === 0)
    return <EmptyState title="Nothing prepared yet." line={(area.notes ?? [])[0] ?? "Prepared items appear here as mail, Teams, MC and records change."} />;
  if (items.length === 0) return <EmptyState title="Nothing matches this view." line="Clear a filter or show every open item, not only today's." />;
  const mineOpen = items.filter((i) => i.mine && open(i));
  return (
    <div onKeyDown={onKey} className="grid min-h-[520px] gap-0 md:grid-cols-[minmax(300px,380px)_minmax(0,1fr)]" data-testid="autopilot-queue">
      <div className="min-w-0 md:border-r md:border-border md:pr-0">
        <div className="flex h-9 items-center gap-2 border-b border-border px-2 text-[12px] text-muted">
          <input
            type="checkbox"
            aria-label="Select every item of yours"
            checked={mineOpen.length > 0 && mineOpen.every((i) => picked.has(i.item_id))}
            onChange={(e) => setPicked(e.target.checked ? new Set(mineOpen.map((i) => i.item_id)) : new Set())}
            disabled={mineOpen.length === 0}
          />
          {picked.size > 0 ? (
            <span className="flex flex-1 flex-wrap items-center gap-1" role="group" aria-label="With the selected">
              <span className="num mr-1 text-fg">{picked.size}</span>
              <button type="button" className={chip(false)} disabled={bulkBusy || pickedItems.length === liveInPick.length} onClick={() => void bulk("accept")} title={liveInPick.length ? "A live item is accepted alone, after its own confirm" : undefined}>
                Accept{liveInPick.length ? ` ${pickedItems.length - liveInPick.length}` : ""}
              </button>
              <button type="button" className={chip(false)} disabled={bulkBusy} onClick={() => void bulk("snooze")}>
                Snooze
              </button>
              <button type="button" className={chip(false)} disabled={bulkBusy} onClick={() => void bulk("reject")}>
                Reject
              </button>
            </span>
          ) : (
            <span className="flex-1">
              <span className="num text-fg">{items.length}</span> {items.length === 1 ? "item" : "items"} · j k move · a e r s act
            </span>
          )}
        </div>
        {liveInPick.length > 0 && picked.size > 0 && <p className="border-b border-border px-2 py-1.5 text-[12px] text-muted">{plural(liveInPick.length, "live item")} left out of a bulk accept: open each and confirm it.</p>}
        {bulkNote && <p className="border-b border-border px-2 py-1.5 text-[12px] text-muted" role="status">{bulkNote}</p>}
        {groups.map((g) => (
          <section key={g.key || "all"} aria-label={g.label || "Prepared items"}>
            {g.label && (
              <h3 className="flex h-8 items-end px-2 pb-1 text-[12px] font-semibold">
                {g.label} <span className="ml-1 font-normal text-muted">· {g.items.length}</span>
              </h3>
            )}
            <ul>
              {g.items.map((i) => {
                const k = `${g.key}/${i.item_id}`;
                const due = dueWords(i.due_at, now);
                const late = due.endsWith("overdue");
                const live = itemLive(area, i);
                return (
                  <li key={k} className={cn("flex items-stretch border-b border-border", k === current && "bg-[color-mix(in_srgb,var(--accent)_7%,transparent)] shadow-[inset_3px_0_0_var(--accent)]")} data-testid="autopilot-row">
                    <span className="flex w-7 shrink-0 items-start justify-center pt-2.5">
                      {i.mine && open(i) && (
                        <input type="checkbox" aria-label={`Select ${i.title}`} checked={picked.has(i.item_id)} onChange={(e) => setPicked((p) => { const n = new Set(p); if (e.target.checked) n.add(i.item_id); else n.delete(i.item_id); return n; })} />
                      )}
                    </span>
                    <button
                      type="button"
                      ref={(el) => { if (el) rowRefs.current.set(k, el); else rowRefs.current.delete(k); }}
                      data-queue-row=""
                      aria-current={k === current ? "true" : undefined}
                      aria-label={i.title}
                      onClick={() => setSel(k)}
                      onFocus={() => setSel(k)}
                      className="min-w-0 flex-1 py-2 pr-2 text-left outline-none cursor-pointer focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
                    >
                      <span className="flex items-baseline gap-2">
                        <span className={cn("min-w-0 flex-1 truncate text-[13px]", open(i) ? "font-medium text-fg" : "text-muted")}>{i.title}</span>
                        <span className={cn("num shrink-0 text-[11px]", late && open(i) ? "text-late" : "text-muted")}>{open(i) ? due : ITEM_STATE[i.state]}</span>
                      </span>
                      {happenedLine(i.happened ?? {}) && <span className="mt-0.5 block truncate text-[12px] text-fg/80">{happenedLine(i.happened ?? {})}</span>}
                      <span className="mt-0.5 block truncate text-[12px] text-muted">
                        {[personName(area, i.owner), workflowLabel(i.workflow), systemLabel(i.system)].join(" · ")}
                        {live && <span className="text-accent"> · live</span>}
                        {!i.mine && " · view only"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
      <div className="min-w-0 px-0 pt-5 md:px-6 md:pt-3">
        {currentItem ? (
          <Item
            key={current!}
            area={area}
            item={currentItem}
            itemKey={current!}
            onDecided={onDecided}
            register={(_el, api) => {
              actRef.current = api ? api.act : null;
            }}
          />
        ) : null}
      </div>
    </div>
  );
}

function AuditView({ area, filters }: { area: Area; filters: Filters }) {
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
  const [tab, setTab] = useState<Tab>("queue");
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [split, setSplit] = useState<Split>("");
  const [decided, setDecided] = useState<Set<string>>(new Set());
  useEffect(() => setDecided(new Set()), [filters, split]);
  const [killError, setKillError] = useState<string | null>(null);
  const [killConfirm, setKillConfirm] = useState(false);
  const [catalog, setCatalog] = useState<AutopilotCatalog | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    getAutopilotCatalog(ctl.signal).then(setCatalog, () => !ctl.signal.aborted && setCatalog({ triggers: [], conditions: [], actions: [], builder: false }));
    return () => ctl.abort();
  }, []);
  const set = (k: keyof Filters) => (v: string | boolean) => setFilters((f) => ({ ...f, [k]: v }));
  const onDecided = useCallback((i: AutopilotItem) => {
    setDecided((d) => new Set(d).add(i.item_id));
    setArea((a) => ({ ...a, items: (a.items ?? []).map((x) => (x.item_id === i.item_id ? i : x)) }));
  }, []);
  const tabs: [Tab, string][] = [
    ["queue", "Review queue"],
    ["workflows", "Workflows"],
    ...(area.owner ? ([["builder", "Builder"]] as [Tab, string][]) : []),
    ["schedules", "Schedules"],
    ...(area.owner ? ([["audit", "Audit log"]] as [Tab, string][]) : []),
  ];
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
  const open = items.filter((i) => i.state === "waiting" || i.state === "edited");
  const now = Date.now();
  const overdue = open.filter((i) => new Date(i.due_at).getTime() < now).length;
  const live = (area.workflows ?? []).filter((w) => w.state === "live" || w.state === "live_held").length;
  const passed = (area.workflows ?? []).filter((w) => w.state === "passed_awaiting_owner").length;
  const shownWorkflows = new Set(filterWorkflows(area, filters).map((w) => w.workflow));
  return (
    <div className="space-y-4" data-testid="autopilot">
      {area.switches.global_off && (
        <ErrorNotice tone="notice" message={area.owner ? "Every autopilot write is switched off. Accepts are refused until you switch it back on." : "The owner switched every autopilot write off. Accepts are refused for now."} />
      )}
      {area.switches.autopilot_off && <ErrorNotice tone="notice" message="The autopilot is switched off on this server: nothing new is prepared." />}
      <div className="flex flex-wrap items-end gap-x-10 gap-y-3 border-b border-border pb-4" data-testid="autopilot-figures">
        {(
          [
            ["Waiting on a decision", open.length, ""],
            ["Overdue", overdue, overdue ? "text-late" : ""],
            ["Workflows", (area.workflows ?? []).length, ""],
            ["Passed, awaiting you", passed, passed ? "text-accent" : ""],
            ["Auto", live, ""],
          ] as const
        ).map(([k, v, tone]) => (
          <div key={k}>
            <p className="text-[12px] text-muted">{k}</p>
            <p className={cn("num text-[19px] leading-6", tone)}>{v}</p>
          </div>
        ))}
        {area.owner && (
          <div className="ml-auto flex flex-wrap items-center gap-2 text-[12px]">
            <span className="text-muted">
              {area.switches.global_off ? "All writes stopped." : "Writes run only for workflows set to Auto."} {area.switches.master_switch ? "The host's live switch is on." : "The host's live switch is off: nothing goes live."}
            </span>
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
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <nav className="flex h-9 items-stretch gap-4" role="tablist" aria-label="Autopilot">
          {tabs.map(([t, label], i) => (
            <button
              key={t}
              id={`ap-tab-${t}`}
              type="button"
              role="tab"
              aria-selected={tab === t}
              tabIndex={tab === t ? 0 : -1}
              className={cn("relative flex items-center text-[13px] cursor-pointer", tab === t ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:bg-accent" : "text-muted hover:text-fg")}
              onClick={() => setTab(t)}
              onKeyDown={(e) => onTabKey(e, i)}
            >
              {label}
              {t === "queue" && open.length > 0 && <span className="num ml-1.5 text-[11px] text-faint">{open.length}</span>}
            </button>
          ))}
        </nav>
        {tab !== "builder" && (
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            {people.length > 0 && (
              <div className="flex flex-wrap gap-1" role="group" aria-label="Person">
                {[{ id: "", name: "Everyone", you: false }, ...people].map((p) => (
                  <button key={p.id || "all"} type="button" aria-pressed={filters.person === p.id} className={chip(filters.person === p.id)} onClick={() => set("person")(p.id)}>
                    {p.id ? personName(area, p.id) : p.name}
                    {p.id && <span className="num text-faint">{items.filter((i) => i.owner === p.id && (i.state === "waiting" || i.state === "edited")).length}</span>}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      {tab !== "builder" && (
        <div className="flex flex-wrap gap-2" data-testid="autopilot-filters">
          <Select label="Role" value={filters.role} onChange={set("role")} options={[["", "Every role"], ...(area.roles ?? []).map((r) => [r, roleLabel(r)] as [string, string])]} />
          <Select label="Workflow" value={filters.workflow} onChange={set("workflow")} options={[["", "Every workflow"], ...uniq([...items.map((i) => i.workflow), ...(area.workflows ?? []).map((w) => w.workflow), ...(area.known_workflows ?? [])]).map((w) => [w, workflowLabel(w)] as [string, string])]} />
          {tab === "queue" && (
            <>
              <Select label="System" value={filters.system} onChange={set("system")} options={[["", "Every system"], ...uniq(items.map((i) => i.system)).map((s) => [s, systemLabel(s)] as [string, string])]} />
              <Select label="State" value={filters.state} onChange={set("state")} options={[["open", "Waiting on a decision"], ["", "Any state"], ["accepted", "Accepted"], ["rejected", "Rejected"], ["snoozed", "Snoozed"]]} />
              <Select label="When" value={filters.today ? "today" : "all"} onChange={(v) => set("today")(v === "today")} options={[["today", "Today and past due"], ["all", "Every open item"]]} />
              <Select label="Split by" value={split} onChange={(v) => setSplit(v as Split)} options={[["", "No split"], ["person", "Person"], ["role", "Role"], ["workflow", "Workflow"], ["system", "System"], ["state", "State"]]} />
            </>
          )}
        </div>
      )}
      <div role="tabpanel" aria-labelledby={`ap-tab-${tab}`}>
        {tab === "queue" && <QueueView area={area} filters={filters} split={split} decided={decided} onDecided={onDecided} />}
        {tab === "workflows" && <WorkflowsView area={area} filters={filters} builder={!!catalog?.builder} onChanged={onReload} />}
        {tab === "builder" && area.owner && <BuilderView area={area} catalog={catalog} />}
        {tab === "schedules" && <SchedulesView filterWorkflow={(w) => (!filters.person && !filters.role && !filters.workflow) || shownWorkflows.has(w)} />}
        {tab === "audit" && area.owner && <AuditView area={area} filters={filters} />}
      </div>
    </div>
  );
}
