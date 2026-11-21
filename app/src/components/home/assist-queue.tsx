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
import { getAutopilotDrafts, setNeverSender, type PreparedItem } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";

export const SWITCHES_HREF = "/autopilot";

/** Of today's items, how many Auto would send by itself and how many would still wait (a dry run). */
export function autoDigest(items: PreparedItem[]): { send: number; wait: number } {
  return {
    send: items.filter((i) => i.auto_would === "send").length,
    wait: items.filter((i) => (i.auto_would ?? "").startsWith("keep")).length,
  };
}

export function AssistQueue({ onCount }: { onCount?: (n: number | null) => void }) {
  const params = useSearchParams();
  const linked = params?.get("item") ?? null;
  const [items, setItems] = useState<PreparedItem[] | null>(null);
  const [never, setNever] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    getAutopilotDrafts(ctl.signal).then(
      (r) => {
        setItems(r.items);
        setNever(r.never_senders ?? []);
        setError(null);
      },
      (e) => !ctl.signal.aborted && setError(e),
    );
    return () => ctl.abort();
  }, [round]);
  useEffect(() => onCount?.(error ? null : items ? items.length : null), [items, error, onCount]);
  useEffect(() => {
    if (linked && items) document.getElementById(`prepared-${linked}`)?.scrollIntoView({ block: "center" });
  }, [linked, items]);
  if (error) return <div className="px-5 pt-3"><LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={reload} /></div>;
  if (!items) return null;            // the rest of Home shows meanwhile; the queue joins it when read
  const { send, wait } = autoDigest(items);
  return (
    <section className="border-b border-border px-5 pt-3 pb-1" aria-label="Prepared for you" data-testid="assist-queue">
      <h2 className="flex items-baseline gap-2 text-[13px] font-semibold">
        Prepared for you
        {items.length > 0 && <span className="num text-[12px] font-normal text-muted">{items.length}</span>}
        <Link href={SWITCHES_HREF} className="ml-auto text-[12px] font-normal text-muted hover:text-fg">
          Switches
        </Link>
      </h2>
      {linked && !items.some((i) => i.item_id === linked) && <p className="py-2 text-[13px] text-muted">That item is no longer waiting for you: it was answered, decided or set aside.</p>}
      {send + wait > 0 && (
        <p className="pt-1 text-[12px] text-muted" data-testid="auto-digest">
          Dry run of Auto: {send} of these would go by themselves, {wait} would still wait for you. Nothing is sent from here without your click.
        </p>
      )}
      {items.length === 0 ? (
        <p className="py-3 text-[13px] text-muted" data-testid="prepared-empty">
          Nothing is waiting for you. What gets prepared is set on <Link href={SWITCHES_HREF} className="underline-offset-2 hover:underline">Switches</Link>.
        </p>
      ) : (
        <PreparedView items={items} empty="" linked={linked} onGone={(id) => setItems((xs) => (xs ?? []).filter((x) => x.item_id !== id))} onChanged={reload} />
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
