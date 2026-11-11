"use client";

// Meetings: a recorded meeting's follow-up and "My action items". Presentational views plus
// self-loading panels, so the Meetings home can mount either one. Nothing
// here sends or writes: a drafted message goes out only through CardActions' preview and
// your confirm, a write intent through the write desk's second-person approval.
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { Pending } from "@/components/ui/skeleton";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getMeetingFollowUp, getMyActionItems } from "@/lib/api";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import { cn } from "@/lib/cn";
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
  return a.due ? `${STATUS[a.status]}, due ${a.due}` : STATUS[a.status];
}

function Cite({ item }: { item: Item }) {
  return item.at ? <span className="text-[11px] text-muted" title={item.citations.join("\n")}>at {item.at}</span> : null;
}

function Records({ records }: { records?: Linked[] }) {
  const linked = (records ?? []).filter((r) => r.status === "linked" && r.object_id);
  const leads = (records ?? []).filter((r) => r.status === "ambiguous");
  if (!linked.length && !leads.length) return null;
  return (
    <span className="flex flex-wrap gap-2 text-[12px]">
      {linked.map((r) => (
        <Link key={r.object_id} href={`/object/${encodeURIComponent(r.type ?? "")}/${encodeURIComponent(r.object_id ?? "")}`} className="font-medium text-accent hover:underline" onClick={(e) => e.stopPropagation()}>
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
      <div className="space-y-1" data-testid="prepared-message">
        <p className="text-[12px] text-muted">Draft {work.card.draft?.to ? `to ${work.card.draft.to.address}` : "(no recipient yet)"}: {work.card.draft?.subject}</p>
        {work.missing?.map((m) => <p key={m} className="text-[12px] text-warn">{m}</p>)}
        <CardActions card={{ id: work.card.id, subject: work.card.subject, actions: work.offers }} state={state} onDone={setState} />
      </div>
    );
  }
  if (work.kind === "document_pack" && work.object_id) {
    const type = work.object_id.split(":")[0];
    return <Link className="text-[12px] font-medium hover:underline" href={`/pack/${encodeURIComponent(type)}/${encodeURIComponent(work.object_id)}`} data-testid="prepared-pack">Open the document pack and its drafted reply</Link>;
  }
  if (work.kind === "write_intent") {
    return (
      <div className="text-[12px]" data-testid="prepared-write">
        <p>{work.label}: prepared for the write desk (dry run, approved by a second person).</p>
        {work.needs?.length ? <p className="text-muted">Still needs: {work.needs.join(", ")}</p> : null}
      </div>
    );
  }
  if (work.kind === "answer") {
    return (
      <div className="text-[12px]" data-testid="prepared-answer">
        {work.answers?.length ? work.answers.map((a) => (
          <p key={a.object_id}><span className="font-medium">{a.label}</span>: {a.values.map((v) => `${v.property.replace(/_/g, " ")} ${v.value}`).join(", ")}</p>
        )) : <p className="text-muted">{work.note ?? "No value you may read answers it yet."}</p>}
      </div>
    );
  }
  if (work.kind === "planner_task" && work.task) {
    return <p className="text-[12px]" data-testid="prepared-planner">Proposed Planner task: {work.task.title}{work.task.due ? `, due ${work.task.due}` : ""} (not created: Planner writes wait for approval).</p>;
  }
  return null;
}

function Section({ title, items, empty }: { title: string; items: Item[]; empty: string }) {
  return (
    <section className="space-y-1">
      <h2 className="text-[14px] font-semibold">{title}</h2>
      {items.length === 0 ? <p className="text-[13px] text-muted">{empty}</p> : (
        <ul className="space-y-1 text-[13px]">
          {items.map((i) => <li key={i.id} className="flex flex-wrap items-baseline gap-2">{i.text} <Cite item={i} /> <Records records={i.records} /></li>)}
        </ul>
      )}
    </section>
  );
}

export function MeetingFollowUpView({ followUp }: { followUp: FollowUp }) {
  const { meeting, status } = followUp;
  return (
    <div className="space-y-5" data-testid="meeting-follow-up">
      <header className="space-y-1">
        <p className="text-[12px] uppercase tracking-wide text-muted">Meeting follow-up</p>
        <h1 className="text-[20px] font-semibold">{meeting.title}</h1>
        <p className="text-[13px] text-muted">{meeting.start.slice(0, 10)}{meeting.organizer?.name ? `, organised by ${meeting.organizer.name}` : ""}</p>
        {followUp.summary && <p className="text-[13px]">{followUp.summary}</p>}
        <p className="text-[13px]" data-testid="follow-up-status">{status.done} of {status.actions} action items done{status.overdue ? `, ${status.overdue} overdue` : ""}</p>
      </header>
      <Section title="Decisions" items={followUp.decisions} empty="No decision was recorded." />
      <section className="space-y-2">
        <h2 className="text-[14px] font-semibold">Action items</h2>
        {followUp.actions.length === 0 ? <p className="text-[13px] text-muted">No action item was recorded.</p> : (
          <ul className="divide-y divide-border">
            {followUp.actions.map((a) => (
              <li key={a.id} className="space-y-1 py-2 text-[13px]" data-testid="follow-up-action">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span>{a.text} <Cite item={a} /></span>
                  <span className={a.status === "overdue" ? "text-bad" : "text-muted"}>{a.owner?.name ?? "No owner"} · {statusText(a)}</span>
                </div>
                <Records records={a.records} />
                {a.prepared && a.status !== "done" && <PreparedWork work={a.prepared} />}
              </li>
            ))}
          </ul>
        )}
      </section>
      <Section title="Open questions" items={followUp.questions} empty="No open question." />
      <Section title="Risks" items={followUp.risks} empty="No risk was raised." />
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

/** Action items grouped by when they are due, overdue first; done last. */
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
  const n = dueIn(a, today);
  const late = a.status !== "done" && (a.status === "overdue" || (n != null && n < 0));
  return (
    <li className={cn("border-b border-border text-[13px]", a.status === "done" && "text-muted")} data-testid="action-item">
      <div className="grid cursor-pointer grid-cols-[18px_minmax(0,1fr)_150px_120px_170px] items-baseline gap-x-4 py-2 hover:bg-hover" onClick={() => setOpen((o) => !o)}>
        <span aria-hidden className={cn("mt-1 size-3 rounded-full border-[1.5px]", a.status === "done" ? "border-ok bg-ok" : late ? "border-late" : "border-border-strong")} />
        <span className="min-w-0">
          <span className={cn("font-medium", a.status === "done" && "line-through decoration-faint")}>{a.text}</span>
          <span className="mt-0.5 flex flex-wrap items-baseline gap-x-3 text-[12px] text-muted">
            {a.meeting_id && (
              <Link className="hover:underline" href={`/meetings/${encodeURIComponent(a.meeting_id)}`} onClick={(e) => e.stopPropagation()}>
                {a.meeting}
                {a.meeting_start && <span className="num"> · {a.meeting_start.slice(5, 10).replace("-", "/")}</span>}
              </Link>
            )}
            <Records records={a.records} />
          </span>
        </span>
        <span className="truncate">{a.owner?.name ?? <span className="text-faint">No owner</span>}</span>
        <span className={cn("num text-[12px]", late ? "text-late" : "text-muted")}>
          {a.status === "done" ? "done" : dueText(n)}
          {a.due && <span className="block text-faint">{a.due.slice(0, 10)}</span>}
        </span>
        <span className="text-[12px] text-muted">
          {a.status === "done" ? (a.evidence?.[0]?.what ?? "marked done") : a.prepared ? <span className="text-accent">{PREPARED[a.prepared.kind]}</span> : "Nothing prepared"}
        </span>
      </div>
      {open && (
        <div className="space-y-2 pb-3 pl-[34px]" data-testid="action-item-detail">
          {a.said_by && (
            <p className="text-[12px] text-muted">
              Said by {a.said_by}
              {a.at ? ` at ${a.at} in the recording` : ""}
            </p>
          )}
          {a.prepared && a.status !== "done" && <PreparedWork work={a.prepared} />}
          {a.evidence?.map((e) => (
            <p key={e.what} className="text-[12px]">
              Evidence: {e.what}
            </p>
          ))}
        </div>
      )}
    </li>
  );
}

export function MyActionItemsView({ items, person, today = new Date() }: { items: Action[]; person?: string | null; today?: Date }) {
  const day = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const groups = groupActions(items, day);
  const count = (k: string) => groups.find((g) => g.key === k)?.items.length ?? 0;
  const open = items.filter((a) => a.status !== "done");
  const byMeeting = new Map<string, { id?: string; n: number }>();
  const byPerson = new Map<string, number>();
  for (const a of open) {
    const m = a.meeting ?? "Unnamed meeting";
    byMeeting.set(m, { id: a.meeting_id, n: (byMeeting.get(m)?.n ?? 0) + 1 });
    const p = a.owner?.name ?? "No owner";
    byPerson.set(p, (byPerson.get(p) ?? 0) + 1);
  }
  return (
    <div className="space-y-4" data-testid="my-action-items">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-border pb-3">
        <h2 className="text-[15px] font-semibold">{person ? `Action items for ${person}` : "Action items from your meetings"}</h2>
        <dl className="flex gap-7 text-[12px]">
          {[
            ["Open", open.length, ""],
            ["Overdue", count("overdue"), count("overdue") ? "text-late" : ""],
            ["Due this week", count("week"), ""],
            ["Done", count("done"), "text-ok"],
          ].map(([label, n, tone]) => (
            <div key={label as string}>
              <dt className="text-muted">{label}</dt>
              <dd className={cn("num text-[15px] font-semibold", tone as string)}>{n}</dd>
            </div>
          ))}
        </dl>
      </div>
      {items.length === 0 ? (
        <p className="text-[13px] text-muted">No action item from a recorded meeting.</p>
      ) : (
        <div className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_260px]">
          <div className="min-w-0 space-y-5">
            <div className="grid grid-cols-[18px_minmax(0,1fr)_150px_120px_170px] gap-x-4 border-b border-border pb-1 text-[11px] font-medium tracking-[0.02em] text-faint uppercase">
              <span />
              <span>Action item · meeting · records</span>
              <span>Owner</span>
              <span>Due</span>
              <span>Prepared</span>
            </div>
            {groups.map((g) => (
              <section key={g.key} aria-label={g.title}>
                <h3 className={cn("flex items-baseline gap-2 text-[12px] font-semibold", g.tone)}>
                  {g.title}
                  <span className="num font-normal text-muted">{g.items.length}</span>
                </h3>
                <ul>
                  {g.items.map((a) => (
                    <ActionRow key={a.id} a={a} today={day} />
                  ))}
                </ul>
              </section>
            ))}
          </div>
          <aside className="space-y-6 text-[13px]">
            <div>
              <h3 className="mb-1.5 text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">Open by meeting</h3>
              <ul className="divide-y divide-border">
                {[...byMeeting.entries()].map(([m, v]) => (
                  <li key={m} className="flex justify-between gap-3 py-1.5">
                    {v.id ? (
                      <Link className="truncate hover:underline" href={`/meetings/${encodeURIComponent(v.id)}`}>
                        {m}
                      </Link>
                    ) : (
                      <span className="truncate">{m}</span>
                    )}
                    <span className="num text-muted">{v.n}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3 className="mb-1.5 text-[12px] font-semibold tracking-[0.02em] text-muted uppercase">Open by person</h3>
              <ul className="divide-y divide-border">
                {[...byPerson.entries()].sort((a, b) => b[1] - a[1]).map(([p, n]) => (
                  <li key={p} className="flex justify-between gap-3 py-1.5">
                    <span className="truncate">{p}</span>
                    <span className="num text-muted">{n}</span>
                  </li>
                ))}
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

/** The follow-up of one meeting, loaded as the signed-in reader. */
export function MeetingFollowUpPanel({ meetingId }: { meetingId: string }) {
  const { data, error, retry } = useLoad((s) => getMeetingFollowUp(meetingId, s), meetingId);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!data) return <Pending label="Reading the meeting's follow-up" />;
  return <MeetingFollowUpView followUp={data.follow_up as unknown as FollowUp} />;
}

/** My action items (or ``person``'s, for meetings I organised). */
export function MyActionItemsPanel({ person }: { person?: string | null }) {
  const { data, error, retry } = useLoad((s) => getMyActionItems(person, s), person ?? "");
  // not found: this server keeps no meeting follow-ups, which is an empty list, not a failure
  if (error instanceof ApiError && error.status === 404) return <p className="text-[13px] text-muted">No meeting follow-ups are kept on this server.</p>;
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!data) return <Pending label="Reading your action items" />;
  return (
    <>
      <StaleNote since={data.stale_since} readAt={data.read_at} className="mb-2" />
      <MyActionItemsView items={data.items as unknown as Action[]} person={person} />
    </>
  );
}
