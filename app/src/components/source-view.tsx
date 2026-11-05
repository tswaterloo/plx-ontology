"use client";

import { useCallback, useEffect, useState } from "react";
import { getSource } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { fieldValue, formatDateTime, recordKind, SYSTEM_LABEL } from "@/lib/format";
import type { SourceView } from "@/lib/types";
import { curatedFields } from "./source-chip";
import { ErrorNotice } from "./ui/notice";
import { SkeletonLines } from "./ui/skeleton";

/** A mail field of the captured message ("row.from"), shown as a person reads it. */
export function mailField(fields: Record<string, string> | null | undefined, name: string): string {
  const v = fields?.[`row.${name}`] ?? fields?.[name];
  return v ? fieldValue(name, v) : "";
}

/** A mail address list as names/addresses ("a@x, b@y" or a JSON list). */
export function mailList(fields: Record<string, string> | null | undefined, name: string): string[] {
  const v = mailField(fields, name);
  return v
    .split(/\s*[,;]\s*/)
    .map((x) => x.trim())
    .filter(Boolean);
}

const RULE = /^\s*([_\-=*~])\1{9,}\s*$/;
const SIGN_OFF = /^\s*(?:regards|kind regards|best regards|warm regards|thanks|thank you|many thanks|best|cheers|sincerely|--)\b[ ,!.]*$/i;
const CONTACTISH = /@[\w.-]+\.\w|\+?\d[\d ().-]{7,}\d|^\s*[etmpfw]:\s|www\./i;

/**
 * A message body as a person reads it: inline-image placeholders ([cid:...]) go wherever
 * they sit, an address repeated after a name (<mailto:...>) goes, a link repeated after its
 * own text goes, a wrapped safe-link reads "(link)", and a line of 10+ _ - = becomes "—".
 * Display only: the cited text is unchanged on the server.
 */
export function readableBody(text: string): string {
  return text
    .replace(/\r\n?/g, "\n")
    .replace(/\[cid:[^\]]*\]/g, "")
    .replace(/<mailto:[^>\s]+>/g, "")
    .replace(/(\bhttps?:\/\/\S+?|\bwww\.\S+?)<https?:\/\/[^>\s]*>/g, "$1")
    .replace(/<https:\/\/[^>\s]*safelinks\.protection\.outlook\.com[^>\s]*>/g, " (link)")
    .replace(/[​‌‍﻿]/g, "")
    .split("\n")
    .map((l) => (RULE.test(l) ? "—" : l.replace(/[ \t]+$/, "")))
    .join("\n")
    .replace(/^\s*$\n(?=\s*$)/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** The body before the signature, and the signature (from a sign-off line, or a run of 3+ short contact lines). */
export function splitSignature(body: string): { main: string; signature: string } {
  const lines = body.split("\n");
  let cut = lines.findIndex((l, i) => i > 0 && SIGN_OFF.test(l));
  if (cut < 0) {
    for (let i = 1; i + 2 < lines.length; i++) {
      const run = lines.slice(i, i + 3);
      if (run.every((l) => l.trim().length > 0 && l.trim().length < 60) && run.some((l) => CONTACTISH.test(l))) {
        cut = i;
        break;
      }
    }
  }
  if (cut < 0) return { main: body, signature: "" };
  return { main: lines.slice(0, cut).join("\n").trim(), signature: lines.slice(cut).join("\n").trim() };
}

function People({ label, people, fold = 2 }: { label: string; people: string[]; fold?: number }) {
  const [open, setOpen] = useState(false);
  if (people.length === 0) return null;
  const shown = open || people.length <= fold ? people : people.slice(0, fold);
  return (
    <div className="contents">
      <dt className="text-meta text-muted">{label}</dt>
      <dd className="min-w-0 break-words text-row">
        {shown.join(", ")}
        {people.length > fold && (
          <button type="button" onClick={() => setOpen((v) => !v)} className="ml-1 text-meta text-accent hover:underline cursor-pointer" aria-expanded={open}>
            {open ? "show less" : `+${people.length - fold} more`}
          </button>
        )}
      </dd>
    </div>
  );
}

export function EmailHeader({ fields, asOf }: { fields: Record<string, string> | null | undefined; asOf?: string | null }) {
  const sentRaw = fields?.["row.sent_at"] ?? fields?.sent_at ?? fields?.["row.received_at"] ?? fields?.received_at ?? asOf ?? "";
  const sent = sentRaw ? formatDateTime(sentRaw) : "";
  const from = mailField(fields, "from");
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 border-y border-border bg-bg-subtle px-4 py-3" data-testid="email-header">
      <dt className="text-meta text-muted">From</dt>
      <dd className={cn("min-w-0 break-words text-row font-medium", !from && "font-normal text-muted")}>{from || "Sender not captured"}</dd>
      <People label="To" people={mailList(fields, "to")} fold={3} />
      <People label="Cc" people={mailList(fields, "cc")} />
      {sent && (
        <>
          <dt className="text-meta text-muted">Date</dt>
          <dd className="text-row">{sent}</dd>
        </>
      )}
      {mailField(fields, "has_attachments") === "yes" && (
        <>
          <dt className="text-meta text-muted">Attachments</dt>
          <dd className="text-row text-muted">Yes (open them from the message in Outlook)</dd>
        </>
      )}
    </dl>
  );
}

