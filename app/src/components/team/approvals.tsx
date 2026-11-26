"use client";

import { AlertTriangle, CircleAlert, Clock3 } from "lucide-react";
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

const CHANNEL: Record<string, string> = { mail: "Email", email: "Email", teams: "Teams", mission_control: "Mission Control", portal: "Portal" };

const waitingOn = (who: string) => (who === "not recorded" ? "an approver not on record" : who);

const ageTone = (days: number) =>
  days >= 5 ? "font-semibold text-late" : days >= 3 ? "font-medium text-risk" : "text-fg";

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
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="approvals-summary">
      <div className="rounded-xl border border-border bg-surface p-4 tabular-nums">
        <dt className="text-[12px] font-medium text-muted">Waiting</dt>
        <dd className="mt-1 text-[22px] leading-7 font-semibold tracking-[-0.02em] text-fg">{total}</dd>
        <dd className="mt-0.5 text-[12.5px] text-muted">
          on {plural(people, "person", "people")}{groups.some((g) => g.waiting_on === "not recorded") ? " and an unnamed approver" : ""}
        </dd>
      </div>
      <div className="rounded-xl border border-border bg-surface p-4 tabular-nums">
        <dt className="text-[12px] font-medium text-muted">Oldest</dt>
        <dd className={cn("mt-1 text-[22px] leading-7 font-semibold tracking-[-0.02em]", oldest && oldest.days >= 5 ? "text-late" : "text-fg")}>
          {oldest ? `${oldest.days} d` : "None"}
        </dd>
        {oldest && <dd className="mt-0.5 truncate text-[12.5px] text-muted" title={oldest.summary ?? undefined}>waiting on {waitingOn(oldest.who)}</dd>}
      </div>
      <div className="rounded-xl border border-border bg-surface p-4 tabular-nums">
        <dt className="text-[12px] font-medium text-muted">5 business days or more</dt>
        <dd className={cn("mt-1 text-[22px] leading-7 font-semibold tracking-[-0.02em]", overdue > 0 ? "text-late" : "text-fg")}>{overdue}</dd>
        <dd className="mt-0.5 text-[12.5px] text-muted">{plural(drafts, "nudge")} drafted, none sent</dd>
      </div>
      <div className="rounded-xl border border-border bg-surface p-4">
        <dt className="text-[12px] font-medium text-muted">Where asked</dt>
        <dd className="mt-1.5 space-y-1 text-[12.5px]">
          {channels.length === 0 ? (
            <span className="text-muted">Nothing waiting</span>
          ) : (
            channels.map(([c, n]) => (
              <span key={c} className="flex justify-between gap-3">
                <span className="text-fg">{c}</span>
                <span className="num text-muted">{n}</span>
              </span>
            ))
          )}
        </dd>
      </div>
    </dl>
  );
}

