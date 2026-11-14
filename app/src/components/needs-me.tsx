"use client";

// What needs the owner: the decisions, approvals and blocked items waiting on them across
// the Workbench, read from the reader-scoped endpoints that already hold them (the
// autopilot review queue, prepared write entries, the approvals queue and the reliability
// alarms). Shown only when the server says the reader is the owner; a source that can't be
// read is named, never counted as zero.

import Link from "next/link";
import { useEffect, useState } from "react";
import { getApprovals, getAutopilot, getMe, getOpsAlarms, getWrites, type OpsAlarms } from "@/lib/api";
import { systemLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import { ago, formatDateTime, objectHref, opsHeadline, plural } from "@/lib/format";
import type { ApprovalsQueue, AutopilotArea, Me, WriteIntentView } from "@/lib/types";

export type NeedKind = "decide" | "approve" | "blocked";
export interface Need {
  key: string;
  kind: NeedKind;
  title: string;
  detail: string;
  href: string | null;
  /** for ordering inside a kind: the sooner due or the older, the earlier */
  rank: number;
}
export interface NeedsSources {
  me: Me | null;
  autopilot: AutopilotArea | null;
  writes: WriteIntentView[] | null;
  approvals: ApprovalsQueue | null;
  alarms: OpsAlarms | null;
}

const KIND_WORD: Record<NeedKind, string> = { decide: "Decide", approve: "Approve", blocked: "Blocked" };
/** An approval waiting this many days on someone else counts as blocked. */
export const STUCK_DAYS = 3;

function stamp(iso: string | null | undefined): number {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? Number.MAX_SAFE_INTEGER : t;
}

function isMe(me: Me | null, who: string): boolean {
  if (!me) return false;
  const w = who.trim().toLowerCase();
  return [me.display_name, me.email].some((v) => !!v && v.trim().toLowerCase() === w);
}

function writeHref(v: WriteIntentView): string | null {
  const id = (v.intent as { record_id?: string | null }).record_id ?? "";
  const type = /^([A-Za-z]+):/.exec(id)?.[1];
  return type ? objectHref(type, id) : null;
}

/** Every item waiting on the owner, by kind; the sources that couldn't be read are listed in `unread`. */
export function needsOf(s: NeedsSources, now = new Date()): { needs: Need[]; unread: string[] } {
  const needs: Need[] = [];
  const unread: string[] = [];
  if (s.autopilot) {
    for (const i of s.autopilot.items ?? []) {
      if (!i.mine || i.state !== "waiting") continue;
      needs.push({ key: `ap:${i.item_id}`, kind: "decide", title: i.title, detail: `${i.label && i.label !== i.title ? `${i.label} · ` : ""}${i.system ? `${systemLabel(i.system)} · ` : ""}due ${formatDateTime(i.due_at, now)}`, href: `/autopilot?item=${encodeURIComponent(i.item_id)}`, rank: stamp(i.due_at) });
    }
  } else unread.push("the autopilot review queue");
  if (s.writes) {
    for (const v of s.writes) {
      const id = (v.intent as { intent_id?: string }).intent_id ?? v.at;
      if (v.status === "prepared") needs.push({ key: `w:${id}`, kind: "approve", title: v.text, detail: `Business-system entry prepared ${ago(v.at, now)}`, href: writeHref(v), rank: stamp(v.at) });
      else if (v.status === "failed") needs.push({ key: `w:${id}`, kind: "blocked", title: v.text, detail: `The write failed ${ago(v.at, now)}; open the record to see why`, href: writeHref(v), rank: stamp(v.at) });
    }
  } else unread.push("prepared write entries");
  if (s.approvals) {
    for (const g of s.approvals.groups ?? []) {
      const oldest = `oldest ${plural(g.oldest_days, "day")}`;
      if (isMe(s.me, g.waiting_on)) {
        for (const a of g.items) needs.push({ key: `a:${a.id}`, kind: "approve", title: a.summary || a.kind, detail: `${a.asked_by ? `Asked by ${a.asked_by} · ` : ""}${plural(a.days, "day")} waiting`, href: a.object_id ? objectHref(a.object_id.split(":")[0], a.object_id) : "/team", rank: -a.days });
      } else if (g.oldest_days >= STUCK_DAYS) {
        needs.push({ key: `ag:${g.waiting_on}`, kind: "blocked", title: `${plural(g.count, "approval")} waiting on ${g.waiting_on}`, detail: `${oldest}; a nudge is drafted on Team`, href: "/team", rank: -g.oldest_days });
      }
    }
  } else unread.push("the approvals queue");
  // the alarms are the owner's and the Team readers' only; a refusal there is not a gap for anyone else
  if (s.alarms) {
    for (const o of s.alarms.open) needs.push({ key: `o:${o.key}`, kind: "blocked", title: opsHeadline(o.line), detail: `Open since ${ago(o.opened_at, now).replace(/ ago$/, "")}`, href: "/ops", rank: stamp(o.opened_at) });
  }
  const order: NeedKind[] = ["decide", "approve", "blocked"];
  needs.sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind) || a.rank - b.rank);
  return { needs, unread };
}

