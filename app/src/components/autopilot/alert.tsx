"use client";

// Something that happened and waits on the person but has nothing to prepare (an approval
// asked of them, a QC hold, a Mission Control question): what happened, why, where it is
// decided and its records. It is decided in its own system; here it is only set aside, or
// passed to a colleague (on their list too, under their own access). An item no record
// names a person for is on the whole role's list (pooled) until one of them takes it.

import { Fragment, useEffect, useState } from "react";
import { ItemActions, type OptimisticCallbacks } from "@/components/autopilot/item-actions";
import { StatusIndicator } from "@/components/autopilot/jobs";
import { cleanHeadline, noteParts, saidOnce } from "@/components/autopilot/lines";
import { Button } from "@/components/ui/button";
import { StyledSelect } from "@/components/ui/controls";
import { useProblem, useProblemSlot } from "@/components/ui/notice";
import { getAutopilotColleagues, handAutopilotItem, setFollowing, takeAutopilotItem, type PreparedItem } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { countedWords, formatDateTime, objectHref } from "@/lib/format";
import { cn } from "@/lib/cn";

/** Ensure any `/object/...` link carries its explicit `<Type>` segment (`/object/<Type>/<id>`). */
export function canonicalObjectHref(href: string): string {
  const m = /^\/object\/([^/?#]+)\/?([?#].*)?$/.exec(href);
  if (!m) return href;
  let decoded = m[1];
  try {
    decoded = decodeURIComponent(m[1]);
  } catch {
    return href;
  }
  const sep = decoded.indexOf(":");
  if (sep <= 0) return href;
  return `${objectHref(decoded.slice(0, sep), decoded)}${m[2] ?? ""}`;
}

/** Extract the raw `object_id` from `/object/<Type>/<id>` or legacy `/object/<id>` for Follow. */
export function objectIdFromHref(href: string): string | null {
  if (!href.startsWith("/object/")) return null;
  const pathOnly = href.slice("/object/".length).split(/[?#]/, 1)[0].replace(/\/+$/, "");
  if (!pathOnly) return null;
  const parts = pathOnly.split("/");
  const raw = parts.length >= 2 ? parts.slice(1).join("/") : parts[0];
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/** Structured key/value pair parsed from a QC hold alert's facts or prose so lot, product,
 *  status, and affected orders render in a clean 2-column grid instead of a prose dump. */
export interface StructuredFact {
  label: string;
  value: string;
}

const QC_FIELD_LABELS: [string, RegExp][] = [
  ["Lot", /\b(?:lot|batch)(?:\s*(?:no\.?|number|#))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]{1,32})/i],
  ["Product", /\b(?:product|item|sku|material)\s*[:\-]\s*([^.;\n|]+?)(?=\s*(?:[.;\n|]|affected|orders|reason|status|held|$))/i],
  ["Affected orders", /\b(?:affected\s+orders?|orders?\s+affected|holds?\s+up|impacts?)\s*[:\-]?\s*([^.;\n|]+)/i],
  ["Customer", /\b(?:customer|account)\s*[:\-]\s*([^.;\n|]+)/i],
  ["Status", /\b(?:hold\s+status|qc\s+status|status)\s*[:\-]\s*([^.;\n|]+)/i],
  ["Reason", /\b(?:reason|issue|finding)\s*[:\-]\s*([^.;\n|]+)/i],
  ["Quantity", /\b(?:qty|quantity)\s*[:\-]\s*([^.;\n|]+)/i],
];

export function extractQcFacts(item: PreparedItem, summary: string, extra: string): {
  structured: StructuredFact[];
  remainingFacts: string[];
} {
  const isQc =
    item.alert_kind === "qc_hold" ||
    /\b(?:qc|quality)\s+hold\b/i.test(`${item.subject ?? ""} ${summary} ${extra}`);
  if (!isQc) return { structured: [], remainingFacts: item.facts };

  const structured: StructuredFact[] = [];
  const seen = new Set<string>();
  const remainingFacts: string[] = [];

  const pushFact = (rawLabel: string, rawValue: string) => {
    const label = rawLabel.trim();
    const value = countedWords(rawValue.trim());
    const key = label.toLowerCase();
    if (!label || !value || seen.has(key)) return;
    seen.add(key);
    structured.push({ label, value });
  };

  for (const fact of item.facts ?? []) {
    const kv = /^([A-Za-z][A-Za-z0-9 /&()-]{1,32}):\s*(.+)$/.exec(fact.trim());
    if (kv) {
      pushFact(kv[1], kv[2]);
    } else {
      remainingFacts.push(fact);
    }
  }

  // Also parse structured QC fields from summary/extra prose when not already covered by explicit facts.
  const prose = [summary, extra, ...remainingFacts].filter(Boolean).join("\n");
  for (const [label, re] of QC_FIELD_LABELS) {
    if (seen.has(label.toLowerCase())) continue;
    const m = re.exec(prose);
    if (m?.[1]) {
      pushFact(label, m[1].replace(/[.,;\s]+$/, ""));
    }
  }

  // Only show a structured 2-column grid when there are at least 2 distinct fields (or explicit key:value facts).
  if (structured.length < 2 && remainingFacts.length === (item.facts ?? []).length) {
    return { structured: [], remainingFacts: item.facts };
  }
  return { structured, remainingFacts };
}

function QcFactGrid({ facts }: { facts: StructuredFact[] }) {
  if (facts.length === 0) return null;
  return (
    <dl
      className="grid grid-cols-1 gap-x-6 gap-y-2.5 border-y border-border/70 py-3 sm:grid-cols-2"
      data-testid="qc-fact-grid"
    >
      {facts.map((f) => (
        <div key={f.label} className="min-w-0">
          <dt className="text-[11px] font-medium uppercase tracking-[0.04em] text-faint">{f.label}</dt>
          <dd className="mt-0.5 break-words text-[13.5px] font-medium leading-5 text-fg tabular-nums">{f.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function AlertCard({
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
  onChanged?: () => void;
  feedback?: React.ReactNode;
} & OptimisticCallbacks) {
  const [passing, setPassing] = useState(false);
  const card = useProblemSlot();
  const parsedNote = noteParts(item.note ?? "");
  const [summary, extra, noteText] = saidOnce([countedWords(item.summary ?? ""), item.message ?? "", parsedNote.text]);
  const mc = item.alert_kind === "mc_approval_pending";
  const qc = extractQcFacts(item, summary, extra);

  if (detail) {
    const headline = cleanHeadline(item.subject || summary || "Alert");
    return (
      <li
        id={`prepared-${item.item_id}`}
        className={cn("w-full space-y-6", linked && "border-l-2 border-l-accent pl-4")}
        data-testid="prepared-alert"
      >
        {card.provide(
          <>
            <header className="space-y-3 border-b border-border pb-5">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <h2 className="min-w-0 flex-1 text-[20px] font-semibold leading-[1.3] tracking-[-0.02em] text-fg">
                  {headline}
                </h2>
                <div className="shrink-0">
                  {item.pooled ? (
                    <Take
                      itemId={item.item_id}
                      onTaken={() => {
                        onFeedback?.("Added to your list");
                        if (onChanged) onChanged();
                        else onGone(item.item_id);
                      }}
                    />
                  ) : (
                    <ItemActions
                      item={item}
                      onGone={onGone}
                      onOptimistic={onOptimistic}
                      onRestore={onRestore}
                      onFeedback={onFeedback}
                      menuAlign="right"
                      setAsideOnly={!!item.handed_from}
                      entries={item.handed_from ? [] : [{ label: "Pass to a colleague", run: () => setPassing(true) }]}
                    />
                  )}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted">
                <StatusIndicator tone="late" label="Alert" />
                {parsedNote.labels.map((l) => (
                  <span key={l} className="text-[12px] text-faint" data-testid="prepared-label">
                    {l}
                  </span>
                ))}
                <span className="num ml-auto text-[12px] text-faint">{formatDateTime(item.created_at)}</span>
              </div>
            </header>

            <section className="space-y-3">
              <h3 className="text-[12px] font-medium uppercase tracking-[0.04em] text-faint">What happened</h3>
              {summary && (
                <p className="text-[14.5px] leading-[1.65] text-fg" data-testid={mc ? "alert-line" : undefined}>
                  {summary}
                </p>
              )}
              <QcFactGrid facts={qc.structured} />
              {extra && (
                <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3.5 text-[14px] leading-[1.65] text-fg/85">
                  {extra}
                </p>
              )}
              {qc.remainingFacts.length > 0 && (
                <ul className="list-disc space-y-1 pl-5 text-[14px] leading-[1.6] text-fg/90">
                  {qc.remainingFacts.map((f, n) => (
                    <li key={n}>{countedWords(f)}</li>
                  ))}
                </ul>
              )}
              {noteText && (
                <p className="text-[13px] text-muted" data-testid="prepared-note">
                  {countedWords(noteText)}
                </p>
              )}
            </section>

            {(item.links ?? []).length > 0 && (
              <div className="flex flex-wrap gap-3 border-t border-border pt-4 text-[13px]">
                {(item.links ?? []).map((l) => {
                  const href = canonicalObjectHref(l.href);
                  const followId = objectIdFromHref(href);
                  return (
                    <span key={l.href} className="inline-flex items-baseline gap-2">
                      <a
                        href={href}
                        {...(href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })}
                        className="inline-flex h-8 items-center rounded-md border border-border-strong bg-surface px-3 text-[12.5px] font-medium text-fg transition-colors hover:bg-surface-2"
                      >
                        {l.label}
                      </a>
                      {followId && <Follow objectId={followId} label={l.label} />}
                    </span>
                  );
                })}
              </div>
            )}

            {item.handed_from && (
              <p className="text-[12.5px] text-muted" data-testid="alert-handed-from">
                Passed to you by {item.handed_from}. Setting it aside clears it for them too.
              </p>
            )}
            {!item.pooled && (
              <PassOn itemId={item.item_id} handedTo={item.handed_to} open={passing} onClose={() => setPassing(false)} onFeedback={onFeedback} />
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
      data-testid="prepared-alert"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[14px] font-semibold text-fg">{item.subject || "From the records"}</span>
        {parsedNote.labels.map((l) => (
          <Fragment key={l}>
            <span className="text-faint" aria-hidden>·</span>
            <p className="text-[11.5px] leading-4 text-faint" data-testid="prepared-label">
              {l}
            </p>
          </Fragment>
        ))}
        <span className="num ml-auto text-[12px] text-faint">{formatDateTime(item.created_at)}</span>
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
          <QcFactGrid facts={qc.structured} />
          {extra && <p className="whitespace-pre-wrap border-l-2 border-border-strong pl-3 text-[13px] leading-relaxed text-muted">{extra}</p>}
          {qc.remainingFacts.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-4 text-[13px] leading-relaxed text-muted">
              {qc.remainingFacts.map((f, n) => (
                <li key={n}>{countedWords(f)}</li>
              ))}
            </ul>
          )}
          {noteText && <p className="text-[12px] text-muted" data-testid="prepared-note">{countedWords(noteText)}</p>}
        </>
      )}
      {(item.links ?? []).length > 0 && (
        <div className="flex flex-wrap gap-3 text-[13px]">
          {(item.links ?? []).map((l) => {
            const href = canonicalObjectHref(l.href);
            const followId = objectIdFromHref(href);
            return (
              <span key={l.href} className="inline-flex items-baseline gap-2">
                <a href={href} {...(href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })} className="inline-flex h-7 items-center rounded-md border border-border bg-surface px-2.5 text-[12.5px] font-medium text-fg transition-colors hover:border-border-strong hover:bg-surface-2">
                  {l.label}
                </a>
                {followId && <Follow objectId={followId} label={l.label} />}
              </span>
            );
          })}
        </div>
      )}
      {item.handed_from && <p className="text-[12px] text-muted" data-testid="alert-handed-from">Passed to you by {item.handed_from}. Setting it aside clears it for them too.</p>}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
        <div className="min-w-0">
          {item.pooled ? (
            <Take
              itemId={item.item_id}
              onTaken={() => {
                onFeedback?.("Added to your list");
                if (onChanged) onChanged();
                else onGone(item.item_id);
              }}
            />
          ) : card.provide(
            <>
              <ItemActions
                item={item}
                onGone={onGone}
                onOptimistic={onOptimistic}
                onRestore={onRestore}
                onFeedback={onFeedback}
                setAsideOnly={!!item.handed_from}
                entries={item.handed_from ? [] : [{ label: "Pass to a colleague", run: () => setPassing(true) }]}
              />
              <PassOn itemId={item.item_id} handedTo={item.handed_to} open={passing} onClose={() => setPassing(false)} onFeedback={onFeedback} />
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
        {busy ? "Taking..." : "Take it"}
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

function PassOn({ itemId, handedTo, open, onClose, onFeedback }: { itemId: string; handedTo?: string; open: boolean; onClose: () => void; onFeedback?: (msg: string) => void }) {
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
      const name = people?.find((p) => p.key === pick)?.name ?? "a colleague";
      setTo(name);
      onFeedback?.(`Passed to ${name}`);
      onClose();
    });
  return (
    <div className="space-y-1">
      {to && <p className="text-[12px] text-muted" data-testid="alert-handed-to">Passed to {to}. If they set it aside, it leaves your list too.</p>}
      {open && people !== null && (people.length === 0 ? (
        <p className="text-[12px] text-muted">Nobody to pass it to.</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <StyledSelect
            ariaLabel="Colleague"
            size="md"
            value={pick}
            onChange={setPick}
            options={people.map((p) => ({ value: p.key, label: p.name }))}
          />
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
