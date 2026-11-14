"use client";

import { ChevronRight, Paperclip } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { getSource } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ApiError, plainError } from "@/lib/errors";
import { fieldLabel, fieldValue, formatDateTime, recordKind, SYSTEM_LABEL } from "@/lib/format";
import type { SourceView } from "@/lib/types";
import { curatedFields } from "./source-chip";
import { StatusGlyph, SystemCode } from "./ui/glyphs";
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

const squash = (t: string) => t.normalize("NFKC").replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/\s+/g, " ").trim().toLowerCase();

/** Does the quote a link carries occur in the source it points at (case and spacing aside)? */
export function quoteOccurs(quote: string, text: string, fields?: Record<string, unknown> | null): boolean {
  const q = squash(quote);
  if (!q) return false;
  const haystack = [text, ...Object.values(fields ?? {}).filter((v) => typeof v === "string" || typeof v === "number").map(String)];
  return haystack.some((h) => squash(h).includes(q));
}

/** Most lines a signature may run to; anything longer is message, not signature. */
const SIGNATURE_MAX_LINES = 12;
/** A line of running prose (many words), which a signature block does not hold. */
const proseLine = (l: string) => l.trim().split(/\s+/).length >= 9;

/** Is `tail` (the lines from a cut to the end) plausibly all signature? Short, a few lines, no prose. */
function signatureLike(tail: string[]): boolean {
  const filled = tail.filter((l) => l.trim());
  return filled.length > 0 && filled.length <= SIGNATURE_MAX_LINES && !filled.some(proseLine);
}

/**
 * The body before the signature, and the signature. A signature starts at a sign-off line
 * ("Thanks,", "Best regards") or at a contact block that follows a blank line, and it is
 * only what runs from there to the end: a few short lines with no sentences. Business
 * instructions that merely sit near an email address or phone number stay in the message.
 */
