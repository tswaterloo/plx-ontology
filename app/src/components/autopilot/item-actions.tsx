"use client";

// The actions every item in the queue shares, laid out the same on every kind of card:
// Later is one click; the rarer ones (not needed, make it a task, never from this sender,
// always do this) sit behind More. A card adds its own entries to More (copy, rewrite...).
// Nothing here sends or writes to a business system.

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblem } from "@/components/ui/notice";
import { alwaysDoThis, decideAutopilotItem, saveMemory, setNeverSender, type PreparedItem } from "@/lib/api";
import { plainError } from "@/lib/errors";

/** 8 AM tomorrow, the person's own time: when a "Later" item comes back. */
export function tomorrowMorning(now: Date = new Date()): Date {
  const d = new Date(now);
  d.setDate(d.getDate() + 1);
  d.setHours(8, 0, 0, 0);
  return d;
}

/** The decision note that says the person took the item on as their own task (scored as
 * neither right nor wrong: ``operating.autopilot_outcomes.TASK_NOTE``). */
export const TASK_NOTE = "made a task";

/** The task's words: what to do, and whom it is about. */
export function taskText(item: Pick<PreparedItem, "subject" | "summary" | "sender" | "ask">): string {
  const what = (item.subject || item.summary || item.ask || "the item from my queue").trim().replace(/^(re|fw|fwd):\s*/i, "");
  const who = item.sender ? ` (${item.sender})` : "";
  return `Remind me to follow up: ${what}${who}`.slice(0, 900);
}

export type MenuEntry = { label: string; run: () => void | Promise<void>; danger?: boolean };

/** A "More" button opening a short list of actions; Escape or a click elsewhere closes it. */
export function MoreMenu({ entries, disabled }: { entries: MenuEntry[]; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  if (!entries.length) return null;
  return (
    <div ref={box} className="relative">
      <Button size="sm" variant="outline" disabled={disabled} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        More
      </Button>
      {open && (
        <div role="menu" className="absolute left-0 z-20 mt-1 min-w-[15rem] border border-border bg-bg py-1 shadow-lift">
          {entries.map((e) => (
            <button
              key={e.label}
              type="button"
              role="menuitem"
              className={`block w-full px-3 py-1.5 text-left text-[13px] hover:bg-hover cursor-pointer ${e.danger ? "text-late" : ""}`}
              onClick={() => {
                setOpen(false);
                void e.run();
              }}
            >
              {e.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Later (back tomorrow 8 AM) as a button, then More: the card's own entries, Make it a task,
 * Not needed, Never from this sender, Always do this. ``primary`` goes first (the card's
 * main action). Always do this is offered only where the person's replay score earned Auto.
 */
export function ItemActions({ item, onGone, onChanged, primary, entries = [], busy, setAsideOnly }: { item: PreparedItem; onGone: (id: string) => void; onChanged?: () => void; primary?: React.ReactNode; entries?: MenuEntry[]; busy?: boolean; setAsideOnly?: boolean }) {
  const [working, setWorking] = useState(false);
  const [, setError, problem] = useProblem();
  const [note, setNote] = useState<string | null>(null);
  const run = async (act: () => Promise<unknown>, then: () => void) => {
    setWorking(true);
    setError(null);
    try {
      await act();
      then();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setWorking(false);
    }
  };
  const gone = () => onGone(item.item_id);
  const later = () => run(() => decideAutopilotItem(item.item_id, { decision: "snooze", until: tomorrowMorning().toISOString() }), gone);
  const more: MenuEntry[] = [...entries];
  if (!setAsideOnly) {
    more.push({
      label: "Make it a task",
      run: () =>
        run(async () => {
          await saveMemory({ kind: "commitment", text: taskText(item) });
          await decideAutopilotItem(item.item_id, { decision: "reject", note: TASK_NOTE });
        }, gone),
    });
  }
  more.push({ label: "Not needed", run: () => run(() => decideAutopilotItem(item.item_id, { decision: "reject" }), gone) });
  const sender = item.sender.includes("@") ? item.sender : "";
  if (!setAsideOnly && item.sender && onChanged) {
    more.push({ label: "Never from this sender", run: () => run(() => setNeverSender(item.sender, true), onChanged), danger: true });
  }
  if (!setAsideOnly && item.always_ok && item.category) {
    if (sender) {
      more.push({
        label: `Always do this for ${sender}`,
        run: () => run(() => alwaysDoThis(item.item_id, "sender"), () => setNote(`Auto is on for ${sender} under ${item.category_label || "this switch"}. Money and anything unclear still wait for you. Change it under Switches.`)),
      });
    }
    more.push({
      label: `Always do this: ${item.category_label || "this kind of item"}`,
      run: () => run(() => alwaysDoThis(item.item_id, "category"), () => setNote(`Auto is on for ${item.category_label || "this switch"}. Money and anything unclear still wait for you. Change it under Switches.`)),
    });
  }
  return (
    <>
      <div className="flex flex-wrap items-start gap-2" data-testid="item-actions">
        {primary}
        <Button size="sm" variant="outline" disabled={busy || working} onClick={() => void later()}>
          Later
        </Button>
        <MoreMenu entries={more} disabled={busy || working} />
      </div>
      {note && <p className="text-[12px] text-muted">{note}</p>}
      {problem}
    </>
  );
}
