"use client";

import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblemSlot } from "@/components/ui/notice";
import { decideAutopilotItem, replyFromLine, saveMemory, type PreparedItem } from "@/lib/api";
import { DraftUse, draftUseOf } from "@/components/autopilot/draft-use";
import { JobCard } from "@/components/autopilot/job";
import { PreparedFeedback } from "@/components/autopilot/prepared-feedback";
import { ItemActions, type MenuEntry } from "@/components/autopilot/item-actions";

export { tomorrowMorning } from "@/components/autopilot/item-actions";
import { TodoCard, TodoSteps } from "@/components/autopilot/todo";
import { AlertCard } from "@/components/autopilot/alert";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";
import { personName, workflowLabel } from "@/lib/autopilot";
import type { AutopilotArea as Area } from "@/lib/types";

/** The reply as an Outlook draft: a mailto link with the recipients, subject and body. */
export function mailtoOf(i: PreparedItem, body: string): string {
  const q = [i.cc.length ? `cc=${encodeURIComponent(i.cc.join(","))}` : "", `subject=${encodeURIComponent(i.subject)}`, `body=${encodeURIComponent(body)}`].filter(Boolean);
  return `mailto:${i.to.map(encodeURIComponent).join(",")}?${q.join("&")}`;
}

/** Autopilot "score" of a workflow: accepted or edited against rejected, and whether it turned
 * itself off (10 or more settled and fewer than one in five accepted or edited). */
export function scoreOf(c: { accepted: number; edited: number; rejected: number }): { settled: number; kept: number; off: boolean } {
  const settled = c.accepted + c.edited + c.rejected;
  const kept = c.accepted + c.edited;
  return { settled, kept, off: settled >= 10 && kept * 5 < settled };
}

/** The same words, whatever the spacing or case: a title that is its body is said once. */
const sameWords = (a: string, b: string) => a.replace(/\s+/g, " ").trim().toLowerCase() === b.replace(/\s+/g, " ").trim().toLowerCase();

