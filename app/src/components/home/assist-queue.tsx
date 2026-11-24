"use client";

// The queue on Home: everything prepared for you (replies with their draft, questions only
// you can answer, system changes to review, alerts waiting on you), newest first, each with
// the same actions. What gets prepared, and what may run by itself, is set on the Switches
// tab. Nothing is sent or changed until you confirm it.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { PreparedView } from "@/components/autopilot/prepared";
import { Button } from "@/components/ui/button";
import { LoadProblem } from "@/components/ui/notice";
import { getAutopilotDrafts, setNeverSender, type DraftList, type PreparedItem } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { useFeed } from "@/lib/feeds";

export const SWITCHES_HREF = "/autopilot";

/** Of today's items, how many Auto would send by itself and how many would still wait (a dry run). */
export function autoDigest(items: PreparedItem[]): { send: number; wait: number } {
  return {
    send: items.filter((i) => i.auto_would === "send").length,
    wait: items.filter((i) => (i.auto_would ?? "").startsWith("keep")).length,
  };
}

/** Pure: the queue's items split the way Home's chips count them: what you confirm (a draft,
 * a question, a change) and alerts waiting on you, which have nothing to confirm. */
export function splitPrepared(items: PreparedItem[]): { confirm: PreparedItem[]; alerts: PreparedItem[] } {
  return { confirm: items.filter((i) => i.kind !== "alert"), alerts: items.filter((i) => i.kind === "alert") };
}

export type PreparedCount = { confirm: number; alerts: number };

/** Lists on Home that each show part of the queue and mount together share one read of it. */
let inFlight: Promise<DraftList> | null = null;
function readDrafts(round: number, allAges = false): Promise<DraftList> {
  if (round > 0 || allAges) return getAutopilotDrafts(undefined, allAges);
  if (!inFlight) {
    // Home's first read of the list is a read-ahead (W6): the rest of Home is complete and usable
    // without it (it can take seconds), and the list says it is being prepared until it arrives
    const read = getAutopilotDrafts(undefined, false, true);
    inFlight = read;
    const done = () => {
      if (inFlight === read) inFlight = null;
    };
    read.then(done, done);
  }
  return inFlight;
}

/**
 * ``except``: alert kinds whose cards are listed in that kind's own section (a hold that blocks
 * an order beside the other holds), not here. ``kind``: only those cards, with no heading of
 * their own, for that section; ``onKind`` says how many there are.
 */
