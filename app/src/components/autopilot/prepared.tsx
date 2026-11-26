"use client";

import { Copy, Mail, MessageSquare, Pencil } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { useProblemSlot } from "@/components/ui/notice";
import { decideAutopilotItem, replyFromLine, saveMemory, type PreparedItem } from "@/lib/api";
import { DraftUse, draftUseOf } from "@/components/autopilot/draft-use";
import { JobCard } from "@/components/autopilot/job";
import { StatusIndicator } from "@/components/autopilot/jobs";
import { PreparedFeedback } from "@/components/autopilot/prepared-feedback";
import { cleanHeadline, formatSender, noteParts, saidOnce } from "@/components/autopilot/lines";
import { ItemActions, type MenuEntry, type OptimisticCallbacks } from "@/components/autopilot/item-actions";

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

/** Elapsed seconds formatted plainly: "12s", "1m 5s". */
export function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

function startedAtMs(requestedAt?: string | null): number {
  if (requestedAt) {
    const parsed = Date.parse(requestedAt);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return Date.now();
}

/** Clean display name when sender is "Name <email@domain>", else as given. */
export function senderLabel(raw: string): string {
  const s = (raw ?? "").trim();
  const m = /^"?([^"<]+?)"?\s*<[^>]+>$/.exec(s);
  return m ? m[1].trim() : s;
}

