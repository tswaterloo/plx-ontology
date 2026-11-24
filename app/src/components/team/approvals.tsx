"use client";

import { StoredAge } from "@/components/ui/stored-age";
import Link from "next/link";
import { useState } from "react";
import { CardActions } from "@/components/card-actions";
import { EmptyState } from "@/components/ui/empty-state";
import { Pending } from "@/components/ui/skeleton";
import { cn } from "@/lib/cn";
import { objectHref, plural } from "@/lib/format";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getApprovals } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { noticeKind, plainError } from "@/lib/errors";
import type { ActionResult, ApprovalsQueue } from "@/lib/types";

type ApprovalGroup = NonNullable<ApprovalsQueue["groups"]>[number];

// Team, Approvals: mail asks no approver answered and Mission Control gates, by who they
// wait on, oldest first. A nudge is a draft: it goes out only through the card's preview
// and your confirm.
/** Where an ask was made, as the reader names it. */
const CHANNEL: Record<string, string> = { mail: "Email", email: "Email", teams: "Teams", mission_control: "Mission Control", portal: "Portal" };

/** The server groups an ask with no named approver under "not recorded". */
const waitingOn = (who: string) => (who === "not recorded" ? "an approver not on record" : who);

/** Age tone: five business days and over is late, three and over is at risk. */
const ageTone = (days: number) => (days >= 5 ? "text-late font-semibold" : days >= 3 ? "text-[color:var(--risk)] font-medium" : "text-fg");

/** The desk at a glance: how much waits, the oldest ask and on whom, how many are over a week, where they were asked. */
function Summary({ groups, total }: { groups: ApprovalGroup[]; total: number }) {
  const items = groups.flatMap((g) => g.items.map((i) => ({ ...i, who: g.waiting_on })));
  const oldest = items.reduce<(typeof items)[number] | null>((a, b) => (!a || b.days > a.days ? b : a), null);
  const overdue = items.filter((i) => i.days >= 5).length;
  const drafts = items.filter((i) => i.nudge.body).length;
  const channels = Object.entries(
    items.reduce<Record<string, number>>((m, i) => ((m[CHANNEL[i.channel] ?? i.channel.replace(/_/g, " ")] = (m[CHANNEL[i.channel] ?? i.channel.replace(/_/g, " ")] ?? 0) + 1), m), {}),
  ).sort((a, b) => b[1] - a[1]);
  const people = groups.filter((g) => g.waiting_on !== "not recorded").length;
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-b border-border pb-3 sm:grid-cols-4" data-testid="approvals-summary">
      <div>
        <dt className="text-[12px] text-muted">Waiting</dt>
        <dd className="text-[20px] leading-7 font-semibold tabular-nums">{total}</dd>
        <dd className="text-[12px] text-muted">on {plural(people, "person", "people")}{groups.some((g) => g.waiting_on === "not recorded") ? " and an unnamed approver" : ""}</dd>
      </div>
      <div>
        <dt className="text-[12px] text-muted">Oldest</dt>
        <dd className={cn("text-[20px] leading-7 font-semibold tabular-nums", oldest && oldest.days >= 5 && "text-late")}>{oldest ? `${oldest.days} d` : "None"}</dd>
        {oldest && <dd className="truncate text-[12px] text-muted" title={oldest.summary ?? undefined}>waiting on {waitingOn(oldest.who)}</dd>}
      </div>
      <div>
        <dt className="text-[12px] text-muted">5 business days or more</dt>
        <dd className={cn("text-[20px] leading-7 font-semibold tabular-nums", overdue > 0 && "text-late")}>{overdue}</dd>
        <dd className="text-[12px] text-muted">{plural(drafts, "nudge")} drafted, none sent</dd>
      </div>
      <div>
        <dt className="text-[12px] text-muted">Where asked</dt>
        <dd className="mt-1 max-w-[220px] space-y-0.5 text-[12px]">
          {channels.length === 0 ? <span className="text-muted">Nothing waiting</span> : channels.map(([c, n]) => (
            <span key={c} className="flex justify-between gap-3">
              <span>{c}</span>
              <span className="num text-muted">{n}</span>
            </span>
          ))}
        </dd>
      </div>
    </dl>
  );
}

const COLS = "lg:grid-cols-[64px_minmax(0,1fr)_150px_128px_minmax(0,220px)]";