export function splitSignature(body: string): { main: string; signature: string } {
  const lines = body.split("\n");
  let cut = -1;
  // the last sign-off whose remainder is a signature (a "Thanks" mid-message does not count)
  for (let i = lines.length - 1; i > 0; i--) {
    if (SIGN_OFF.test(lines[i]) && signatureLike(lines.slice(i + 1))) {
      cut = i;
      break;
    }
  }
  if (cut < 0) {
    for (let i = 1; i + 2 < lines.length; i++) {
      if (lines[i - 1].trim() !== "" || lines[i].trim() === "") continue; // a block starts after a blank line
      const tail = lines.slice(i);
      if (tail.filter((l) => CONTACTISH.test(l)).length >= 2 && signatureLike(tail) && tail.filter((l) => l.trim()).every((l) => l.trim().length < 60)) {
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
    <div className="flex gap-1.5 text-[12px] leading-[18px]">
      <dt className="shrink-0 text-muted">{label}:</dt>
      <dd className="min-w-0 break-words text-fg/85">
        {shown.join(", ")}
        {people.length > fold && (
          <button type="button" onClick={() => setOpen((v) => !v)} className="ml-1.5 text-muted underline decoration-border-strong underline-offset-2 hover:text-fg cursor-pointer" aria-expanded={open}>
            {open ? "fewer" : `${people.length - fold} more`}
          </button>
        )}
      </dd>
    </div>
  );
}

/** "Ross Smith <ross@x.com>" as its name and address; a bare address is both. */
function splitSender(from: string): { name: string; address: string } {
  const m = /^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/.exec(from);
  if (m && m[1]) return { name: m[1], address: m[2] };
  return { name: m?.[2] ?? from, address: "" };
}

/** A message's header as a mail app draws it: the sender's tile and name, the date on the right, To and Cc under them. */
export function EmailHeader({ fields, asOf }: { fields: Record<string, string> | null | undefined; asOf?: string | null }) {
  const sentRaw = fields?.["row.sent_at"] ?? fields?.sent_at ?? fields?.["row.received_at"] ?? fields?.received_at ?? asOf ?? "";
  const sent = sentRaw ? formatDateTime(sentRaw) : "";
  const from = mailField(fields, "from");
  const who = from ? splitSender(from) : null;
  const attached = mailField(fields, "has_attachments") === "yes";
  return (
    <div className="flex gap-3" data-testid="email-header">
      <dl className="min-w-0 flex-1">
        <div className="flex items-baseline gap-3">
          <dt className="sr-only">From</dt>
          <dd className={cn("min-w-0 flex-1 truncate text-[13px] leading-[18px] font-semibold", !who && "font-normal text-muted")} title={from || undefined}>
            {who ? who.name : "Sender not captured"}
            {who?.address && <span className="ml-1.5 font-normal text-muted">{who.address}</span>}
          </dd>
          {sent && (
            <>
              <dt className="sr-only">Date</dt>
              <dd className="shrink-0 text-[12px] leading-4 tabular-nums text-muted">{sent}</dd>
            </>
          )}
        </div>
        <div className="mt-1 space-y-0.5">
          <People label="To" people={mailList(fields, "to")} fold={3} />
          <People label="Cc" people={mailList(fields, "cc")} />
          {attached && (
            <div className="flex items-center gap-1.5 pt-0.5 text-[12px] leading-[18px] text-muted">
              <dt className="sr-only">Attachments</dt>
              <Paperclip className="size-3.5" strokeWidth={1.8} aria-hidden />
              <dd>Attachments are in Outlook</dd>
            </div>
          )}
        </div>
      </dl>
    </div>
  );
}

/** A record's fields as a properties sheet: label over value, two to a row where there is room; the cited ones marked. */
export function FieldGrid({ pairs, testId, highlight = 0, compact = false }: { pairs: [string, string][]; testId?: string; highlight?: number; compact?: boolean }) {
  if (compact)
    return (
      <dl className="space-y-px" data-testid={testId}>
        {pairs.map(([k, v], i) => (
          <div key={`${k}-${i}`} className="grid grid-cols-[minmax(0,124px)_minmax(0,1fr)] items-baseline gap-3 py-[5px]">
            <dt className="text-[12px] leading-4 text-muted">{k}</dt>
            <dd className="min-w-0 break-words text-[12px] leading-4 tabular-nums">{v}</dd>
          </div>
        ))}
      </dl>
    );
  return (
    <dl className="grid grid-cols-1 overflow-hidden rounded-xl bg-bg shadow-lift sm:grid-cols-2" data-testid={testId}>
      {pairs.map(([k, v], i) => (
        <div
          key={`${k}-${i}`}
          className={cn(
            "relative min-w-0 border-b border-border px-4 py-2.5 sm:odd:border-r",
            i < highlight && "bg-[color-mix(in_srgb,var(--accent)_7%,transparent)]",
          )}
        >
          {i < highlight && <span className="absolute inset-y-2.5 left-0 w-[3px] rounded-r-full bg-accent" aria-hidden />}
          <dt className="text-[12px] leading-4 text-muted">{k}</dt>
          <dd className="mt-0.5 break-words text-[13px] leading-5 font-medium tabular-nums">{v}</dd>
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
  const prefix = kind === "Purchase order" ? "PO " : kind === "Customer order" ? "SO " : kind ? `${kind} ` : "";
  if (num) return [/^\d/.test(num) ? `${prefix}${num}` : num, party].filter(Boolean).join(" · ");
  if (party) return party;
  return kind ?? SYSTEM_LABEL[view.system] ?? "Source";
}

/** What kind of thing a source is, in a word or two: "Mail message", "Purchase order", "File". */
export function sourceKind(view: SourceView): string {
  if (view.system === "Email") return "Mail message";
  if (view.system === "Teams") return "Teams message";
  const kind = recordKind(view.handle, view.label);
  return kind && kind !== SYSTEM_LABEL[view.system] ? kind : view.kind === "text" ? "Document" : "Record";
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

function InspectorTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-2 text-[12px] leading-4 font-medium text-muted">{children}</h2>;
}

function Prop({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[72px_minmax(0,1fr)] items-baseline gap-3 py-1">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="min-w-0 text-[13px] leading-5 break-words">{children}</dd>
    </div>
  );
}

/**
 * The content of a cited source: its system tile, kind and title, then a message as a mail
 * app lays it out (or a record as a properties sheet), the cited passage marked. `wide` is
 * the full page: the text in a readable column and, with room, an inspector on the right
 * holding the source's details and every other field.
 */
export function SourceDocument({
  view,
  text,
  cited = [],
  quote,
  headingLevel = 1,
  titleId,
  actions,
  wide = false,
  after,
  aside,
  context,
}: {
  view: SourceView;
  text: string;
  cited?: string[];
  quote?: string | null;
  headingLevel?: 1 | 2;
  titleId?: string;
  actions?: React.ReactNode;
  wide?: boolean;
  after?: React.ReactNode;
  /** more inspector parts (the full page's records citing it) */
  aside?: React.ReactNode;
  /** one line under the title: where it sits ("Read into PO 90412") */
  context?: string | null;
}) {
  const [sig, setSig] = useState(false);
  const [all, setAll] = useState(false);
  const email = view.system === "Email";
  const curated = curatedFields(view.fields, cited);
  const lead = mergeTotals(curated.lead);
  const more = mergeTotals(curated.more);
  const title = sourceTitle(view);
  const kind = sourceKind(view);
  const system = SYSTEM_LABEL[view.system] ?? view.system;
  const body = email ? readableBody(text) : text;
  const split = email ? splitSignature(body) : { main: body, signature: "" };
  // a separator line left just above the signature says nothing once the signature folds away
  const main = split.signature ? split.main.replace(/\n+—\s*$/, "") : split.main;
  const signature = split.signature;
  const H = headingLevel === 1 ? "h1" : "h2";
  const citedCount = cited.length > 0 ? lead.filter((_, i) => i < cited.length).length : 0;
  const shownFields = lead.length > 0 ? lead : more.slice(0, 12);
  const rest = lead.length > 0 ? more : more.slice(12);
  const read = view.as_of ? formatDateTime(view.as_of) : "";

  const content = (
    <>
      <header className="flex min-w-0 gap-3.5">
        <SystemCode system={view.system} size={wide ? 40 : 32} className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <p className="text-[12px] leading-4 text-muted">
            {system}
            {!email && (
              <>
                <span className="px-1.5 text-faint" aria-hidden>
                  ·
                </span>
                {kind}
              </>
            )}
          </p>
          <H
            id={titleId}
            className={cn("mt-1 break-words font-semibold", wide ? "text-[22px] leading-7 tracking-[-0.02em]" : "text-[17px] leading-[23px] tracking-[-0.01em]")}
            data-testid="source-title"
          >
            {title}
          </H>
          {!email && read && <p className={cn("mt-1 text-[12px] leading-4 text-muted", wide && "@min-[880px]:hidden")}>Read {read}</p>}
          {context && (
            <p className="mt-1 text-[13px] leading-5 text-fg/85" data-testid="source-context">
              {context}
            </p>
          )}
          {actions}
        </div>
      </header>

      {view.changed_since && (
        <p className="flex items-center gap-2 text-[13px] leading-5">
          <StatusGlyph status="risk" size={14} />
          Changed since the answer cited it. Reread before acting.
        </p>
      )}

      {email && (
        <div className="border-y border-border py-4">
          <EmailHeader fields={view.fields} asOf={view.as_of} />
        </div>
      )}

      {quote && (
        <figure className="relative rounded-lg bg-[color-mix(in_srgb,var(--accent)_6%,transparent)] py-2.5 pr-3.5 pl-4" data-testid="cited-passage">
          <span className="absolute inset-y-2.5 left-0 w-[3px] rounded-r-full bg-accent" aria-hidden />
          <figcaption className="mb-0.5 text-[12px] leading-4 text-muted">
            {quoteOccurs(quote, text, view.fields) ? "Cited passage" : "Quoted in the link (not found in this source)"}
          </figcaption>
          <blockquote className="text-[13px] leading-5">“{quote}”</blockquote>
        </figure>
      )}

      {text ? (
        <div className="space-y-4">
          {quote && <h3 className="text-[12px] leading-4 font-medium text-muted">{email ? "Full message" : "Full text"}</h3>}
          <div
            className={cn("max-w-[68ch] whitespace-pre-wrap break-words text-fg/95", wide ? "text-[14px] leading-[23px]" : "text-[13px] leading-[21px]")}
            data-testid="source-text"
          >
            {main}
          </div>
          {signature && (
            <div>
              <button type="button" onClick={() => setSig((v) => !v)} aria-expanded={sig} className="inline-flex h-7 items-center rounded-md px-2 -ml-2 text-[12px] text-muted hover:bg-hover hover:text-fg cursor-pointer">
                {sig ? "Hide signature" : `Show signature (${signature.split("\n").filter((l) => l.trim()).length} lines)`}
              </button>
              {sig && <div className="mt-1 max-w-[68ch] whitespace-pre-wrap break-words text-[12px] leading-[18px] text-muted">{signature}</div>}
            </div>
          )}
          {after}
        </div>
      ) : !email && shownFields.length > 0 ? (
        <>
          <FieldGrid pairs={shownFields} testId="source-fields" highlight={citedCount} />
          {!wide && rest.length > 0 && (
            <details className="group" data-testid="source-more-fields">
              <summary className="inline-flex h-7 cursor-pointer list-none items-center gap-1 rounded-md px-2 -ml-2 text-[12px] text-muted hover:bg-hover hover:text-fg [&::-webkit-details-marker]:hidden">
                <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
                All fields <span className="num text-faint">{rest.length}</span>
              </summary>
              <div className="pt-2">
                <FieldGrid pairs={rest} compact />
              </div>
            </details>
          )}
        </>
      ) : (
        <p className="text-[13px] text-muted">No readable text.</p>
      )}
    </>
  );

  if (!wide)
    return (
      <div className="min-w-0 space-y-5" data-testid="source-document">
        {content}
      </div>
    );

  return (
    <div className="@container flex min-w-0 flex-1 flex-col" data-testid="source-document">
      <div className="grid min-w-0 flex-1 @min-[880px]:grid-cols-[minmax(0,1fr)_320px]">
        <article className="min-w-0 px-5 pt-7 pb-14 sm:px-10 sm:pt-9">
          <div className="mx-auto max-w-[760px] space-y-6">{content}</div>
        </article>
        <aside
          className="min-w-0 space-y-7 border-t border-border bg-[color-mix(in_srgb,var(--surface)_70%,var(--bg))] px-5 py-6 sm:px-10 @min-[880px]:border-t-0 @min-[880px]:border-l @min-[880px]:px-6 @min-[880px]:pt-9"
          aria-label="Details"
        >
          <section>
            <InspectorTitle>Details</InspectorTitle>
            <dl>
              <Prop label="Source">
                <span className="inline-flex items-center gap-2">
                  <SystemCode system={view.system} size={16} />
                  {system}
                </span>
              </Prop>
              <Prop label="Kind">{kind}</Prop>
              {read && <Prop label={email ? "Captured" : "Read"}>{read}</Prop>}
              {cited.length > 0 && <Prop label="Cited">{cited.map((c) => fieldLabel(c)).join(", ")}</Prop>}
              {view.changed_since && (
                <Prop label="Status">
                  <span className="inline-flex items-center gap-1.5">
                    <StatusGlyph status="risk" size={12} />
                    Changed since cited
                  </span>
                </Prop>
              )}
            </dl>
          </section>
          {aside}
          {!email && rest.length > 0 && (
            <section data-testid="source-more-fields">
              {/* narrow: a toggle; wide: the fields always show, so a plain heading (no dead, "collapsed" button) */}
              <button
                type="button"
                onClick={() => setAll((v) => !v)}
                aria-expanded={all}
                className="mb-2 flex w-full items-center gap-1 text-left text-[12px] leading-4 font-medium text-muted hover:text-fg cursor-pointer @min-[880px]:hidden"
              >
                <ChevronRight className={cn("size-3.5 transition-transform", all && "rotate-90")} aria-hidden />
                All fields <span className="font-normal num text-faint">{rest.length}</span>
              </button>
              <h3 className="mb-2 hidden text-[12px] leading-4 font-medium text-muted @min-[880px]:block">
                All fields <span className="font-normal num text-faint">{rest.length}</span>
              </h3>
              <div className={cn("hidden @min-[880px]:block", all && "block")}>
                <FieldGrid pairs={rest} compact />
              </div>
            </section>
          )}
        </aside>
      </div>
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
  wide = false,
  onView,
  aside,
  context,
}: {
  handle: string;
  cited?: string[];
  quote?: string | null;
  headingLevel?: 1 | 2;
  titleId?: string;
  actions?: (view: SourceView) => React.ReactNode;
  wide?: boolean;
  onView?: (view: SourceView) => void;
  aside?: React.ReactNode;
  context?: string | null;
}) {
  const [view, setView] = useState<SourceView | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  // a source that isn't there (or isn't the reader's) is said plainly, not as a failure
  const [missing, setMissing] = useState(false);
  const [more, setMore] = useState(false);
  const seen = useRef(onView);
  seen.current = onView;
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    setMissing(false);
    setView(null);
    getSource(handle, 0, c.signal)
      .then((v) => {
        setView(v);
        setText(v.text ?? "");
        seen.current?.(v);
      })
      .catch((e) => {
        if (c.signal.aborted) return;
        setMissing(e instanceof ApiError && e.status === 404);
        setError(plainError(e));
      });
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
  const pad = wide ? "mx-auto w-full max-w-[760px] px-5 pt-9 sm:px-10" : "";
  if (error && !view) {
    // the page still names what it is: a source that couldn't be opened
    const H = headingLevel === 1 ? "h1" : "h2";
    return (
      <div className={pad}>
        <H className="sr-only" id={titleId}>Source</H>
        {missing ? (
          <p className="text-[13px] text-muted" data-testid="source-missing">
            This source isn't here, or you don't have access to it. The link may be old.
          </p>
        ) : (
          <ErrorNotice message={error} onRetry={load} />
        )}
      </div>
    );
  }
  if (!view)
    return (
      <div className={cn(pad, "space-y-3")}>
        <SkeletonLines lines={6} label="Loading the source" />
      </div>
    );
  const pager = view.next_offset ? (
    <div className="flex max-w-[68ch] items-center gap-3 pt-2">
      <span className="h-px flex-1 bg-border" aria-hidden />
      <button
        type="button"
        onClick={loadMore}
        disabled={more}
        className="inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] disabled:opacity-60 cursor-pointer"
      >
        {more ? "Loading" : "Show more"}
      </button>
      <span className="h-px flex-1 bg-border" aria-hidden />
    </div>
  ) : null;
  return (
    <div className={cn("min-w-0", wide && "flex flex-1 flex-col")}>
      <SourceDocument
        view={view}
        text={text}
        cited={cited}
        quote={quote}
        headingLevel={headingLevel}
        titleId={titleId}
        actions={actions?.(view)}
        wide={wide}
        aside={aside}
        context={context}
        after={
          <>
            {pager}
            {error && <ErrorNotice className="mt-3" message={error} />}
          </>
        }
      />
    </div>
  );
}
