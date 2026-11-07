"use client";

// Proposed follow-ups and record updates, as the API drafted them from the gated records.
// The person edits the text, copies it or opens it in their own mail client (a mailto: link
// built here); a proposed update is a before/after the person applies in BC or the Portal.
// Nothing here sends mail or writes to any system.

import { ArrowUpRight, Check, ChevronRight, Copy } from "lucide-react";
import { useState } from "react";
import { formatDate } from "@/lib/format";
import { draftText, mailtoHref } from "@/lib/proposals";
import type { Proposal } from "@/lib/types";
import { SourceChips } from "./source-chip";
import { OpenRing } from "./ui/glyphs";

const SYSTEM_NAME: Record<string, string> = { BC: "Business Central", Portal: "the Portal", FileMaker: "FileMaker", Sage: "Sage" };

function dateOr(v: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(v) ? formatDate(v) : v;
}

function Draft({ p }: { p: Proposal }) {
  const [to, setTo] = useState(p.to ?? "");
  const [subject, setSubject] = useState(p.email_subject ?? "");
  const [body, setBody] = useState(p.body ?? "");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(draftText(to, subject, body));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };
  const row = "flex items-center gap-3 border-b border-border px-4 text-[13px]";
  const input = "h-10 min-w-0 flex-1 bg-transparent text-fg placeholder:text-faint focus:outline-none";
  return (
    <div className="space-y-3">
      <div className="overflow-hidden rounded-xl bg-bg shadow-lift focus-within:shadow-[var(--lift),0_0_0_2px_color-mix(in_srgb,var(--accent)_35%,transparent)]">
        <label className={row}>
          <span className="w-14 shrink-0 text-muted">To</span>
          {!to && <OpenRing size={12} className="-mr-1" />}
          <input className={input} value={to} onChange={(e) => setTo(e.target.value)} placeholder="No contact found: add the address" aria-label="To" />
        </label>
        {p.contact_note && (
          <p className="border-b border-border bg-surface px-4 py-2 text-[12px] leading-4 text-muted" data-testid="contact-note">
            {p.contact_note}
          </p>
        )}
        <label className={row}>
          <span className="w-14 shrink-0 text-muted">Subject</span>
          <input className={`${input} font-medium`} value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="Subject" />
        </label>
        <textarea
          className="block min-h-56 w-full resize-y bg-transparent px-4 pt-3.5 pb-4 font-sans text-[13px] leading-[21px] text-fg focus:outline-none"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          aria-label="Draft text"
        />
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <a
          href={mailtoHref(to, subject, body)}
          className="mr-1 inline-flex h-8 items-center gap-1.5 rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg shadow-sm hover:brightness-110"
          data-testid="open-in-outlook"
        >
          Open in Outlook
          <ArrowUpRight className="size-3.5" strokeWidth={2.2} aria-hidden />
        </a>
        <button type="button" onClick={copy} className="inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-[13px] text-muted hover:bg-hover hover:text-fg cursor-pointer">
          {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

function Change({ p }: { p: Proposal }) {
  const c = p.change!;
  return (
    <div className="space-y-3" data-testid="proposed-change">
      <div className="overflow-hidden rounded-xl bg-bg shadow-lift">
        <div className="max-w-full overflow-x-auto">
          <table className="w-full text-[13px]">
          <caption className="sr-only">{c.field} on {c.record}: now and proposed</caption>
          <tbody>
            <tr className="border-b border-border">
              <th scope="row" className="w-40 px-4 py-2.5 text-left align-top font-normal text-muted">{c.field} now</th>
              <td className="py-2.5 pr-4 tabular-nums">
                <span className="text-muted line-through decoration-[color-mix(in_srgb,var(--fg)_40%,transparent)]">{dateOr(c.before)}</span>
                {/* on a phone the source sits under the value, so the row never runs off the screen */}
                <span className="mt-1 block sm:hidden"><SourceChips sources={[c.before_source]} max={1} /></span>
              </td>
              <td className="hidden py-2.5 pr-4 text-right sm:table-cell"><SourceChips sources={[c.before_source]} max={1} /></td>
            </tr>
            <tr>
              <th scope="row" className="px-4 py-2.5 text-left align-top font-normal text-muted">Proposed</th>
              <td className="py-2.5 pr-4 font-medium tabular-nums">
                {dateOr(c.after)}
                <span className="mt-1 block font-normal sm:hidden"><SourceChips sources={[c.evidence]} max={1} /></span>
              </td>
              <td className="hidden py-2.5 pr-4 text-right sm:table-cell"><SourceChips sources={[c.evidence]} max={1} /></td>
            </tr>
          </tbody>
        </table>
</div>
      </div>
      <p className="text-[13px] leading-5">{c.why}</p>
      <p className="text-[12px] leading-4 text-muted">Make the change in {SYSTEM_NAME[c.system] ?? c.system}. The Workbench doesn&apos;t write to it.</p>
    </div>
  );
}

function ProposalCard({ p }: { p: Proposal }) {
  const cited = (p.figures ?? []).filter((f) => f.source);
  const missing = p.missing ?? [];
  return (
    <article className="space-y-3" data-testid="proposal" data-kind={p.kind}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <h3 className="text-[15px] leading-[22px] font-medium tracking-[-0.01em]">{p.title}</h3>
        <span className="shrink-0 text-[12px] text-muted">{p.kind === "record_update" ? "Proposed update" : "Draft, not sent"}</span>
      </div>
      {p.change ? <Change p={p} /> : <Draft p={p} />}
      {missing.length > 0 && (
        <p className="flex gap-2 text-[12px] leading-[18px] text-fg" data-testid="proposal-missing">
          <OpenRing size={12} className="mt-[3px]" />
          <span>Not on record: {missing.join("; ")}.</span>
        </p>
      )}
      {!p.change && cited.length > 0 && (
        <details className="group text-[12px] text-muted">
          <summary className="-mx-2 inline-flex h-7 cursor-pointer list-none items-center gap-1 rounded-md px-2 hover:bg-hover hover:text-fg [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden />
            Sources for each figure
          </summary>
          <ul className="mt-1.5 space-y-1.5 pl-5">
            {cited.map((f, i) => (
              <li key={i} className="flex flex-wrap items-center gap-2">
                <span>{f.label}: <span className="text-fg">{f.value}</span></span>
                <SourceChips sources={[f.source!]} max={1} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </article>
  );
}

/** The page's or answer's proposals; nothing when there are none. `bare`: no heading (a section that already names them). */
export function ProposalCards({ proposals, bare = false }: { proposals: Proposal[] | null | undefined; bare?: boolean }) {
  if (!proposals || proposals.length === 0) return null;
  return (
    <section id="proposals" className="scroll-mt-20 space-y-8" aria-label="Proposed follow-ups" data-testid="proposals">
      {!bare && <h2 className="-mb-5 text-[12px] leading-4 font-medium text-muted">Drafts</h2>}
      {proposals.map((p) => (
        <ProposalCard key={p.id} p={p} />
      ))}
    </section>
  );
}
