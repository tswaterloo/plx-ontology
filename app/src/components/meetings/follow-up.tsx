"use client";

import { AlertCircle, CheckCircle2, Circle, FileText, Search, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useId, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { Pending } from "@/components/ui/skeleton";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getMeetingFollowUp, getMyActionItems } from "@/lib/api";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import { cn } from "@/lib/cn";
import { formatDate, objectHref } from "@/lib/format";
import type { ActionResult, CardActionOffer } from "@/lib/types";

export interface Person { name?: string | null; address?: string | null }
export interface Linked { value: string; status: "linked" | "ambiguous" | "not_found"; object_id?: string; type?: string; label?: string }
export interface Item { id: string; kind: string; text: string; said_by?: string | null; at?: string | null; citations: string[]; records?: Linked[] }
export interface Prepared {
  kind: "message" | "document_pack" | "write_intent" | "planner_task" | "answer";
  sent: false; writes: 0; citations: string[];
  card?: { id: string; subject: { label: string }; draft?: { to?: { address: string } | null; subject?: string; body?: string } };
  card_id?: string; offers?: CardActionOffer[]; missing?: string[]; approval?: string;
  object_id?: string; families?: string[]; note?: string | null;
  label?: string; needs?: string[];
  task?: { title: string; assignee?: string | null; due?: string | null };
  answers?: { object_id: string; label: string; values: { property: string; value: string; citation: string }[] }[];
}
export interface Action extends Item { owner?: Person | null; due?: string | null; status: "open" | "overdue" | "done"; evidence?: { what: string; source?: string }[]; prepared?: Prepared; meeting?: string; meeting_id?: string; meeting_start?: string }
export interface FollowUp {
  meeting: { meeting_id: string; title: string; start: string; organizer?: Person | null; attendees?: Person[]; markings?: string[] };
  summary?: string; decisions: Item[]; actions: Action[]; questions: Item[]; risks: Item[];
  status: { actions: number; done: number; overdue: number; open: number };
}

const STATUS: Record<Action["status"], string> = { open: "Open", overdue: "Overdue", done: "Done" };

export function statusText(a: Action): string {
  if (a.status === "done") return `Done: ${a.evidence?.[0]?.what ?? "marked done"}`;
  return a.due ? `${STATUS[a.status]}, due ${formatDate(a.due.slice(0, 10))}` : STATUS[a.status];
}

function Cite({ item }: { item: Item }) {
  return item.at ? <span className="num text-[12px] text-muted" title={item.citations.join("\n")}>at {item.at}</span> : null;
}

function Records({ records }: { records?: Linked[] }) {
  const linked = (records ?? []).filter((r) => r.status === "linked" && r.object_id);
  const leads = (records ?? []).filter((r) => r.status === "ambiguous");
  if (!linked.length && !leads.length) return null;
  return (
    <span className="flex flex-wrap gap-2 text-[12.5px]">
      {linked.map((r) => (
        <Link key={r.object_id} href={objectHref(r.type || (r.object_id ?? "").split(":")[0], r.object_id ?? "")} className="font-medium text-accent hover:underline" onClick={(e) => e.stopPropagation()}>
          {r.label ?? r.value}
        </Link>
      ))}
      {leads.map((r) => <span key={r.value} className="text-muted">{r.value}: more than one record, not linked</span>)}
    </span>
  );
}