function Card({ item, linked, onGone, onChanged }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void; onChanged: () => void }) {
  const [text, setText] = useState(item.body);
  const [editing, setEditing] = useState(false);
  const [line, setLine] = useState("");
  const [pref, setPref] = useState("");
  const [open, setOpen] = useState<"rewrite" | "pref" | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const card = useProblemSlot();
  const setError = card.set;
  const run = async (f: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await f();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const write = () =>
    run(async () => {
      const out = await replyFromLine(item.item_id, line);
      setText(out.decision?.text ?? "");
      setEditing(true);
      setOpen(null);
      setLine("");
      onChanged();
    });
  const confirm = () =>
    run(async () => {
      const out = await decideAutopilotItem(item.item_id, { decision: "accept", ...(text !== item.body ? { text } : {}), ...(token ? { confirm_token: token } : {}) });
      if (out.confirm_token) {
        setToken(out.confirm_token);
        return;
      }
      if (out.decision?.sent) onGone(item.item_id);
      else if (out.state === "waiting") setNote("Not sent: the send was refused or failed. It stays here to confirm again.");
      else setNote("Confirmed. Nothing was sent: sending isn't switched on for this yet.");
    });
  const keep = () =>
    run(async () => {
      await saveMemory({ kind: "preference", text: pref.trim() });
      setPref("");
      setOpen(null);
      setNote("Saved as your preference: your next drafts follow it. Change or remove it in Memory.");
    });
  const haveReply = text.trim() !== "";
  const versions = (item.options ?? []).filter((o) => o.reply.trim() && o.reply !== text);
  const entries: MenuEntry[] = [];
  if (haveReply && item.redo) entries.push({ label: "Rewrite from a line", run: () => setOpen("rewrite") });
  if (haveReply) entries.push({ label: "Copy", run: () => run(async () => void (await navigator.clipboard.writeText(text))) });
  if (haveReply && item.channel === "mail" && item.to.length > 0) entries.push({ label: "Open in Outlook", run: () => void (window.location.href = mailtoOf(item, text)) });
  entries.push({ label: "Save a preference for my drafts", run: () => setOpen("pref") });
  return (
    <li id={`prepared-${item.item_id}`} className={cn("space-y-2 py-4", linked && "border-l-2 border-accent pl-3")} data-testid="prepared-item">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="text-[13px] font-medium">{item.sender || "Someone"}</span>
        {item.subject && !sameWords(item.subject, item.message) && <span className="text-[12px] text-muted">{item.subject}</span>}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {item.message && <p className="max-w-[46rem] whitespace-pre-wrap text-[13px] text-muted">{countedWords(item.message)}</p>}
      {!haveReply && (
        <div className="max-w-[46rem] space-y-2 border-l-2 border-border pl-3">
          <p className="text-[13px]">{countedWords(item.ask)}</p>
          {item.facts.length > 0 && (
            <ul className="list-disc pl-4 text-[12px] text-muted">
              {item.facts.map((f) => (
                <li key={f}>{countedWords(f)}</li>
              ))}
            </ul>
          )}
          {(item.options ?? []).length > 0 && (
            <div className="flex flex-wrap gap-2" aria-label="Answers">
              {(item.options ?? []).map((o) => (
                <Button key={o.reply} size="sm" variant="outline" disabled={busy} onClick={() => setText(o.reply)}>
                  {o.label || "This one"}
                </Button>
              ))}
            </div>
          )}
          <label className="block text-[12px] text-muted">
            {(item.options ?? []).length > 0 ? "Or say it in a few words" : "What do you want to say?"}
            <input value={line} maxLength={1000} onChange={(e) => setLine(e.target.value)} className="mt-1 h-8 w-full border border-border bg-bg px-2 text-[13px] text-fg" />
          </label>
          <Button size="sm" disabled={busy || !line.trim()} onClick={() => void write()}>
            Write the reply
          </Button>
        </div>
      )}
      {haveReply && (
        <>
          {editing ? <textarea aria-label="Reply" value={text} onChange={(e) => setText(e.target.value)} rows={8} className="w-full max-w-[46rem] border border-border bg-bg p-2 text-[13px] text-fg" /> : <p className="max-w-[46rem] whitespace-pre-wrap bg-hover p-3 text-[13px]">{text}</p>}
          {versions.length > 0 && (
            <div className="flex max-w-[46rem] flex-wrap items-center gap-2" aria-label="Other versions">
              <span className="text-[12px] text-muted">Other versions:</span>
              {versions.map((o) => (
                <Button key={o.reply} size="sm" variant="ghost" disabled={busy} onClick={() => setText(o.reply)}>
                  {o.label || "Another version"}
                </Button>
              ))}
            </div>
          )}
          {open === "rewrite" && (
            <div className="flex max-w-[46rem] flex-wrap items-end gap-2">
              <label className="block min-w-[16rem] flex-1 text-[12px] text-muted">
                Rewrite it: what do you want to say?
                <input autoFocus value={line} maxLength={1000} onChange={(e) => setLine(e.target.value)} className="mt-1 h-8 w-full border border-border bg-bg px-2 text-[13px] text-fg" />
              </label>
              <Button size="sm" variant="outline" disabled={busy || !line.trim()} onClick={() => void write()}>
                Rewrite
              </Button>
            </div>
          )}
        </>
      )}
      {open === "pref" && (
        <div className="flex max-w-[46rem] flex-wrap items-end gap-2">
          <label className="min-w-[16rem] flex-1 text-[12px] text-muted">
            Save as a preference (for example: never mention prices to Kim)
            <input autoFocus value={pref} maxLength={1000} onChange={(e) => setPref(e.target.value)} className="mt-1 h-8 w-full border border-border bg-bg px-2 text-[13px] text-fg" />
          </label>
          <Button size="sm" variant="outline" disabled={busy || !pref.trim()} onClick={() => void keep()}>
            Save
          </Button>
        </div>
      )}
      <TodoSteps todos={item.todos ?? []} />
      <DraftUse use={draftUseOf(item)} />
      {item.auto_would && (
        <p className="max-w-[46rem] text-[12px] text-muted" data-testid="auto-would">
          {item.auto_would === "send" ? "With Auto on, this would go by itself." : "With Auto on, this would still wait for you."}
        </p>
      )}
      {(item.learned ?? []).length > 0 && (
        <p className="max-w-[46rem] text-[12px] text-muted" data-testid="draft-learned">
          Learned from your edit: {(item.learned ?? []).join("; ")}
        </p>
      )}
      {card.provide(<ItemActions
        item={item}
        onGone={onGone}
        onChanged={onChanged}
        busy={busy}
        entries={entries}
        primary={
          haveReply ? (
            <>
              <Button size="sm" disabled={busy} onClick={() => void confirm()}>
                {token ? "Confirm again to send" : "Confirm"}
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing((v) => !v)}>
                {editing ? "Done editing" : "Edit"}
              </Button>
            </>
          ) : null
        }
      />)}
      {note && <p className="text-[12px] text-muted">{note}</p>}
      {card.banner}
    </li>
  );
}

/** Cards drawn at first: the list is all there (its count, every item one click away). Drawing
 * all of an owner's ~1,100 at once made Home 15,000 elements and a 0.75 s block. */
export const FIRST_CARDS = 40;