const SHOWN = 4;

export function NeedsMeList({ sources }: { sources: NeedsSources }) {
  const [open, setOpen] = useState<NeedKind | null>(null);
  const { needs, unread } = needsOf(sources);
  const kinds = (["decide", "approve", "blocked"] as NeedKind[]).map((k) => [k, needs.filter((n) => n.kind === k)] as const);
  return (
    <section className="border-b border-border px-5 py-3" aria-label="Needs you" data-testid="needs-me">
      <h2 className="flex items-baseline gap-2 text-[13px] font-semibold">
        Needs you
        {needs.length > 0 && (
          <span className="text-[12px] font-normal text-muted" data-testid="needs-me-counts">
            {kinds.map(([k, list]) => `${list.length} to ${k === "blocked" ? "unblock" : k}`).join(" · ")}
          </span>
        )}
      </h2>
      {needs.length === 0 && unread.length === 0 && <p className="mt-1 text-[12px] text-muted">No decision, approval or blocked item waits on you.</p>}
      {kinds.map(([k, list]) =>
        list.length === 0 ? null : (
          <div key={k} className="mt-2" data-testid={`needs-me-${k}`}>
            <h3 className={cn("text-[11px] font-medium", k === "blocked" ? "text-late" : "text-accent")}>{KIND_WORD[k]}</h3>
            <ul>
              {(open === k ? list : list.slice(0, SHOWN)).map((n) => {
                const body = (
                  <>
                    <span className="block truncate text-[13px]">{n.title}</span>
                    <span className="block truncate text-[12px] text-muted">{n.detail}</span>
                  </>
                );
                return (
                  <li key={n.key} className="py-1">
                    {n.href ? (
                      <Link href={n.href} className="block hover:underline">
                        {body}
                      </Link>
                    ) : (
                      body
                    )}
                  </li>
                );
              })}
            </ul>
            {list.length > SHOWN && open !== k && (
              <button type="button" onClick={() => setOpen(k)} className="text-[12px] font-medium text-muted hover:text-fg cursor-pointer">
                {list.length - SHOWN} more
              </button>
            )}
          </div>
        ),
      )}
      {unread.length > 0 && (
        <p className="mt-2 text-[12px] text-muted" data-testid="needs-me-unread">
          Couldn&apos;t read {unread.join(", ")}; the list may be missing items from there.
        </p>
      )}
    </section>
  );
}

/** The owner's "Needs you" on My work; nothing at all for any other reader. */
export function NeedsMe() {
  const [sources, setSources] = useState<NeedsSources | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    const soft = <T,>(p: Promise<T>) => p.then((v) => v, () => null);
    Promise.all([soft(getMe(ctl.signal)), soft(getAutopilot(ctl.signal)), soft(getWrites(ctl.signal).then((l) => l.items)), soft(getApprovals(ctl.signal)), soft(getOpsAlarms(ctl.signal))]).then(
      ([me, autopilot, writes, approvals, alarms]) => {
        if (!ctl.signal.aborted) setSources({ me, autopilot, writes, approvals, alarms });
      },
    );
    return () => ctl.abort();
  }, []);
  // the owner is the server's word (the autopilot area's owner flag); unknown is not the owner
  if (!sources?.autopilot?.owner) return null;
  return <NeedsMeList sources={sources} />;
}