export function FieldGrid({ pairs, testId, highlight = 0 }: { pairs: [string, string][]; testId?: string; highlight?: number }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2" data-testid={testId}>
      {pairs.map(([k, v], i) => (
        <div key={`${k}-${i}`} className={cn("min-w-0 rounded-md px-2 py-1.5", i < highlight && "bg-accent-soft")}>
          <dt className="text-meta text-muted">{k}</dt>
          <dd className="break-words text-row font-medium tabular-nums">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A source's title as a reader names it: the subject, "Email from X, date", or "PO 106001 · DSM". */
export function sourceTitle(view: SourceView): string {
  const f = view.fields ?? {};
  const generic = (t: string | null | undefined) => !t || /^(?:email message|bc production|bc sandbox|record)$/i.test(t.trim());
  if (view.system === "Email") {
    const subject = mailField(f, "subject") || view.title;
    if (!generic(subject)) return subject!;
    const from = mailField(f, "from");
    const at = f["row.sent_at"] ?? f.sent_at ?? view.as_of;
    return [from ? `Email from ${from}` : "Email", at ? formatDateTime(at) : null].filter(Boolean).join(", ");
  }
  if (!generic(view.title)) return view.title!;
  const pick = (re: RegExp) => Object.entries(f).find(([k, v]) => re.test(k.replace(/^row\./, "")) && v && !/^(?:null|none)$/i.test(v))?.[1];
  const num = pick(/^(?:number|no|po_?number|so_?number|order_?number|document_?number|ao_?code|code)$/i);
  const party = pick(/^(?:vendor_?name|buy_?from_?vendor_?name|supplier_?name|customer_?name|sell_?to_?customer_?name|display_?name|name)$/i);
  const kind = recordKind(view.handle, view.label);
  const prefix = kind === "Purchase order" ? "PO " : kind === "Sales order" ? "SO " : kind ? `${kind} ` : "";
  if (num) return [/^\d/.test(num) ? `${prefix}${num}` : num, party].filter(Boolean).join(" · ");
  if (party) return party;
  return kind ?? SYSTEM_LABEL[view.system] ?? "Source";
}

/** Fields with equal incl./excl. tax totals read as one "Total". */
function mergeTotals(pairs: [string, string][]): [string, string][] {
  const inc = pairs.find(([k]) => /^Total amount including tax$/i.test(k));
  const exc = pairs.find(([k]) => /^Total amount excluding tax$/i.test(k));
  if (inc && exc && inc[1] === exc[1]) {
    return pairs.filter((p) => p !== exc).map((p) => (p === inc ? (["Total", inc[1]] as [string, string]) : p));
  }
  return pairs;
}

/** The content of a cited source: header, cited passage first, then the rest. */
export function SourceDocument({
  view,
  text,
  cited = [],
  quote,
  headingLevel = 1,
  titleId,
  actions,
}: {
  view: SourceView;
  text: string;
  cited?: string[];
  quote?: string | null;
  headingLevel?: 1 | 2;
  titleId?: string;
  actions?: React.ReactNode;
}) {
  const [sig, setSig] = useState(false);
  const email = view.system === "Email";
  const curated = curatedFields(view.fields, cited);
  const lead = mergeTotals(curated.lead);
  const more = mergeTotals(curated.more);
  const title = sourceTitle(view);
  const kind = email ? "Email" : recordKind(view.handle, view.label);
  const eyebrow = [SYSTEM_LABEL[view.system] ?? view.system, !email && kind && kind !== SYSTEM_LABEL[view.system] ? kind : null]
    .filter(Boolean)
    .join(", ");
  const body = email ? readableBody(text) : text;
  const { main, signature } = email ? splitSignature(body) : { main: body, signature: "" };
  const H = headingLevel === 1 ? "h1" : "h2";
  const citedCount = cited.length > 0 ? lead.filter((_, i) => i < cited.length).length : 0;
  return (
    <div className="min-w-0 space-y-5" data-testid="source-document">
      <header className={cn("min-w-0 space-y-1.5", headingLevel === 1 && "border-b border-border pb-4")}>
        <p className="text-meta text-muted">
          {eyebrow}
          {email && view.as_of ? `, ${formatDateTime(view.as_of)}` : ""}
        </p>
        <H id={titleId} className={cn("break-words", headingLevel === 1 ? "text-display" : "text-title")} data-testid="source-title">
          {title}
        </H>
        {!email && view.as_of && <p className="text-meta text-muted">Read {formatDateTime(view.as_of)}</p>}
        {actions}
      </header>
      {view.changed_since && (
        <p className="rounded-sm border border-risk-line bg-risk-soft px-3 py-2 text-row">
          Changed since the answer cited it. Reread before acting.
        </p>
      )}
      {email && <EmailHeader fields={view.fields} asOf={view.as_of} />}
      {quote && (
        <blockquote className="rounded-sm bg-[var(--risk-soft)] px-3 py-2 text-body" data-testid="cited-passage">
          <span className="mb-0.5 block text-meta text-muted">Cited passage</span>
          “{quote}”
        </blockquote>
      )}
      {text ? (
        <div className="space-y-3">
          {quote && <h3 className="text-meta font-semibold text-fg">Full message</h3>}
          <div className="whitespace-pre-wrap break-words text-body" data-testid="source-text">
            {main}
          </div>
          {signature && (
            <div>
              <button type="button" onClick={() => setSig((v) => !v)} aria-expanded={sig} className="inline-flex items-center gap-1 text-meta text-muted hover:text-fg cursor-pointer">
                {sig ? "Hide signature" : "Show signature"}
              </button>
              {sig && <div className="mt-2 whitespace-pre-wrap break-words text-meta text-muted">{signature}</div>}
            </div>
          )}
        </div>
      ) : !email && lead.length + more.length > 0 ? (
        <>
          <FieldGrid pairs={lead.length > 0 ? lead : more.slice(0, 12)} testId="source-fields" highlight={citedCount} />
          {lead.length > 0 && more.length > 0 && (
            <details className="group" data-testid="source-more-fields">
              <summary className="cursor-pointer text-row text-muted hover:text-fg">All fields ({more.length} more)</summary>
              <div className="pt-3">
                <FieldGrid pairs={more} />
              </div>
            </details>
          )}
        </>
      ) : (
        <p className="text-row text-muted">No readable text.</p>
      )}
    </div>
  );
}

/** Loads a source by handle (and its further text on demand) and shows it. */
export function SourceLoader({
  handle,
  cited = [],
  quote,
  headingLevel = 1,
  titleId,
  actions,
}: {
  handle: string;
  cited?: string[];
  quote?: string | null;
  headingLevel?: 1 | 2;
  titleId?: string;
  actions?: (view: SourceView) => React.ReactNode;
}) {
  const [view, setView] = useState<SourceView | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    setView(null);
    getSource(handle, 0, c.signal)
      .then((v) => {
        setView(v);
        setText(v.text ?? "");
      })
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, [handle]);
  useEffect(() => load(), [load]);

  async function loadMore() {
    if (!view?.next_offset) return;
    setMore(true);
    try {
      const next = await getSource(handle, view.next_offset);
      setText((t) => t + (next.text ?? ""));
      setView(next);
    } catch (e) {
      setError(plainError(e));
    } finally {
      setMore(false);
    }
  }
  if (error && !view) return <ErrorNotice message={error} onRetry={load} />;
  if (!view) return <SkeletonLines lines={6} label="Loading the source" />;
  return (
    <div className="min-w-0">
      <SourceDocument view={view} text={text} cited={cited} quote={quote} headingLevel={headingLevel} titleId={titleId} actions={actions?.(view)} />
      {view.next_offset ? (
        <button
          type="button"
          onClick={loadMore}
          disabled={more}
          className="mt-4 rounded-sm border border-control px-3 py-1 text-row hover:bg-surface-2 cursor-pointer"
        >
          {more ? "Loading…" : "Show more"}
        </button>
      ) : null}
      {error && <ErrorNotice className="mt-3" message={error} />}
    </div>
  );
}