export function PreparedView({ items, empty, linked, onGone, onChanged }: { items: PreparedItem[]; empty: string; linked?: string | null; onGone: (id: string) => void; onChanged: () => void }) {
  const [shown, setShown] = useState(FIRST_CARDS);
  if (items.length === 0) return <p className="py-6 text-[13px] text-muted" data-testid="prepared-empty">{empty}</p>;
  // a linked item (a deep link, a notification) is always drawn, wherever it sits in the list
  const at = linked ? items.findIndex((i) => i.item_id === linked) : -1;
  const count = Math.max(shown, at + 1);
  const drawn = items.length > count ? items.slice(0, count) : items;
  return (
    <>
      <ul className="divide-y divide-border" aria-label="Prepared for me">
        {drawn.map((i) => (
          <Fragment key={i.item_id}>
          {i.kind === "job" ? (
            <JobCard item={i} linked={i.item_id === linked} onGone={onGone} />
          ) : i.kind === "todo" ? (
            <TodoCard item={i} linked={i.item_id === linked} onGone={onGone} />
          ) : i.kind === "alert" ? (
            <AlertCard item={i} linked={i.item_id === linked} onGone={onGone} onChanged={onChanged} />
          ) : (
            <Card item={i} linked={i.item_id === linked} onGone={onGone} onChanged={onChanged} />
          )}
          <li className="-mt-2 pb-3 !border-t-0" data-testid="prepared-feedback-row"><PreparedFeedback item={i} /></li>
          </Fragment>
        ))}
      </ul>
      {drawn.length < items.length && (
        <p className="flex items-center gap-2 py-2 text-[12px] text-muted" data-testid="more-cards">
          {items.length - drawn.length} more
          <Button size="sm" variant="ghost" onClick={() => setShown(drawn.length + 100)}>
            Show 100 more
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setShown(items.length)}>
            Show all
          </Button>
        </p>
      )}
    </>
  );
}

/** Admins: who switched what on, and each workflow's accepted, edited and rejected counts. */
export function EveryoneView({ area }: { area: Area }) {
  const who = area.drafts_switched_on ?? [];
  const by = Object.entries(area.outcomes?.by_autopilot ?? {}).filter(([, c]) => c.accepted + c.edited + c.rejected > 0);
  const waiting = new Map<string, number>();
  for (const i of area.items ?? []) if (i.state === "waiting" || i.state === "edited") waiting.set(i.owner, (waiting.get(i.owner) ?? 0) + 1);
  const queue = [...waiting].sort((a, b) => b[1] - a[1]);
  return (
    <div className="space-y-6">
      <section aria-labelledby="ap-waiting" className="space-y-2">
        <h2 id="ap-waiting" className="text-[13px] font-semibold">
          Waiting now, per person
        </h2>
        {queue.length === 0 ? (
          <p className="text-[13px] text-muted">Nothing is waiting on anyone.</p>
        ) : (
          <ul className="divide-y divide-border" data-testid="ap-waiting">
            {queue.map(([p, n]) => (
              <li key={p} className="flex gap-3 py-1.5 text-[13px]">
                <span>{personName(area, p)}</span>
                <span className="text-muted">{n} waiting</span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="ap-who" className="space-y-2">
        <h2 id="ap-who" className="text-[13px] font-semibold">
          Who switched what on
        </h2>
        {who.length === 0 ? (
          <p className="text-[13px] text-muted">Nobody has switched anything on.</p>
        ) : (
          // one line per person (their workflows in it), never the same name once per workflow
          <ul className="divide-y divide-border" data-testid="ap-who">
            {[...new Set(who.map((w) => w.person))].map((person) => {
              const theirs = who.filter((w) => w.person === person);
              // "set by the owner" once when it holds for every one of them
              const allOwner = theirs.every((w) => w.by_owner);
              return (
                <li key={person} className="flex gap-3 py-1.5 text-[13px]">
                  <span className="shrink-0">{personName(area, person)}</span>
                  <span className="text-muted">
                    {theirs.map((w) => `${w.label}${w.partly ? " (partly)" : ""}${w.by_owner && !allOwner ? " (set by the owner)" : ""}`).join(", ")}
                    {allOwner ? "; all set by the owner" : ""}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </section>
      {/* only once something was accepted, edited or rejected: an empty score section says nothing */}
      {by.length > 0 && (
        <section aria-labelledby="ap-score" className="space-y-2">
          <h2 id="ap-score" className="text-[13px] font-semibold">
            How each one is doing
          </h2>
          <ul className="divide-y divide-border">
            {by.map(([name, c]) => {
              const s = scoreOf(c);
              return (
                <li key={name} className="flex flex-wrap gap-x-4 py-1.5 text-[13px]">
                  <span className="min-w-[14rem]">{workflowLabel(name)}</span>
                  <span className="num text-muted">
                    {c.accepted} accepted, {c.edited} edited, {c.rejected} rejected
                  </span>
                  {s.off && <span className="text-late">Below the bar, keep it off: {s.kept} of {s.settled} kept</span>}
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
