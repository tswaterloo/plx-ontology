"use client";

// Something that happened and waits on the person but has nothing to prepare (an approval
// asked of them, a QC hold, a Mission Control question): what happened, why, where it is
// decided and its records. It is decided in its own system; here it is only set aside, or
// passed to a colleague (on their list too, under their own access). An item no record
// names a person for is on the whole role's list (pooled) until one of them takes it.

import { useEffect, useState } from "react";
import { ItemActions } from "@/components/autopilot/item-actions";
import { noteParts, saidOnce } from "@/components/autopilot/lines";
import { Button } from "@/components/ui/button";
import { useProblem, useProblemSlot } from "@/components/ui/notice";
import { getAutopilotColleagues, handAutopilotItem, setFollowing, takeAutopilotItem, type PreparedItem } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { countedWords, formatDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";

export function AlertCard({ item, linked, onGone, onChanged, feedback }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void; onChanged?: () => void; feedback?: React.ReactNode }) {
  const [passing, setPassing] = useState(false);
  const card = useProblemSlot();
  const parsedNote = noteParts(item.note ?? "");
  const [summary, extra, noteText] = saidOnce([countedWords(item.summary ?? ""), item.message ?? "", parsedNote.text]);
  const mc = item.alert_kind === "mc_approval_pending";
  return (
    <li
      id={`prepared-${item.item_id}`}
      className={cn(
        "max-w-[50rem] space-y-3 rounded-lg border border-border bg-surface/60 p-4 transition-colors",
        linked && "border-accent/70 ring-1 ring-accent/30",
      )}
      data-testid="prepared-alert"
    >
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <span className="text-[14px] font-semibold text-fg">{item.subject || "From the records"}</span>
        {parsedNote.labels.map((l) => (
          <p key={l} className="inline-flex items-center rounded-md bg-surface-2 px-2 py-0.5 text-[11px] leading-4 text-muted" data-testid="prepared-label">
            {l}
          </p>
        ))}
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      {mc ? (
        <>
          {/* a Mission Control approval is the agents' own text: one plain line, the rest on open */}
          <p className="truncate text-[14px] font-medium leading-relaxed text-fg" title={summary} data-testid="alert-line">{summary}</p>
          {(summary.length > 40 || extra || item.facts.length > 0 || noteText) && (
            <details className="rounded-md border border-border/70 bg-bg/70 px-3 py-2 text-[12px] text-muted" data-testid="alert-details">
              <summary className="cursor-pointer font-medium text-fg/90">Details</summary>
              <div className="mt-2 space-y-1.5 leading-relaxed">
                <p className="whitespace-pre-wrap break-words text-fg">{summary}</p>
                {extra && <p className="whitespace-pre-wrap break-words">{extra}</p>}
                {item.facts.map((f, n) => (
                  <p key={n}>{countedWords(f)}</p>
                ))}
                {noteText && <p>{countedWords(noteText)}</p>}
              </div>
            </details>
          )}
        </>
      ) : (
        <>
          {summary && <p className="text-[14px] font-medium leading-relaxed text-fg">{summary}</p>}
          {extra && <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 text-[13px] leading-relaxed text-muted">{extra}</p>}
          {item.facts.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-4 text-[13px] leading-relaxed text-muted">
              {item.facts.map((f, n) => (
                <li key={n}>{countedWords(f)}</li>
              ))}
            </ul>
          )}
          {noteText && <p className="text-[12px] text-muted" data-testid="prepared-note">{countedWords(noteText)}</p>}
        </>
      )}
      {(item.links ?? []).length > 0 && (
        <div className="flex flex-wrap gap-3 text-[13px]">
          {(item.links ?? []).map((l) => (
            <span key={l.href} className="inline-flex items-baseline gap-2">
              <a href={l.href} {...(l.href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })} className="text-accent underline-offset-2 hover:underline">
                {l.label}
              </a>
              {l.href.startsWith("/object/") && <Follow objectId={decodeURIComponent(l.href.slice("/object/".length))} label={l.label} />}
            </span>
          ))}
        </div>
      )}
      {item.handed_from && <p className="text-[12px] text-muted" data-testid="alert-handed-from">Passed to you by {item.handed_from}. Setting it aside clears it for them too.</p>}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
        <div className="min-w-0">
          {item.pooled ? (
            <Take itemId={item.item_id} onTaken={() => (onChanged ? onChanged() : onGone(item.item_id))} />
          ) : card.provide(
            <>
              <ItemActions item={item} onGone={onGone} setAsideOnly={!!item.handed_from} entries={item.handed_from ? [] : [{ label: "Pass to a colleague", run: () => setPassing(true) }]} />
              <PassOn itemId={item.item_id} handedTo={item.handed_to} open={passing} onClose={() => setPassing(false)} />
            </>,
          )}
        </div>
        {feedback && <div className="shrink-0" data-testid="prepared-feedback-row">{feedback}</div>}
      </div>
      {card.banner}
    </li>
  );
}