const COLS = "lg:grid-cols-[72px_minmax(0,1fr)_150px_128px_minmax(0,220px)]";

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
    <div className="space-y-4" data-testid="approvals-queue">
      <StoredAge at={at} />
      <StaleNote since={queue.stale_since} readAt={queue.read_at} />
      {queue.total > 0 && <Summary groups={all} total={queue.total} />}
      {Object.keys(queue.by_aging ?? {}).length > 0 && (
        <div className="flex flex-wrap items-stretch gap-5 border-b border-border" role="tablist" aria-label="Waiting for">
          {[["all", queue.total] as const, ...Object.entries(queue.by_aging ?? {})].map(([bucket, n]) => {
            const on = (bucket === "all" ? null : bucket) === aging;
            return (
              <button
                key={bucket}
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => setAging(bucket === "all" ? null : bucket)}
                className={cn(
                  "relative flex h-9 items-center gap-1.5 text-[13px] whitespace-nowrap transition-colors duration-150 cursor-pointer",
                  on
                    ? "font-medium text-fg after:absolute after:inset-x-0 after:bottom-0 after:h-[2px] after:bg-fg"
                    : "text-muted hover:text-fg",
                )}
              >
                {bucket === "all" ? "All" : bucket.replace(/^./, (c) => c.toUpperCase())}
                <span className="num text-[12px] font-normal text-muted">{n}</span>
              </button>
            );
          })}
        </div>
      )}
      {groups.length === 0 && (
        <EmptyState title="No approvals waiting." line="Mail asks no approver has answered and Mission Control gates show here, oldest first, with a drafted nudge for each." />
      )}
      {groups.length > 0 && (
        <div className={cn("hidden items-end gap-x-4 px-4 text-[11.5px] font-medium text-muted lg:grid", COLS)} aria-hidden>
          <span>Waiting</span>
          <span>What is asked</span>
          <span>Asked by</span>
          <span>Where</span>
          <span>Nudge</span>
        </div>
      )}
      {groups.map((g) => (
        <section
          key={g.waiting_on}
          aria-label={`Waiting on ${waitingOn(g.waiting_on)}`}
          className="overflow-hidden rounded-xl border border-border bg-surface"
        >
          <div className="border-b border-border px-4 py-2.5">
            <h3 className="text-[13.5px] font-semibold text-fg">
              Waiting on {waitingOn(g.waiting_on)}
              <span className="font-normal text-muted">
                {" "}
                · {plural(g.count, "item")} ·{" "}
                <span className={cn(g.oldest_days >= 5 && "font-medium text-late")}>oldest {plural(g.oldest_days, "business day")}</span>
              </span>
            </h3>
          </div>
          <ul className="divide-y divide-border">
            {g.items.map((item) => {
              const where = CHANNEL[item.channel] ?? item.channel.replace(/_/g, " ");
              const family = item.family ? item.family.replace(/_/g, " ") : null;
              const also = (item.waiting_on ?? []).filter((p) => p !== g.waiting_on);
              return (
                <li
                  key={item.id}
                  className={cn(
                    "grid grid-cols-[60px_minmax(0,1fr)] gap-x-4 gap-y-1.5 px-4 py-3 text-[13.5px] transition-colors duration-150 hover:bg-row-on lg:items-start",
                    COLS,
                  )}
                  data-testid="approval-item"
                >
                  <span
                    className={cn("num inline-flex items-center gap-1 pt-0.5 text-[13px] tabular-nums", ageTone(item.days))}
                    title={plural(item.days, "business day")}
                  >
                    {item.days >= 5 ? (
                      <CircleAlert className="size-3.5 shrink-0 text-late" aria-hidden />
                    ) : item.days >= 3 ? (
                      <AlertTriangle className="size-3.5 shrink-0 text-risk" aria-hidden />
                    ) : (
                      <Clock3 className="size-3.5 shrink-0 text-faint" aria-hidden />
                    )}
                    {item.days} d<span className="sr-only">: {plural(item.days, "business day")}</span>
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium break-words text-fg">{item.summary || "Approval waiting"}</p>
                    {(() => {
                      const extra = [also.length > 0 ? `also waits on ${also.join(", ")}` : null, (item.citations ?? []).length > 0 ? plural((item.citations ?? []).length, "source") : null];
                      const full = [plural(item.days, "business day"), where, family, item.asked_by ? `asked by ${item.asked_by}` : null, ...extra].filter(Boolean).join(" · ");
                      const short = [family, ...extra].filter(Boolean).join(" · ");
                      const link = item.object_id ? (
                        <Link className="font-medium text-fg underline decoration-border-strong underline-offset-2 hover:decoration-fg" href={objectHref(item.object_id.split(":")[0], item.object_id)}>
                          Open the message
                        </Link>
                      ) : null;
                      return (
                        <p className="mt-0.5 text-[12.5px] text-muted">
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
                  <span className="col-start-2 hidden truncate text-[13px] text-fg lg:col-start-auto lg:block lg:pt-0.5">
                    {item.asked_by ?? <span className="text-muted">Not on record</span>}
                  </span>
                  <span className="hidden text-[13px] text-muted lg:block lg:pt-0.5">{where}</span>
                  <div className="col-start-2 min-w-0 space-y-1.5 lg:col-start-auto">
                    {item.nudge.body ? (
                      <details className="group text-[12.5px]">
                        <summary className="cursor-pointer list-none font-medium text-muted hover:text-fg [&::-webkit-details-marker]:hidden">Draft nudge, not sent</summary>
                        <p className="mt-1.5 border-l border-border-strong pl-3 whitespace-pre-wrap text-[13px] leading-5 text-fg">
                          {item.nudge.body}
                        </p>
                      </details>
                    ) : (
                      <p className="text-[12.5px] text-muted">No draft nudge.</p>
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
