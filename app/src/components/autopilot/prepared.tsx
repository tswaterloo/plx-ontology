"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/notice";
import { decideAutopilotItem, replyFromLine, saveMemory, type PreparedItem } from "@/lib/api";
import { DraftUse, draftUseOf } from "@/components/autopilot/draft-use";
import { JobCard } from "@/components/autopilot/job";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
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

function Card({ item, linked, onGone, onChanged }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void; onChanged: () => void }) {
  const [text, setText] = useState(item.body);
  const [editing, setEditing] = useState(false);
  const [line, setLine] = useState("");
  const [pref, setPref] = useState("");
  const [token, setToken] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
      else setNote("Confirmed. Nothing was sent: this is not switched on for sending yet.");
    });
  const copy = () => run(async () => void (await navigator.clipboard.writeText(text)));
  const keep = () =>
    run(async () => {
      await saveMemory({ kind: "preference", text: pref.trim() });
      setPref("");
      setNote("Saved as your preference: your next drafts follow it. Change or remove it in Memory.");
    });
  const haveReply = text.trim() !== "";
  return (
    <li id={`prepared-${item.item_id}`} className={cn("space-y-2 py-4", linked && "border-l-2 border-accent pl-3")} data-testid="prepared-item">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="text-[13px] font-medium">{item.sender || "Someone"}</span>
        {item.subject && <span className="text-[12px] text-muted">{item.subject}</span>}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {item.message && <p className="max-w-[46rem] whitespace-pre-wrap text-[13px] text-muted">{item.message}</p>}
      {!haveReply && (
        <div className="max-w-[46rem] space-y-2 border-l-2 border-border pl-3">
          <p className="text-[13px]">{item.ask}</p>
          {item.facts.length > 0 && (
            <ul className="list-disc pl-4 text-[12px] text-muted">
              {item.facts.map((f) => (
                <li key={f}>{f}</li>
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
            {(item.options ?? []).length > 0 ? "Other: answer in a few words" : "What do you want to say?"}
            <input value={line} maxLength={1000} onChange={(e) => setLine(e.target.value)} className="mt-1 h-8 w-full rounded-lg border border-border bg-bg px-2 text-[13px] text-fg" />
          </label>
          <Button size="sm" disabled={busy || !line.trim()} onClick={() => void write()}>
            Write the reply
          </Button>
        </div>
      )}
      {haveReply && (
        <>
          {editing ? <textarea aria-label="Reply" value={text} onChange={(e) => setText(e.target.value)} rows={8} className="w-full max-w-[46rem] rounded-lg border border-border bg-bg p-2 text-[13px] text-fg" /> : <p className="max-w-[46rem] whitespace-pre-wrap rounded-lg bg-hover p-3 text-[13px]">{text}</p>}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={busy} onClick={() => void confirm()}>
              {token ? "Confirm again to send" : "Confirm"}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditing((v) => !v)}>
              {editing ? "Done editing" : "Edit"}
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void copy()}>
              Copy
            </Button>
            {item.channel === "mail" && item.to.length > 0 && (
              <a className="inline-flex h-8 items-center rounded-lg border border-border px-3 text-[13px] hover:bg-hover" href={mailtoOf(item, text)}>
                Open in Outlook
              </a>
            )}
          </div>
        </>
      )}
      <DraftUse use={draftUseOf(item)} />
      {(item.learned ?? []).length > 0 && (
        <p className="max-w-[46rem] text-[12px] text-muted" data-testid="draft-learned">
          Learned from your edit: {(item.learned ?? []).join("; ")}
        </p>
      )}
      <div className="flex max-w-[46rem] flex-wrap items-end gap-2">
        <label className="min-w-[16rem] flex-1 text-[12px] text-muted">
          Save as a preference (for example: never mention prices to Kim)
          <input value={pref} maxLength={1000} onChange={(e) => setPref(e.target.value)} className="mt-1 h-8 w-full rounded-lg border border-border bg-bg px-2 text-[13px] text-fg" />
        </label>
        <Button size="sm" variant="outline" disabled={busy || !pref.trim()} onClick={() => void keep()}>
          Save
        </Button>
      </div>
      {note && <p className="text-[12px] text-muted">{note}</p>}
      {error && <ErrorNotice message={error} />}
    </li>
  );
}

/** Why the list is empty: nothing switched on, or on and nothing new as of the feed's last look. */
export function emptyWords(area: Pick<Area, "my_drafts" | "checked_at">): string {
  if (!(area.my_drafts ?? []).some((c) => c.on)) return "Nothing is prepared for you: nothing is switched on above.";
  if (!area.checked_at) return "On. Nothing new for you yet.";
  return `On. Nothing new for you as of ${formatDateTime(area.checked_at)}.`;
}

export function PreparedView({ items, empty, linked, onGone, onChanged }: { items: PreparedItem[]; empty: string; linked?: string | null; onGone: (id: string) => void; onChanged: () => void }) {
  if (items.length === 0) return <p className="py-6 text-[13px] text-muted" data-testid="prepared-empty">{empty}</p>;
  return (
    <ul className="divide-y divide-border" aria-label="Prepared for me">
      {items.map((i) =>
        i.kind === "job" ? (
          <JobCard key={i.item_id} item={i} linked={i.item_id === linked} />
        ) : (
          <Card key={i.item_id} item={i} linked={i.item_id === linked} onGone={onGone} onChanged={onChanged} />
        ),
      )}
    </ul>
  );
}

/** Admins: who switched what on, and each workflow's accepted, edited and rejected counts. */
export function EveryoneView({ area }: { area: Area }) {
  const who = area.drafts_switched_on ?? [];
  const by = Object.entries(area.outcomes?.by_autopilot ?? {}).filter(([, c]) => c.accepted + c.edited + c.rejected > 0);
  return (
    <div className="space-y-6">
      <section aria-labelledby="ap-who" className="space-y-2">
        <h2 id="ap-who" className="text-[13px] font-semibold">
          Who switched what on
        </h2>
        {who.length === 0 ? (
          <p className="text-[13px] text-muted">Nobody has switched anything on.</p>
        ) : (
          <ul className="divide-y divide-border">
            {who.map((w) => (
              <li key={`${w.person}/${w.choice}`} className="flex gap-3 py-1.5 text-[13px]">
                <span>{personName(area, w.person)}</span>
                <span className="text-muted">
                  {w.label}
                  {w.partly ? ", partly" : ""}
                  {w.by_owner ? ", set by the owner" : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="ap-score" className="space-y-2">
        <h2 id="ap-score" className="text-[13px] font-semibold">
          How each one is doing
        </h2>
        {by.length === 0 ? (
          <p className="text-[13px] text-muted">Nothing has been accepted, edited or rejected yet.</p>
        ) : (
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
        )}
      </section>
    </div>
  );
}