export function PreparedWork({ work }: { work: Prepared }) {
  const [state, setState] = useState<ActionResult | null>(null);
  if (work.kind === "message" && work.card) {
    return (
      <div className="space-y-1.5 border-l-2 border-border-strong pl-3" data-testid="prepared-message">
        <p className="text-[12.5px] text-muted">Draft {work.card.draft?.to ? `to ${work.card.draft.to.address}` : "(no recipient yet)"}: {work.card.draft?.subject}</p>
        {work.missing?.map((m) => <p key={m} className="text-[12.5px] text-warn">{m}</p>)}
        <CardActions card={{ id: work.card.id, subject: work.card.subject, actions: work.offers }} state={state} onDone={setState} />
      </div>
    );
  }
  if (work.kind === "document_pack" && work.object_id) {
    const type = work.object_id.split(":")[0];
    return <Link className="text-[12.5px] font-medium text-accent hover:underline" href={`${objectHref(type, work.object_id)}#documents`} data-testid="prepared-pack">Open the document pack and its drafted reply</Link>;
  }
  if (work.kind === "write_intent") {
    return (
      <div className="border-l-2 border-border-strong pl-3 text-[12.5px]" data-testid="prepared-write">
        <p className="text-fg">{work.label}: prepared for the write desk (dry run, approved by a second person).</p>
        {work.needs?.length ? <p className="mt-0.5 text-muted">Still needs: {work.needs.join(", ")}</p> : null}
      </div>
    );
  }
  if (work.kind === "answer") {
    return (
      <div className="border-l-2 border-border-strong pl-3 text-[12.5px]" data-testid="prepared-answer">
        {work.answers?.length ? work.answers.map((a) => (
          <p key={a.object_id} className="text-fg"><span className="font-medium">{a.label}</span>: {a.values.map((v) => `${v.property.replace(/_/g, " ")} ${v.value}`).join(", ")}</p>
        )) : <p className="text-muted">{work.note ?? "No value you may read answers it yet."}</p>}
      </div>
    );
  }
  if (work.kind === "planner_task" && work.task) {
    return <p className="border-l-2 border-border-strong pl-3 text-[12.5px] text-fg" data-testid="prepared-planner">Proposed Planner task: {work.task.title}{work.task.due ? `, due ${formatDate(work.task.due.slice(0, 10))}` : ""} (not created: Planner writes wait for approval).</p>;
  }
  return null;
}

