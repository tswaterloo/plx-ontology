"use client";

// Meetings: a recorded meeting's follow-up and "My action items". Presentational views plus
// self-loading panels, so the Meetings home can mount either one. Nothing
// here sends or writes: a drafted message goes out only through CardActions' preview and
// your confirm, a write intent through the write desk's second-person approval.
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getMeetingFollowUp, getMyActionItems } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
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
        <Link key={r.object_id} href={`/object/${encodeURIComponent(r.type ?? "")}/${encodeURIComponent(r.object_id ?? "")}`} className="rounded bg-hover px-1.5 hover:underline">
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

export function MyActionItemsView({ items, person }: { items: Action[]; person?: string | null }) {
  return (
    <div className="space-y-3" data-testid="my-action-items">
      <h1 className="text-[20px] font-semibold">{person ? `Action items for ${person}` : "My action items"}</h1>
      {items.length === 0 ? <p className="text-[13px] text-muted">No action item from a recorded meeting.</p> : (
        <ul className="divide-y divide-border">
          {items.map((a) => (
            <li key={a.id} className="flex flex-wrap items-baseline justify-between gap-2 py-2 text-[13px]">
              <span>{a.text}{" "}
                {a.meeting_id && <Link className="text-muted hover:underline" href={`/meetings/${encodeURIComponent(a.meeting_id)}`}>{a.meeting}</Link>}
              </span>
              <span className={a.status === "overdue" ? "text-bad" : "text-muted"}>{statusText(a)}</span>
            </li>
          ))}
        </ul>
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
  if (!data) return <Skeleton className="h-40 w-full" />;
  return <MeetingFollowUpView followUp={data.follow_up as unknown as FollowUp} />;
}

/** My action items (or ``person``'s, for meetings I organised). */
export function MyActionItemsPanel({ person }: { person?: string | null }) {
  const { data, error, retry } = useLoad((s) => getMyActionItems(person, s), person ?? "");
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!data) return <Skeleton className="h-24 w-full" />;
  return <MyActionItemsView items={data.items as unknown as Action[]} person={person} />;
}