// Take a pooled item: it becomes yours alone, with its prepared work to check like any of
// your items; refused when a colleague took it first.
function Take({ itemId, onTaken }: { itemId: string; onTaken: () => void }) {
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
  const take = async () => {
    setBusy(true);
    setError(null);
    try {
      await takeAutopilotItem(itemId);
      onTaken();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-1">
      <Button size="sm" disabled={busy} onClick={() => void take()} data-testid="alert-take">
        Take it
      </Button>
      {problem}
    </div>
  );
}

// Follow a record the alert is about: told when it or a record linked to it changes (the
// watch's own follow, which checks the record opens for you); nothing is decided here.
function Follow({ objectId, label }: { objectId: string; label: string }) {
  const [state, setState] = useState<"idle" | "busy" | "on" | "error">("idle");
  if (state === "on") return <span className="text-[12px] text-muted">Following</span>;
  return (
    <button
      type="button"
      className="text-[12px] text-muted underline-offset-2 hover:underline disabled:opacity-50"
      aria-label={`Follow ${label}`}
      disabled={state === "busy"}
      onClick={() => {
        setState("busy");
        setFollowing(objectId, true).then(
          () => setState("on"),
          () => setState("error"),
        );
      }}
    >
      {state === "error" ? "Could not follow" : "Follow"}
    </button>
  );
}

function PassOn({ itemId, handedTo, open, onClose }: { itemId: string; handedTo?: string; open: boolean; onClose: () => void }) {
  const [people, setPeople] = useState<{ key: string; name: string }[] | null>(null);
  const [pick, setPick] = useState("");
  const [to, setTo] = useState(handedTo ?? "");
  const [busy, setBusy] = useState(false);
  const [, setError, problem] = useProblem();
  const run = async (act: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await act();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  // a closed picker leaves no problem of its own behind
  useEffect(() => {
    if (!open) setError(null);
  }, [open, setError]);
  useEffect(() => {
    if (!open || people !== null) return;
    void run(async () => {
      const got = (await getAutopilotColleagues()).people ?? [];
      setPeople(got);
      setPick(got[0]?.key ?? "");
    });
  }, [open, people]);
  const hand = () =>
    run(async () => {
      await handAutopilotItem(itemId, pick);
      setTo(people?.find((p) => p.key === pick)?.name ?? "a colleague");
      onClose();
    });
  return (
    <div className="space-y-1">
      {to && <p className="text-[12px] text-muted" data-testid="alert-handed-to">Passed to {to}. If they set it aside, it leaves your list too.</p>}
      {open && people !== null && (people.length === 0 ? (
        <p className="text-[12px] text-muted">Nobody to pass it to.</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <select className="h-8 border border-border bg-transparent px-2 text-[13px]" aria-label="Colleague" value={pick} onChange={(e) => setPick(e.target.value)}>
            {people.map((p) => (
              <option key={p.key} value={p.key}>
                {p.name}
              </option>
            ))}
          </select>
          <Button size="sm" disabled={busy || !pick} onClick={() => void hand()}>
            Pass on
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
        </div>
      ))}
      {problem}
    </div>
  );
}