export function PreparedCard({
  item,
  linked,
  detail,
  onGone,
  onChanged,
  onOptimistic,
  onRestore,
  onFeedback,
  feedback,
}: {
  item: PreparedItem;
  linked?: boolean;
  detail?: boolean;
  onGone: (id: string) => void;
  onChanged: () => void;
  feedback?: React.ReactNode;
} & OptimisticCallbacks) {
  const [text, setText] = useState(item.body);
  const [editing, setEditing] = useState(false);
  const [line, setLine] = useState("");
  const [pref, setPref] = useState("");
  const [open, setOpen] = useState<"rewrite" | "pref" | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const initiallyWorking = item.plan_status === "working";
  const [waitingSince, setWaitingSince] = useState<number | null>(() => (initiallyWorking ? startedAtMs(item.requested_at) : null));
  const [sawWorking, setSawWorking] = useState<boolean>(initiallyWorking);
  const [baseline, setBaseline] = useState<{ body: string; planAt: string | null } | null>(null);
  const [elapsedSec, setElapsedSec] = useState<number>(() => (initiallyWorking ? Math.max(0, Math.floor((Date.now() - startedAtMs(item.requested_at)) / 1000)) : 0));
  const card = useProblemSlot();
  const setError = card.set;
  const waiting = waitingSince !== null;
  // a reply written on request arrives from the drafting worker: poll while it runs, show
  // "still writing" with elapsed time, and stop polling the moment the reply arrives or the job ends
  useEffect(() => {
    if (waitingSince === null) return;
    setElapsedSec(Math.max(0, Math.floor((Date.now() - waitingSince) / 1000)));
    const tick = setInterval(() => setElapsedSec(Math.max(0, Math.floor((Date.now() - waitingSince) / 1000))), 1000);
    const poll = setInterval(onChanged, 4000);
    return () => {
      clearInterval(tick);
      clearInterval(poll);
    };
  }, [waitingSince, onChanged]);
  useEffect(() => {
    if (item.plan_status === "working") {
      setSawWorking(true);
      if (waitingSince === null) setWaitingSince(startedAtMs(item.requested_at));
      return;
    }
    const baseBody = baseline?.body ?? text;
    if (item.body && item.body !== baseBody) {
      setText(item.body);
      setWaitingSince(null);
      setSawWorking(false);
      setBaseline(null);
      setNote(null);
      return;
    }
    const planMoved = baseline !== null && Boolean(item.plan_at) && item.plan_at !== baseline.planAt;
    if (waitingSince !== null && (sawWorking || planMoved)) {
      setWaitingSince(null);
      setSawWorking(false);
      setBaseline(null);
      if (item.plan_status === "failed" || item.plan_reason) {
        const why = item.plan_reason ? ` (${item.plan_reason.split(":", 1)[0]})` : "";
        setNote(`Could not write the reply${why}. Try again.`);
      } else {
        setNote("Finished without a new draft. Check the card or try different words.");
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.body, item.plan_status, item.plan_at, item.plan_reason, item.requested_at]);
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
      const written = out.decision?.text ?? "";
      setOpen(null);
      setLine("");
      if (written) {
        setText(written);
        setEditing(true);
        setWaitingSince(null);
        setSawWorking(false);
        setBaseline(null);
      } else {
        setBaseline({ body: item.body, planAt: item.plan_at ?? null });
        setSawWorking(item.plan_status === "working");
        setWaitingSince(Date.now());
        setElapsedSec(0);
        setNote(null);
      }
      onChanged();
    });
  const confirm = () =>
    run(async () => {
      const out = await decideAutopilotItem(item.item_id, { decision: "accept", ...(text !== item.body ? { text } : {}), ...(token ? { confirm_token: token } : {}) });
      if (out.confirm_token) {
        setToken(out.confirm_token);
        return;
      }
      if (out.decision?.sent) {
        if (onOptimistic) onOptimistic(item.item_id, "Sent");
        else onGone(item.item_id);
      } else if (out.state === "waiting") {
        setNote("Not sent: the send was refused or failed. It stays here to confirm again.");
      } else {
        setNote("Confirmed. Nothing was sent: sending isn't switched on for this yet.");
        onFeedback?.("Confirmed");
      }
    });
  const copyReply = () =>
    run(async () => {
      await navigator.clipboard.writeText(text);
      setNote("Copied to clipboard.");
      onFeedback?.("Copied reply to clipboard");
    });
  const keep = () =>
    run(async () => {
      await saveMemory({ kind: "preference", text: pref.trim() });
      setPref("");
      setOpen(null);
      setNote("Saved as your preference: your next drafts follow it. Change or remove it in Memory.");
      onFeedback?.("Saved as your preference");
    });
  const haveReply = text.trim() !== "";
  const versions = (item.options ?? []).filter((o) => o.reply.trim() && o.reply !== text);
  const entries: MenuEntry[] = [];
  if (haveReply && item.redo) entries.push({ label: "Rewrite from a line", run: () => setOpen("rewrite") });
  if (haveReply && !detail) {
    entries.push({ label: "Copy", run: () => copyReply() });
  }
  if (haveReply && item.channel === "mail" && item.to.length > 0) entries.push({ label: "Open in Outlook", run: () => void (window.location.href = mailtoOf(item, text)) });
  entries.push({ label: "Save a preference for my drafts", run: () => setOpen("pref") });
  const parsedNote = noteParts(item.note ?? "");
  const [dedupedAsk, dedupedMessage, dedupedNoteText] = saidOnce([
    !haveReply ? item.ask ?? "" : "",
    item.message ?? "",
    parsedNote.text,
  ]);
  const showSubject =
    Boolean(item.subject) &&
    !sameWords(item.subject, dedupedMessage) &&
    !sameWords(item.subject, dedupedAsk);

  if (detail) {
    const senderInfo = formatSender(item.sender);
    const headline = cleanHeadline(item.subject || item.summary || item.ask || senderInfo.name || "Prepared reply");
    const statusTone = waiting || item.plan_status === "working" ? "new" : haveReply ? "ok" : "risk";
    const statusText = waiting || item.plan_status === "working" ? "Drafting" : haveReply ? "Ready" : "Needs input";
    const ChannelIcon = item.channel === "teams" ? MessageSquare : Mail;
    const deliverableLabel = item.channel === "teams" ? "Teams reply draft" : "Email reply draft";

    return (
      <li
        id={`prepared-${item.item_id}`}
        className={cn("w-full space-y-6", linked && "border-l-2 border-l-accent pl-4")}
        data-testid="prepared-item"
      >
        {card.provide(
          <>
            <header className="space-y-3 border-b border-border pb-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <h2 className="min-w-0 flex-1 text-[20px] font-semibold leading-[1.3] tracking-[-0.02em] text-fg">
                  {countedWords(headline)}
                </h2>
                <div className="shrink-0">
                  <ItemActions
                    item={item}
                    onGone={onGone}
                    onChanged={onChanged}
                    onOptimistic={onOptimistic}
                    onRestore={onRestore}
                    onFeedback={onFeedback}
                    busy={busy}
                    entries={entries}
                    menuAlign="right"
                    primary={
                      haveReply ? (
                        <>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void confirm()}
                            className="inline-flex h-8 items-center rounded-md bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-all hover:opacity-90 active:scale-[0.98] disabled:opacity-50 cursor-pointer"
                          >
                            {busy ? "Sending..." : token ? "Confirm again to send" : "Approve & Send"}
                          </button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 rounded-md px-2.5 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
                            disabled={busy}
                            onClick={() => setEditing((v) => !v)}
                          >
                            <Pencil className="size-3.5" aria-hidden />
                            <span>{editing ? "Done editing" : "Edit"}</span>
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="h-8 rounded-md px-2.5 text-[13px] text-muted hover:bg-surface-2 hover:text-fg"
                            disabled={busy}
                            onClick={() => void copyReply()}
                          >
                            <Copy className="size-3.5" aria-hidden />
                            <span>Copy</span>
                          </Button>
                        </>
                      ) : null
                    }
                  />
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted">
                <StatusIndicator tone={statusTone} label={statusText} />
                {senderInfo.name && (
                  <>
                    <span className="font-medium text-fg" title={senderInfo.email || item.sender || undefined}>
                      {senderInfo.name}
                    </span>
                    {senderInfo.email && senderInfo.email !== senderInfo.name && (
                      <span className="text-[12px] text-faint">{senderInfo.email}</span>
                    )}
                  </>
                )}
                {parsedNote.labels.map((l) => (
                  <span key={l} className="text-[12px] text-faint" data-testid="prepared-label">
                    {l}
                  </span>
                ))}
                <span className="num ml-auto text-[12px] text-faint">{formatDateTime(item.created_at)}</span>
              </div>
            </header>

            {(dedupedMessage || (!haveReply && item.facts.length > 0)) && (
              <section className="space-y-2.5">
                <h3 className="text-[12px] font-medium uppercase tracking-[0.04em] text-faint">What happened</h3>
                {dedupedMessage && (
                  <p className="whitespace-pre-wrap text-[14.5px] leading-[1.65] text-fg/90">
                    {countedWords(dedupedMessage)}
                  </p>
                )}
                {!haveReply && item.facts.length > 0 && (
                  <ul className="list-disc space-y-1 pl-5 text-[14px] leading-[1.6] text-fg/85">
                    {item.facts.map((f) => (
                      <li key={f}>{countedWords(f)}</li>
                    ))}
                  </ul>
                )}
              </section>
            )}

            {!haveReply && (
              <section className="space-y-4 border-t border-border pt-5">
                {dedupedAsk && (
                  <p className="text-[15px] font-medium leading-[1.55] text-fg">{countedWords(dedupedAsk)}</p>
                )}
                {(item.options ?? []).length > 0 && (
                  <div className="flex flex-wrap gap-2" aria-label="Answers">
                    {(item.options ?? []).map((o) => (
                      <Button
                        key={o.reply}
                        size="sm"
                        variant="outline"
                        className="rounded-md"
                        disabled={busy}
                        onClick={() => setText(o.reply)}
                      >
                        {o.label || "This one"}
                      </Button>
                    ))}
                  </div>
                )}
                <div className="space-y-2.5">
                  <label className="block text-[12.5px] font-medium text-muted">
                    {(item.options ?? []).length > 0 ? "Or say it in a few words" : "What do you want to say?"}
                    <input
                      value={line}
                      maxLength={1000}
                      onChange={(e) => setLine(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && line.trim() && !busy && !waiting) {
                          e.preventDefault();
                          void write();
                        }
                      }}
                      className="mt-2 h-9 w-full rounded-md border border-border-strong bg-surface px-3 text-[14px] text-fg focus:border-accent/60 focus:outline-none"
                    />
                  </label>
                  <button
                    type="button"
                    disabled={busy || waiting || !line.trim()}
                    onClick={() => void write()}
                    className="inline-flex h-8 items-center rounded-md bg-accent px-3.5 text-[13px] font-medium text-accent-fg transition-all hover:opacity-90 active:scale-[0.98] disabled:opacity-40 cursor-pointer"
                  >
                    {waiting ? "Still writing..." : "Write the reply"}
                  </button>
                </div>
              </section>
            )}

            {haveReply && (
              <section className="space-y-3 border-t border-border pt-5">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="inline-flex items-center gap-2 text-[12px] font-medium uppercase tracking-[0.04em] text-faint">
                    <ChannelIcon className="size-3.5" aria-hidden />
                    <span>{deliverableLabel}</span>
                  </h3>
                  {versions.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1" aria-label="Other versions">
                      <span className="mr-1 text-[12px] text-muted">Other versions:</span>
                      {versions.map((o) => (
                        <Button
                          key={o.reply}
                          size="xs"
                          variant="ghost"
                          className="rounded-md text-muted hover:bg-surface-2 hover:text-fg"
                          disabled={busy}
                          onClick={() => setText(o.reply)}
                        >
                          {o.label || "Another version"}
                        </Button>
                      ))}
                    </div>
                  )}
                </div>

                <div className="space-y-3 border-l-2 border-border-strong pl-4 py-1">
                  {(item.to.length > 0 || item.cc.length > 0 || showSubject) && (
                    <div className="space-y-1 border-b border-border pb-2.5 text-[13px]">
                      {item.to.length > 0 && (
                        <div className="flex items-baseline gap-3">
                          <span className="w-14 shrink-0 text-[12px] font-medium text-faint">To</span>
                          <span className="min-w-0 flex-1 break-words text-fg">{item.to.join(", ")}</span>
                        </div>
                      )}
                      {item.cc.length > 0 && (
                        <div className="flex items-baseline gap-3">
                          <span className="w-14 shrink-0 text-[12px] font-medium text-faint">CC</span>
                          <span className="min-w-0 flex-1 break-words text-muted">{item.cc.join(", ")}</span>
                        </div>
                      )}
                      {showSubject && (
                        <div className="flex items-baseline gap-3">
                          <span className="w-14 shrink-0 text-[12px] font-medium text-faint">Subject</span>
                          <span className="min-w-0 flex-1 font-medium text-fg">{item.subject}</span>
                        </div>
                      )}
                    </div>
                  )}

                  {editing ? (
                    <textarea
                      aria-label="Reply"
                      value={text}
                      onChange={(e) => setText(e.target.value)}
                      rows={8}
                      className="w-full resize-none rounded-md border border-border-strong bg-surface p-3 text-[14.5px] leading-[1.65] text-fg focus:border-accent/60 focus:outline-none"
                    />
                  ) : (
                    <p className="whitespace-pre-wrap text-[14.5px] leading-[1.65] text-fg">{text}</p>
                  )}
                </div>

                {open === "rewrite" && (
                  <div className="flex flex-wrap items-end gap-2 border-t border-border pt-3.5">
                    <label className="block min-w-[16rem] flex-1 text-[12.5px] font-medium text-muted">
                      Rewrite it: what do you want to say?
                      <input
                        autoFocus
                        value={line}
                        maxLength={1000}
                        onChange={(e) => setLine(e.target.value)}
                        className="mt-1.5 h-9 w-full rounded-md border border-border-strong bg-surface px-3 text-[13.5px] text-fg focus:border-accent/60 focus:outline-none"
                      />
                    </label>
                    <Button size="sm" variant="outline" className="h-9 rounded-md" disabled={busy || waiting || !line.trim()} onClick={() => void write()}>
                      {waiting ? "Still writing..." : "Rewrite"}
                    </Button>
                  </div>
                )}
              </section>
            )}

            {open === "pref" && (
              <div className="flex flex-wrap items-end gap-2 border-t border-border pt-3.5">
                <label className="min-w-[16rem] flex-1 text-[12.5px] font-medium text-muted">
                  Save as a preference (for example: never mention prices to Kim)
                  <input
                    autoFocus
                    value={pref}
                    maxLength={1000}
                    onChange={(e) => setPref(e.target.value)}
                    className="mt-1.5 h-9 w-full rounded-md border border-border-strong bg-surface px-3 text-[13.5px] text-fg focus:border-accent/60 focus:outline-none"
                  />
                </label>
                <Button size="sm" variant="outline" className="h-9 rounded-md" disabled={busy || !pref.trim()} onClick={() => void keep()}>
                  Save
                </Button>
              </div>
            )}

            <TodoSteps todos={item.todos ?? []} />
            <DraftUse use={draftUseOf(item)} />

            {item.auto_would && (
              <p className="text-[12.5px] text-muted" data-testid="auto-would">
                {item.auto_would === "send" ? "With Auto on, this would go by itself." : "With Auto on, this would still wait for you."}
              </p>
            )}
            {(item.learned ?? []).length > 0 && (
              <p className="text-[12.5px] text-muted" data-testid="draft-learned">
                Learned from your edit: {(item.learned ?? []).join("; ")}
              </p>
            )}
            {waiting && (
              <p className="text-[13px] text-muted" role="status" data-testid="reply-writing-status">
                Writing the reply from your line. Still writing ({formatElapsed(elapsedSec)}). It shows here when it is ready.
              </p>
            )}
            {!waiting && note && <p className="text-[13px] text-muted">{note}</p>}
            {dedupedNoteText && (
              <p className="text-[13px] text-muted" data-testid="prepared-note">
                {dedupedNoteText}
              </p>
            )}
            {feedback && (
              <div className="border-t border-border pt-4" data-testid="prepared-feedback-row">
                {feedback}
              </div>
            )}
          </>,
        )}
        {card.banner}
      </li>
    );
  }

  return (
    <li
      id={`prepared-${item.item_id}`}
      className={cn(
        "w-full space-y-3 py-1 transition-colors",
        linked && "border-l-2 border-l-accent pl-3",
      )}
      data-testid="prepared-item"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[14px] font-semibold text-fg" title={item.sender || undefined}>
          {senderLabel(item.sender) || "Someone"}
        </span>
        {showSubject && (
          <>
            <span className="text-muted/60" aria-hidden>·</span>
            <span className="max-w-full truncate text-[13px] text-muted">
              {item.subject}
            </span>
          </>
        )}
        {parsedNote.labels.map((l) => (
          <Fragment key={l}>
            <span className="text-muted/60" aria-hidden>·</span>
            <p className="text-[12px] leading-4 text-muted" data-testid="prepared-label">
              {l}
            </p>
          </Fragment>
        ))}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {dedupedMessage && (
        <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 text-[13.5px] leading-relaxed text-muted">
          {countedWords(dedupedMessage)}
        </p>
      )}
      {!haveReply && (
        <div className="space-y-2.5 border-t border-border/70 pt-3.5">
          {dedupedAsk && <p className="text-[14.5px] font-medium leading-[1.6] text-fg">{countedWords(dedupedAsk)}</p>}
          {item.facts.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-4 text-[13.5px] leading-relaxed text-muted">
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
          <label className="block text-[12.5px] text-muted">
            {(item.options ?? []).length > 0 ? "Or say it in a few words" : "What do you want to say?"}
            <input value={line} maxLength={1000} onChange={(e) => setLine(e.target.value)} className="mt-1 h-8 w-full rounded-md border border-border bg-bg px-2.5 text-[13.5px] text-fg" />
          </label>
          <Button size="sm" disabled={busy || waiting || !line.trim()} onClick={() => void write()}>
            {waiting ? "Still writing..." : "Write the reply"}
          </Button>
        </div>
      )}
      {haveReply && (
        <>
          {editing ? (
            <textarea aria-label="Reply" value={text} onChange={(e) => setText(e.target.value)} rows={8} className="w-full resize-none rounded-lg border border-border-strong bg-bg p-3 text-[14.5px] leading-[1.65] text-fg focus:border-fg/35 focus:outline-none" />
          ) : (
            <p className="whitespace-pre-wrap border-t border-border/70 pt-3.5 text-[14.5px] leading-[1.65] text-fg">{text}</p>
          )}
          {versions.length > 0 && (
            <div className="flex flex-wrap items-center gap-2" aria-label="Other versions">
              <span className="text-[12px] text-muted">Other versions:</span>
              {versions.map((o) => (
                <Button key={o.reply} size="sm" variant="ghost" disabled={busy} onClick={() => setText(o.reply)}>
                  {o.label || "Another version"}
                </Button>
              ))}
            </div>
          )}
          {open === "rewrite" && (
            <div className="flex flex-wrap items-end gap-2">
              <label className="block min-w-[16rem] flex-1 text-[12.5px] text-muted">
                Rewrite it: what do you want to say?
                <input autoFocus value={line} maxLength={1000} onChange={(e) => setLine(e.target.value)} className="mt-1 h-8 w-full rounded-md border border-border bg-bg px-2.5 text-[13.5px] text-fg" />
              </label>
              <Button size="sm" variant="outline" disabled={busy || waiting || !line.trim()} onClick={() => void write()}>
                {waiting ? "Still writing..." : "Rewrite"}
              </Button>
            </div>
          )}
        </>
      )}
      {open === "pref" && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="min-w-[16rem] flex-1 text-[12.5px] text-muted">
            Save as a preference (for example: never mention prices to Kim)
            <input autoFocus value={pref} maxLength={1000} onChange={(e) => setPref(e.target.value)} className="mt-1 h-8 w-full rounded-md border border-border bg-bg px-2.5 text-[13.5px] text-fg" />
          </label>
          <Button size="sm" variant="outline" disabled={busy || !pref.trim()} onClick={() => void keep()}>
            Save
          </Button>
        </div>
      )}
      <TodoSteps todos={item.todos ?? []} />
      <DraftUse use={draftUseOf(item)} />
      {item.auto_would && (
        <p className="text-[12.5px] text-muted" data-testid="auto-would">
          {item.auto_would === "send" ? "With Auto on, this would go by itself." : "With Auto on, this would still wait for you."}
        </p>
      )}
      {(item.learned ?? []).length > 0 && (
        <p className="text-[12.5px] text-muted" data-testid="draft-learned">
          Learned from your edit: {(item.learned ?? []).join("; ")}
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
        <div className="min-w-0">
          {card.provide(<ItemActions
            item={item}
            onGone={onGone}
            onChanged={onChanged}
            onOptimistic={onOptimistic}
            onRestore={onRestore}
            onFeedback={onFeedback}
            busy={busy}
            entries={entries}
            primary={
              haveReply ? (
                <>
                  <Button size="sm" disabled={busy} onClick={() => void confirm()}>
                    {busy ? "Confirming..." : token ? "Confirm again to send" : "Confirm"}
                  </Button>
                  <Button size="sm" variant="ghost" className="text-muted hover:bg-surface-2 hover:text-fg" disabled={busy} onClick={() => setEditing((v) => !v)}>
                    {editing ? "Done editing" : "Edit"}
                  </Button>
                </>
              ) : null
            }
          />)}
        </div>
        {feedback && <div className="shrink-0" data-testid="prepared-feedback-row">{feedback}</div>}
      </div>
      {waiting && (
        <p className="text-[12.5px] text-muted" role="status" data-testid="reply-writing-status">
          Writing the reply from your line. Still writing ({formatElapsed(elapsedSec)}). It shows here when it is ready.
        </p>
      )}
      {!waiting && note && <p className="text-[12.5px] text-muted">{note}</p>}
      {dedupedNoteText && <p className="text-[12.5px] text-muted" data-testid="prepared-note">{dedupedNoteText}</p>}
      {card.banner}
    </li>
  );
}

/** Cards drawn at first: the list is all there (its count, every item one click away). Drawing
 * all of an owner's ~1,100 at once made Home 15,000 elements and a 0.75 s block. */
export const FIRST_CARDS = 40;

export function PreparedView({ items, empty, linked, onGone, onChanged }: { items: PreparedItem[]; empty: string; linked?: string | null; onGone: (id: string) => void; onChanged: () => void }) {
  const [shown, setShown] = useState(FIRST_CARDS);
  if (items.length === 0) return <p className="py-6 text-[13.5px] text-muted" data-testid="prepared-empty">{empty}</p>;
  // a linked item (a deep link, a notification) is always drawn, wherever it sits in the list
  const at = linked ? items.findIndex((i) => i.item_id === linked) : -1;
  const count = Math.max(shown, at + 1);
  const drawn = items.length > count ? items.slice(0, count) : items;
  return (
    <>
      <ul className="divide-y divide-border [&>li]:py-4 [&>li:first-child]:pt-1" aria-label="Prepared for me">
        {drawn.map((i) => {
          const fb = <PreparedFeedback item={i} />;
          return (
            <Fragment key={i.item_id}>
              {i.kind === "job" ? (
                <JobCard item={i} linked={i.item_id === linked} onGone={onGone} feedback={fb} />
              ) : i.kind === "todo" ? (
                <TodoCard item={i} linked={i.item_id === linked} onGone={onGone} onChanged={onChanged} feedback={fb} />
              ) : i.kind === "alert" ? (
                <AlertCard item={i} linked={i.item_id === linked} onGone={onGone} onChanged={onChanged} feedback={fb} />
              ) : (
                <PreparedCard item={i} linked={i.item_id === linked} onGone={onGone} onChanged={onChanged} feedback={fb} />
              )}
            </Fragment>
          );
        })}
      </ul>
      {drawn.length < items.length && (
        <p className="flex items-center gap-2 py-2 text-[12.5px] text-muted" data-testid="more-cards">
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
        <h2 id="ap-waiting" className="text-[13.5px] font-semibold text-fg">
          Waiting now, per person
        </h2>
        {queue.length === 0 ? (
          <p className="text-[13.5px] text-muted">Nothing is waiting on anyone.</p>
        ) : (
          <ul className="divide-y divide-border" data-testid="ap-waiting">
            {queue.map(([p, n]) => (
              <li key={p} className="flex gap-3 py-2 text-[13.5px]">
                <span className="text-fg">{personName(area, p)}</span>
                <span className="text-muted">{n} waiting</span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="ap-who" className="space-y-2">
        <h2 id="ap-who" className="text-[13.5px] font-semibold text-fg">
          Who switched what on
        </h2>
        {who.length === 0 ? (
          <p className="text-[13.5px] text-muted">Nobody has switched anything on.</p>
        ) : (
          // one line per person (their workflows in it), never the same name once per workflow
          <ul className="divide-y divide-border" data-testid="ap-who">
            {[...new Set(who.map((w) => w.person))].map((person) => {
              const theirs = who.filter((w) => w.person === person);
              // "set by the owner" once when it holds for every one of them
              const allOwner = theirs.every((w) => w.by_owner);
              return (
                <li key={person} className="flex gap-3 py-2 text-[13.5px]">
                  <span className="shrink-0 text-fg">{personName(area, person)}</span>
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
          <h2 id="ap-score" className="text-[13.5px] font-semibold text-fg">
            How each one is doing
          </h2>
          <ul className="divide-y divide-border">
            {by.map(([name, c]) => {
              const s = scoreOf(c);
              return (
                <li key={name} className="flex flex-wrap gap-x-4 py-2 text-[13.5px]">
                  <span className="min-w-[14rem] text-fg">{workflowLabel(name)}</span>
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