function Section({ title, items, empty }: { title: string; items: Item[]; empty: string }) {
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="text-[13.5px] font-semibold text-fg">{title}</h2>
        {items.length > 0 && <span className="num text-[12px] text-muted">{items.length}</span>}
      </div>
      {items.length === 0 ? (
        <p className="px-4 py-3 text-[13.5px] text-muted">{empty}</p>
      ) : (
        <ul className="divide-y divide-border text-[13.5px]">
          {items.map((i) => (
            <li key={i.id} className="flex flex-wrap items-baseline gap-2 px-4 py-2.5 text-fg transition-colors duration-150 hover:bg-row-on">
              <span>{i.text}</span> <Cite item={i} /> <Records records={i.records} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function MeetingFollowUpView({ followUp }: { followUp: FollowUp }) {
  const { meeting, status } = followUp;
  return (
    <div className="space-y-5" data-testid="meeting-follow-up">
      <header className="rounded-xl border border-border bg-surface p-5 space-y-1.5">
        <p className="text-[12px] font-medium text-muted">Meeting follow-up</p>
        <h1 className="text-[20px] leading-7 font-semibold tracking-[-0.02em] text-fg">{meeting.title}</h1>
        <p className="text-[13px] text-muted">{meeting.start.slice(0, 10)}{meeting.organizer?.name ? `, organised by ${meeting.organizer.name}` : ""}</p>
        {followUp.summary && <p className="pt-1 text-[14px] leading-5 text-fg">{followUp.summary}</p>}
        <p className="pt-1 text-[13px] font-medium text-fg tabular-nums" data-testid="follow-up-status">
          {status.done} of {status.actions} action items done{status.overdue ? `, ${status.overdue} overdue` : ""}
        </p>
      </header>
      <Section title="Decisions" items={followUp.decisions} empty="No decision was recorded." />
      <section className="overflow-hidden rounded-xl border border-border bg-surface">
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <h2 className="text-[13.5px] font-semibold text-fg">Action items</h2>
          {followUp.actions.length > 0 && <span className="num text-[12px] text-muted">{followUp.actions.length}</span>}
        </div>
        {followUp.actions.length === 0 ? (
          <p className="px-4 py-3 text-[13.5px] text-muted">No action item was recorded.</p>
        ) : (
          <ul className="divide-y divide-border">
            {followUp.actions.map((a) => (
              <li key={a.id} className="space-y-1.5 px-4 py-3 text-[13.5px] transition-colors duration-150 hover:bg-row-on" data-testid="follow-up-action">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-medium text-fg">{a.text} <Cite item={a} /></span>
                  <span className={cn("text-[12.5px]", a.status === "overdue" ? "font-medium text-bad" : a.status === "done" ? "text-ok" : "text-muted")}>
                    {a.owner?.name ?? "No owner"} · {statusText(a)}
                  </span>
                </div>
                <Records records={a.records} />
                {a.prepared && a.status !== "done" && <PreparedWork work={a.prepared} />}
              </li>
            ))}
          </ul>
        )}
      </section>
      <div className="grid gap-5 md:grid-cols-2">
        <Section title="Open questions" items={followUp.questions} empty="No open question." />
        <Section title="Risks" items={followUp.risks} empty="No risk was raised." />
      </div>
    </div>
  );
}

const DAY = 86_400_000;

function dueIn(a: Action, today: Date): number | null {
  if (!a.due) return null;
  const d = new Date(`${a.due.slice(0, 10)}T00:00:00`);
  return Number.isNaN(d.getTime()) ? null : Math.round((d.getTime() - today.getTime()) / DAY);
}

function dueText(n: number | null): string {
  if (n == null) return "no date";
  if (n === 0) return "today";
  if (n === 1) return "tomorrow";
  if (n === -1) return "yesterday";
  return n < 0 ? `${-n} days ago` : `in ${n} days`;
}

const PREPARED: Record<Prepared["kind"], string> = {
  message: "Draft message ready",
  document_pack: "Document pack ready",
  write_intent: "Entry prepared (dry run)",
  planner_task: "Planner task proposed",
  answer: "Answered from the records",
};

type Group = { key: string; title: string; items: Action[]; tone?: string };

export function groupActions(items: Action[], today: Date): Group[] {
  const groups: Group[] = [
    { key: "overdue", title: "Overdue", items: [], tone: "text-late" },
    { key: "week", title: "Due this week", items: [] },
    { key: "later", title: "Later", items: [] },
    { key: "undated", title: "No due date", items: [] },
    { key: "done", title: "Done", items: [] },
  ];
  const at = (k: string) => groups.find((g) => g.key === k)!.items;
  for (const a of items) {
    const n = dueIn(a, today);
    if (a.status === "done") at("done").push(a);
    else if (a.status === "overdue" || (n != null && n < 0)) at("overdue").push(a);
    else if (n == null) at("undated").push(a);
    else if (n <= 7) at("week").push(a);
    else at("later").push(a);
  }
  return groups.filter((g) => g.items.length > 0);
}

function ActionRow({ a, today }: { a: Action; today: Date }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const n = dueIn(a, today);
  const late = a.status !== "done" && (a.status === "overdue" || (n != null && n < 0));
  return (
    <li className={cn("text-[13.5px]", a.status === "done" && "text-muted")} data-testid="action-item">
      <div
        className="grid cursor-pointer grid-cols-[18px_minmax(0,1fr)_88px] items-baseline gap-x-4 px-4 py-3 transition-colors duration-150 hover:bg-row-on md:grid-cols-[18px_minmax(0,1fr)_150px_120px_176px]"
        onClick={() => setOpen((o) => !o)}
      >
        {a.status === "done" ? (
          <CheckCircle2 aria-hidden className="mt-0.5 size-4 shrink-0 text-ok" strokeWidth={2} />
        ) : late ? (
          <AlertCircle aria-hidden className="mt-0.5 size-4 shrink-0 text-late" strokeWidth={2} />
        ) : (
          <Circle aria-hidden className="mt-0.5 size-4 shrink-0 text-muted" strokeWidth={1.8} />
        )}
        <span className="min-w-0">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={open ? detailId : undefined}
            className={cn("block text-left text-[14px] font-medium text-fg focus-visible:ring-2 focus-visible:ring-ring/50", a.status === "done" && "text-muted line-through decoration-faint")}
          >
            {a.text}
          </button>
          <span className="mt-0.5 flex flex-wrap items-baseline gap-x-3 text-[12.5px] text-muted">
            {a.meeting_id && (
              <Link className="hover:text-fg hover:underline" href={`/meetings/${encodeURIComponent(a.meeting_id)}`} onClick={(e) => e.stopPropagation()}>
                {a.meeting}
                {a.meeting_start && <span> · {formatDate(a.meeting_start, { short: true })}</span>}
              </Link>
            )}
            <Records records={a.records} />
          </span>
          <span className="mt-0.5 flex flex-wrap gap-x-3 text-[12.5px] text-muted md:hidden" data-testid="action-item-compact">
            <span>{a.owner?.name ?? "No owner"}</span>
            {a.status !== "done" && a.prepared && <span className="text-accent">{PREPARED[a.prepared.kind]}</span>}
          </span>
        </span>
        <span className="truncate text-fg max-md:hidden">{a.owner?.name ?? <span className="text-muted">No owner</span>}</span>
        <span className={cn("num text-[12.5px]", late ? "font-medium text-late" : "text-muted")}>
          {a.status === "done" ? "done" : dueText(n)}
          {a.due && <span className="block text-[12px] text-muted">{formatDate(a.due.slice(0, 10), { short: true })}</span>}
        </span>
        <span className="text-[12.5px] text-muted max-md:hidden">
          {a.status === "done" ? (
            a.evidence?.[0]?.what ?? "marked done"
          ) : a.prepared ? (
            <span className="inline-flex items-center gap-1.5 font-medium text-accent">
              <FileText className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
              {PREPARED[a.prepared.kind]}
            </span>
          ) : (
            "Nothing prepared"
          )}
        </span>
      </div>
      {open && (
        <div id={detailId} className="space-y-2 border-t border-border/60 px-4 py-3 pl-[50px]" data-testid="action-item-detail">
          {a.said_by && (
            <p className="text-[12.5px] text-muted">
              Said by {a.said_by}
              {a.at ? ` at ${a.at} in the recording` : ""}
            </p>
          )}
          {a.prepared && a.status !== "done" && <PreparedWork work={a.prepared} />}
          {a.evidence?.map((e) => (
            <p key={e.what} className="text-[12.5px] text-fg">
              Evidence: {e.what}
            </p>
          ))}
        </div>
      )}
    </li>
  );
}

export function MyActionItemsView({ items, person, today = new Date() }: { items: Action[]; person?: string | null; today?: Date }) {
  const [pickedPerson, setPickedPerson] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const day = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const q = search.trim().toLowerCase();
  const visibleItems = items.filter((a) => {
    if (pickedPerson && (a.owner?.name ?? "No owner") !== pickedPerson) return false;
    if (!q) return true;
    return [a.text, a.meeting ?? "", a.owner?.name ?? ""].join(" ").toLowerCase().includes(q);
  });
  const groups = groupActions(visibleItems, day);
  const count = (k: string) => groups.find((g) => g.key === k)?.items.length ?? 0;
  const open = items.filter((a) => a.status !== "done");
  const openShown = visibleItems.filter((a) => a.status !== "done");
  const byMeeting = new Map<string, { id?: string; n: number }>();
  const byPerson = new Map<string, number>();
  for (const a of open) {
    const m = a.meeting ?? "Unnamed meeting";
    byMeeting.set(m, { id: a.meeting_id, n: (byMeeting.get(m)?.n ?? 0) + 1 });
    const p = a.owner?.name ?? "No owner";
    byPerson.set(p, (byPerson.get(p) ?? 0) + 1);
  }
  const hasFilter = !!pickedPerson || q.length > 0;
  return (
    <div className="space-y-5" data-testid="my-action-items">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">{person ? `Action items for ${person}` : "Action items from your meetings"}</h2>
          {pickedPerson && (
            <button
              type="button"
              onClick={() => setPickedPerson(null)}
              className="inline-flex h-6 items-center gap-1 rounded-md bg-surface-2 px-2 text-[12px] font-medium text-fg transition-colors hover:bg-hover active:scale-[0.98] cursor-pointer"
            >
              <span>{pickedPerson}</span>
              <span className="text-muted" aria-hidden>×</span>
            </button>
          )}
        </div>
        {items.length > 0 && (
          <div className="flex items-center gap-2 max-sm:w-full">
            <label className="relative inline-flex items-center max-sm:flex-1">
              <Search className="pointer-events-none absolute left-2.5 size-3.5 text-muted" strokeWidth={2} aria-hidden />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape" && search) {
                    e.preventDefault();
                    setSearch("");
                  }
                }}
                placeholder="Filter action items"
                aria-label="Filter action items"
                className="h-8 w-52 max-w-full rounded-md border border-border bg-surface pl-8 pr-7 text-[12.5px] text-fg outline-none transition-colors hover:border-border-strong focus:border-border-strong focus-visible:ring-2 focus-visible:ring-ring/40 placeholder:text-muted max-sm:w-full"
              />
              {search && (
                <button
                  type="button"
                  aria-label="Clear action item filter"
                  onClick={() => setSearch("")}
                  className="absolute right-1.5 inline-flex size-5 items-center justify-center rounded-sm text-muted hover:bg-hover hover:text-fg active:scale-[0.95] cursor-pointer"
                >
                  <X className="size-3" aria-hidden />
                </button>
              )}
            </label>
            {hasFilter && (
              <button
                type="button"
                onClick={() => {
                  setPickedPerson(null);
                  setSearch("");
                }}
                className="h-8 rounded-md px-2 text-[12.5px] font-medium text-muted transition-colors hover:bg-hover hover:text-fg active:scale-[0.98] cursor-pointer"
              >
                Reset
              </button>
            )}
          </div>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ["Open", openShown.length, ""],
          ["Overdue", count("overdue"), count("overdue") ? "text-late" : ""],
          ["Due this week", count("week"), ""],
          ["Done", count("done"), "text-ok"],
        ].map(([label, n, tone]) => (
          <div key={label as string} className="rounded-xl border border-border bg-surface p-3.5 tabular-nums">
            <dt className="text-[12px] font-medium text-muted">{label}</dt>
            <dd className={cn("num mt-1 text-[20px] leading-6 font-semibold tracking-[-0.02em] text-fg", tone as string)}>{n}</dd>
          </div>
        ))}
      </dl>

      {items.length === 0 ? (
        <p className="text-[13.5px] text-muted">No action item from a recorded meeting.</p>
      ) : groups.length === 0 ? (
        <div className="flex flex-wrap items-center gap-3 py-6 text-[13.5px] text-muted">
          <span>No action items match these filters.</span>
          <button
            type="button"
            onClick={() => {
              setPickedPerson(null);
              setSearch("");
            }}
            className="inline-flex h-7 cursor-pointer items-center rounded-md border border-border-strong bg-surface px-2.5 text-[12px] font-medium text-fg transition-colors hover:bg-surface-2 active:scale-[0.98]"
          >
            Clear filters
          </button>
        </div>
      ) : (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_280px]">
          <div className="min-w-0 space-y-4">
            <div className="grid grid-cols-[18px_minmax(0,1fr)_88px] gap-x-4 px-4 text-[11.5px] font-medium text-muted md:grid-cols-[18px_minmax(0,1fr)_150px_120px_176px]">
              <span />
              <span>Action item · meeting · records</span>
              <span className="max-md:hidden">Owner</span>
              <span>Due</span>
              <span className="max-md:hidden">Prepared</span>
            </div>
            {groups.map((g) => (
              <section
                key={g.key}
                aria-label={g.title}
                className="overflow-hidden rounded-xl border border-border bg-surface"
              >
                <div className="border-b border-border px-4 py-2.5">
                  <h3 className={cn("flex items-baseline gap-2 text-[13px] font-semibold text-fg", g.tone)}>
                    {g.title}
                    <span className="num font-normal text-muted">{g.items.length}</span>
                  </h3>
                </div>
                <ul className="divide-y divide-border">
                  {g.items.map((a) => (
                    <ActionRow key={a.id} a={a} today={day} />
                  ))}
                </ul>
              </section>
            ))}
          </div>
          <aside className="space-y-4 text-[13.5px]">
            <div className="overflow-hidden rounded-xl border border-border bg-surface">
              <div className="border-b border-border px-4 py-2.5">
                <h3 className="text-[12px] font-semibold text-fg">Open by meeting</h3>
              </div>
              <ul className="divide-y divide-border">
                {[...byMeeting.entries()].map(([m, v]) => (
                  <li key={m} className="flex justify-between gap-3 px-4 py-2 transition-colors duration-150 hover:bg-row-on">
                    {v.id ? (
                      <Link className="truncate font-medium text-fg hover:text-accent hover:underline" href={`/meetings/${encodeURIComponent(v.id)}`}>
                        {m}
                      </Link>
                    ) : (
                      <span className="truncate text-fg">{m}</span>
                    )}
                    <span className="num text-muted">{v.n}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="overflow-hidden rounded-xl border border-border bg-surface">
              <div className="border-b border-border px-4 py-2.5">
                <h3 className="text-[12px] font-semibold text-fg">Open by person</h3>
              </div>
              <ul className="divide-y divide-border p-1.5">
                {[...byPerson.entries()].sort((a, b) => b[1] - a[1]).map(([p, n]) => {
                  const active = pickedPerson === p;
                  return (
                    <li key={p} className="py-0.5 first:pt-0 last:pb-0">
                      <button
                        type="button"
                        aria-pressed={active}
                        onClick={() => setPickedPerson((cur) => (cur === p ? null : p))}
                        className={cn(
                          "flex w-full justify-between gap-3 rounded-md px-2.5 py-1.5 text-left transition-colors active:scale-[0.98] cursor-pointer",
                          active ? "bg-fg font-medium text-bg" : "text-fg hover:bg-row-on",
                        )}
                      >
                        <span className="truncate">{p}</span>
                        <span className={cn("num", active ? "opacity-80" : "text-muted")}>{n}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

function useLoad<T>(load: (signal: AbortSignal) => Promise<T>, key: string) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    load(ctl.signal).then(setData, (e) => { if (!ctl.signal.aborted) setError(e); });
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, round]);
  return { data, error, retry };
}

export function MeetingFollowUpPanel({ meetingId }: { meetingId: string }) {
  const { data, error, retry } = useLoad((s) => getMeetingFollowUp(meetingId, s), meetingId);
  if (error instanceof ApiError && error.status === 404) {
    return (
      <div className="max-w-lg text-[13.5px]" data-testid="follow-up-missing">
        <p className="text-muted">No follow-up here: this meeting isn&apos;t one you attended, or it wasn&apos;t recorded.</p>
        <Link href="/meetings?view=follow-ups" className="mt-3 inline-flex h-8 items-center rounded-md bg-fg px-3 text-[12.5px] font-medium text-bg hover:opacity-90 active:scale-[0.98]">
          Your meeting follow-ups
        </Link>
      </div>
    );
  }
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!data) return <Pending label="Reading the meeting's follow-up" />;
  return <MeetingFollowUpView followUp={data.follow_up as unknown as FollowUp} />;
}

export function MyActionItemsPanel({ person }: { person?: string | null }) {
  const { data, error, retry } = useLoad((s) => getMyActionItems(person, s), person ?? "");
  if (error instanceof ApiError && error.status === 404) {
    return <p className="text-[13.5px] text-muted">No meeting follow-ups are kept on this server.</p>;
  }
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!data) return <Pending label="Reading your action items" />;
  return (
    <>
      <StaleNote since={data.stale_since} readAt={data.read_at} className="mb-3" />
      <MyActionItemsView items={data.items as unknown as Action[]} person={person} />
    </>
  );
}