export function TeamApprovals() {
  const [aging, setAging] = useState<string | null>(null);
  const { data: queue, error, retry, at } = useRead<ApprovalsQueue>("/api/approvals", getApprovals);
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!queue) return <Pending label="Reading the approvals" />;
  const all = queue.groups ?? [];
  const groups = all
    .map((g) => ({ ...g, items: aging ? g.items.filter((i) => i.aging === aging) : g.items }))
    .filter((g) => g.items.length > 0);
  return (
    <div className="space-y-5" data-testid="approvals-queue">
      <StoredAge at={at} />
      <StaleNote since={queue.stale_since} readAt={queue.read_at} />
      {queue.total > 0 && <Summary groups={all} total={queue.total} />}
      {Object.keys(queue.by_aging ?? {}).length > 0 && (
        <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Waiting for">
          {[["all", queue.total] as const, ...Object.entries(queue.by_aging ?? {})].map(([bucket, n]) => {
            const on = (bucket === "all" ? null : bucket) === aging;
            return (
              <button key={bucket} type="button" role="tab" aria-selected={on} onClick={() => setAging(bucket === "all" ? null : bucket)} className={cn("inline-flex h-7 items-center gap-1.5 rounded-[3px] px-3 text-[12px] cursor-pointer", on ? "bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover")}>
                {bucket === "all" ? "All" : bucket.replace(/^./, (c) => c.toUpperCase())}
                <span className="num text-faint">{n}</span>
              </button>
            );
          })}
        </div>
      )}
      {groups.length === 0 && <EmptyState title="No approvals waiting." line="Mail asks no approver has answered and Mission Control gates show here, oldest first, with a drafted nudge for each." />}
      {groups.length > 0 && (
        <div className={cn("hidden items-end gap-x-4 px-3 text-[11px] leading-4 text-faint lg:grid", COLS)} aria-hidden>
          <span>Waiting</span>
          <span>What is asked</span>
          <span>Asked by</span>
          <span>Where</span>
          <span>Nudge</span>
        </div>
      )}
      {groups.map((g) => (
        <section key={g.waiting_on} aria-label={`Waiting on ${waitingOn(g.waiting_on)}`}>
          <h3 className="border-b border-border px-3 pb-1.5 text-[13px] font-semibold">
            Waiting on {waitingOn(g.waiting_on)}
            <span className="font-normal text-muted">
              {" "}
              · {plural(g.count, "item")} ·{" "}
              <span className={cn(g.oldest_days >= 5 && "text-late")}>oldest {plural(g.oldest_days, "business day")}</span>
            </span>
          </h3>
          <ul className="divide-y divide-border">
            {g.items.map((item) => {
              const where = CHANNEL[item.channel] ?? item.channel.replace(/_/g, " ");
              const family = item.family ? item.family.replace(/_/g, " ") : null;
              const also = (item.waiting_on ?? []).filter((p) => p !== g.waiting_on);
              return (
                <li key={item.id} className={cn("grid grid-cols-[52px_minmax(0,1fr)] gap-x-4 gap-y-1 px-3 py-2 text-[13px] lg:items-start", COLS, item.days >= 5 && "shadow-[inset_2px_0_0_var(--late)]")} data-testid="approval-item">
                  <span className={cn("num pt-px text-[13px] tabular-nums", ageTone(item.days))} title={plural(item.days, "business day")}>
                    {item.days} d<span className="sr-only">: {plural(item.days, "business day")}</span>
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium break-words">{item.summary || "Approval waiting"}</p>
                    {(() => {
                      const extra = [also.length > 0 ? `also waits on ${also.join(", ")}` : null, (item.citations ?? []).length > 0 ? plural((item.citations ?? []).length, "source") : null];
                      // the phone reads every fact here; a desktop has age, asker and channel in their own columns
                      const full = [plural(item.days, "business day"), where, family, item.asked_by ? `asked by ${item.asked_by}` : null, ...extra].filter(Boolean).join(" · ");
                      const short = [family, ...extra].filter(Boolean).join(" · ");
                      const link = item.object_id ? (
                        <Link className="font-medium text-fg hover:underline" href={objectHref(item.object_id.split(":")[0], item.object_id)}>
                          Open the message
                        </Link>
                      ) : null;
                      return (
                        <p className="mt-0.5 text-[12px] text-muted">
                          <span data-testid="approval-line" className="lg:hidden">
                            {full}
                            {link && " · "}
                          </span>
                          {short && (
                            <span className="hidden lg:inline">
                              {short}
                              {link && " · "}
                            </span>
                          )}
                          {link}
                        </p>
                      );
                    })()}
                  </div>
                  <span className="col-start-2 hidden truncate text-[12px] lg:col-start-auto lg:block lg:pt-px lg:text-[13px]">{item.asked_by ?? <span className="text-faint">Not on record</span>}</span>
                  <span className="hidden text-[12px] lg:block lg:pt-px lg:text-[13px]">{where}</span>
                  <div className="col-start-2 min-w-0 space-y-1 lg:col-start-auto">
                    {item.nudge.body ? (
                      <details className="group text-[12px]">
                        <summary className="cursor-pointer list-none text-muted hover:text-fg [&::-webkit-details-marker]:hidden">Draft nudge, not sent</summary>
                        <p className="mt-1 border-l-2 border-border pl-2.5 whitespace-pre-wrap text-[13px] leading-5">{item.nudge.body}</p>
                      </details>
                    ) : (
                      <p className="text-[12px] text-faint">No draft nudge.</p>
                    )}
                    {item.card_id && (
                      <CardActions
                        card={{ id: item.card_id, subject: { label: item.summary ?? "Approval waiting" }, actions: item.offers }}
                        state={states[item.card_id] ?? null}
                        onDone={(r) => setStates((s) => ({ ...s, [item.card_id as string]: r }))}
                      />
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