export function AssistQueue({ onCount, only, except, kind, onKind }: { onCount?: (n: PreparedCount | null) => void; only?: "confirm" | "alerts"; except?: string[]; kind?: string; onKind?: (n: number) => void }) {
  const params = useSearchParams();
  const linked = params?.get("item") ?? null;
  const [items, setItems] = useState<PreparedItem[] | null>(null);
  const [never, setNever] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [older, setOlder] = useState(0);
  const [allAges, setAllAges] = useState(false);
  const [listNote, setListNote] = useState("");
  const feed = useFeed("autopilot");
  const reload = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    let live = true;
    readDrafts(round, allAges).then(
      (r) => {
        if (!live) return;
        setItems(r.items);
        setOlder(r.older ?? 0);
        // the list says when it is not current or not shown (never a silent empty list)
        setListNote(r.status === "stale" ? "The list was last re-checked a while ago; it may be behind." : r.status === "off" || r.status === "not_run" ? (r.reason ? `Your list is not shown: ${r.reason}.` : "") : "");
        setNever(r.never_senders ?? []);
        setError(null);
      },
      (e) => live && setError(e),
    );
    return () => {
      live = false;
    };
  }, [round, allAges]);
  useEffect(() => {
    if (kind && onKind) onKind(items?.filter((i) => i.kind === "alert" && i.alert_kind === kind).length ?? 0);
  }, [items, kind, onKind]);
  useEffect(() => {
    if (error || !items) return onCount?.(null);
    const { confirm, alerts } = splitPrepared(items);
    onCount?.({ confirm: confirm.length, alerts: alerts.length });
  }, [items, error, onCount]);
  useEffect(() => {
    if (linked && items) document.getElementById(`prepared-${linked}`)?.scrollIntoView({ block: "center" });
  }, [linked, items]);
  if (error) return <div className="px-5 pt-3"><LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={reload} /></div>;
  // the rest of Home shows meanwhile; the queue joins it when read (a quiet line says so, only
  // where the queue has a section of its own)
  if (!items) return kind || only ? null : <p className="px-5 pt-3 text-[12px] text-muted" data-testid="assist-queue-loading" aria-live="polite">Your list is being prepared.</p>;
  if (kind) {
    const mine = items.filter((i) => i.kind === "alert" && i.alert_kind === kind);
    if (mine.length === 0) return null;
    return <PreparedView items={mine} empty="" linked={linked} onGone={(id) => setItems((xs) => (xs ?? []).filter((x) => x.item_id !== id))} onChanged={reload} />;
  }
  const left = except?.length ? items.filter((i) => !(i.kind === "alert" && i.alert_kind && except.includes(i.alert_kind))) : items;
  const split = splitPrepared(left);
  const listed = only === "confirm" ? split.confirm : only === "alerts" ? split.alerts : left;
  if (only && listed.length === 0) return null;
  // everything waiting is listed in its kind's own section: no empty queue beside it
  if (listed.length === 0 && left.length < items.length) return null;
  const { send, wait } = autoDigest(listed);
  // the heading says what is there: drafts to confirm are prepared for you; alerts alone
  // (a QC hold) have nothing to confirm, so they are not called prepared
  const heading = split.confirm.length === 0 && split.alerts.length > 0 || only === "alerts" ? "Waiting on you" : "Prepared for you";
  return (
    <section className="border-b border-border px-5 pt-3 pb-1" aria-label={heading} data-testid="assist-queue">
      <h2 className="flex items-baseline gap-2 text-[13px] font-semibold">
        {heading}
        {listed.length > 0 && <span className="num text-[12px] font-normal text-muted">{listed.length}</span>}
        <Link href={SWITCHES_HREF} className="ml-auto text-[12px] font-normal text-muted hover:text-fg">
          Switches
        </Link>
      </h2>
      {listNote && <p className="py-1 text-[12px] text-muted" data-testid="list-note">{listNote}</p>}
      {linked && !items.some((i) => i.item_id === linked) && <p className="py-2 text-[13px] text-muted">That item is no longer waiting for you: it was answered, decided or set aside.</p>}
      {send + wait > 0 && (
        <p className="pt-1 text-[12px] text-muted" data-testid="auto-digest">
          Dry run of Auto: {send} of these would go by themselves, {wait} would still wait for you. Nothing is sent from here without your click.
        </p>
      )}
      {listed.length === 0 ? (
        <p className="py-3 text-[13px] text-muted" data-testid="prepared-empty">
          {feed && feed.state !== "running"
            ? "Nothing is waiting for you, but nothing new is being prepared either: the feed behind it is not running. "
            : "Nothing is waiting for you. "}
          What gets prepared is set on <Link href={SWITCHES_HREF} className="underline-offset-2 hover:underline">Switches</Link>.
        </p>
      ) : (
        <PreparedView items={listed} empty="" linked={linked} onGone={(id) => setItems((xs) => (xs ?? []).filter((x) => x.item_id !== id))} onChanged={reload} />
      )}
      {older > 0 && !allAges && (
        <p className="py-2 text-[12px] text-muted" data-testid="older-items">
          {older} older {older === 1 ? "item is" : "items are"} not shown.{" "}
          <Button size="sm" variant="ghost" onClick={() => setAllAges(true)}>
            Show them
          </Button>
        </p>
      )}
      {never.length > 0 && (
        <ul className="space-y-1 pt-2 pb-2 text-[12px] text-muted" aria-label="Never prepared from">
          {never.map((s) => (
            <li key={s} className="flex items-center gap-2">
              Nothing is prepared for you from {s}.
              <Button size="sm" variant="ghost" onClick={() => void setNeverSender(s, false).then(reload, (e) => setError(e))}>
                Prepare again
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
