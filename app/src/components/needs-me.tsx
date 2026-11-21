"use client";

// What needs the owner: the decisions, approvals and blocked items waiting on them across
// the Workbench, read from the reader-scoped endpoints that already hold them (prepared
// write entries and the approvals queue; what the autopilot prepared is the queue above it). Reliability
// alarms are not the owner's work: they live on the Admin page. Shown only when the server
// says the reader is the owner; a source that can't be
// read is named, never counted as zero.

import Link from "next/link";
import { useEffect, useState } from "react";
import { getApprovals, getMe, getWrites } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ago, objectHref, plural } from "@/lib/format";
import type { ApprovalsQueue, Me, WriteIntentView } from "@/lib/types";

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
  writes: WriteIntentView[] | null;
  /** undefined while the queue is still being read (it fills in later; never "couldn't read") */
  approvals?: ApprovalsQueue | null;
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
        const who = g.waiting_on === "not recorded" ? "an approver not on record" : g.waiting_on;
        const drafted = g.items.some((a) => a.nudge?.body);
        needs.push({ key: `ag:${g.waiting_on}`, kind: "blocked", title: `${plural(g.count, "approval")} waiting on ${who}`, detail: `${oldest}${drafted ? "; a nudge is drafted on Team" : "; no nudge drafted yet"}`, href: "/team#approvals", rank: -g.oldest_days });
      }
    }
  } else if (s.approvals === null) unread.push("the approvals queue");
  const order: NeedKind[] = ["decide", "approve", "blocked"];
  needs.sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind) || a.rank - b.rank);
  return { needs, unread };
}

const SHOWN = 4;

export function NeedsMeList({ sources }: { sources: NeedsSources }) {
  const [open, setOpen] = useState<NeedKind | null>(null);
  const { needs, unread } = needsOf(sources);
  const kinds = (["decide", "approve", "blocked"] as NeedKind[]).map((k) => [k, needs.filter((n) => n.kind === k)] as const);
  // every source read and nothing waits: no block at all (Home says it once for the whole page)
  if (needs.length === 0 && unread.length === 0) return null;
  return (
    <section className="border-b border-border px-5 py-3" aria-label="Needs you" data-testid="needs-me">
      <h2 className="flex items-baseline gap-2 text-[13px] font-semibold">
        Needs you
        {needs.length > 0 && (
          <span className="text-[12px] font-normal text-muted" data-testid="needs-me-counts">
            {kinds.filter(([, list]) => list.length > 0).map(([k, list]) => `${list.length} to ${k === "blocked" ? "unblock" : k}`).join(" · ")}
          </span>
        )}
      </h2>
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
  // the approvals queue is read on its own: the list shows without waiting for it (its
  // first read can take a minute on a cold server) and gains its rows when it lands
  const [approvals, setApprovals] = useState<ApprovalsQueue | null | undefined>(undefined);
  useEffect(() => {
    const ctl = new AbortController();
    const soft = <T,>(p: Promise<T>) => p.then((v) => v, () => null);
    Promise.all([soft(getMe(ctl.signal)), soft(getWrites(ctl.signal).then((l) => l.items))]).then(([me, writes]) => {
      if (!ctl.signal.aborted) setSources({ me, writes });
    });
    soft(getApprovals(ctl.signal)).then((a) => !ctl.signal.aborted && setApprovals(a));
    return () => ctl.abort();
  }, []);
  // the owner is the server's word (/api/me); unknown is not the owner
  if (!sources?.me?.owner) return null;
  return <NeedsMeList sources={{ ...sources, approvals }} />;
}
