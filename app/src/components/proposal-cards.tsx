"use client";

// Proposed follow-ups and record updates, as the API drafted them from the gated records.
// The person edits the text, copies it or opens it in their own mail client (a mailto: link
// built here); a proposed update is a before/after the person applies in BC or the Portal.
// Nothing here sends mail or writes to any system.

import { useState } from "react";
import { formatDate } from "@/lib/format";
import { draftText, mailtoHref } from "@/lib/proposals";
import type { Proposal } from "@/lib/types";
import { SourceChips } from "./source-chip";
import { Panel } from "./ui/card";

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
  const field = "mt-1 w-full rounded border border-control bg-raised px-3 py-1.5 text-row text-fg";
  return (
    <div className="space-y-2">
      <label className="block text-meta text-muted">
        To
        <input className={field} value={to} onChange={(e) => setTo(e.target.value)} placeholder="No contact found: add the address" aria-label="To" />
      </label>
      {p.contact_note && <p className="text-meta text-muted" data-testid="contact-note">{p.contact_note}</p>}
      <label className="block text-meta text-muted">
        Subject
        <input className={field} value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="Subject" />
      </label>
      <textarea className={`${field} min-h-56 font-sans`} value={body} onChange={(e) => setBody(e.target.value)} aria-label="Draft text" />
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={copy} className="h-8 rounded-sm border border-control px-3 text-row hover:bg-surface-2 cursor-pointer">
          {copied ? "Copied" : "Copy"}
        </button>
        <a href={mailtoHref(to, subject, body)} className="inline-flex h-8 items-center rounded-sm bg-accent px-3 text-row font-medium text-accent-fg hover:brightness-110" data-testid="open-in-outlook">
          Open in Outlook
        </a>
      </div>
    </div>
  );
}

function Change({ p }: { p: Proposal }) {
  const c = p.change!;
  return (
    <div className="space-y-2" data-testid="proposed-change">
      <table className="w-full text-row">
        <caption className="sr-only">{c.field} on {c.record}: now and proposed</caption>
        <tbody>
          <tr className="border-b border-border">
            <th scope="row" className="py-1.5 pr-4 text-left font-normal text-muted">{c.field} now</th>
            <td className="py-1.5 tabular-nums">
              <span className="line-through decoration-risk">{dateOr(c.before)}</span>
              {/* on a phone the source sits under the value, so the row never runs off the screen */}
              <span className="mt-1 block sm:hidden"><SourceChips sources={[c.before_source]} max={1} /></span>
            </td>
            <td className="hidden py-1.5 text-right sm:table-cell"><SourceChips sources={[c.before_source]} max={1} /></td>
          </tr>
          <tr>
            <th scope="row" className="py-1.5 pr-4 text-left font-normal text-muted">Proposed</th>
            <td className="py-1.5 font-medium tabular-nums">
              {dateOr(c.after)}
              <span className="mt-1 block font-normal sm:hidden"><SourceChips sources={[c.evidence]} max={1} /></span>
            </td>
            <td className="hidden py-1.5 text-right sm:table-cell"><SourceChips sources={[c.evidence]} max={1} /></td>
          </tr>
        </tbody>
      </table>
      <p className="text-row">{c.why}</p>
      <p className="text-meta text-muted">Make the change in {SYSTEM_NAME[c.system] ?? c.system}. The Workbench doesn&apos;t write to it.</p>
    </div>
  );
}

function ProposalCard({ p }: { p: Proposal }) {
  const cited = (p.figures ?? []).filter((f) => f.source);
  const missing = p.missing ?? [];
  return (
    <Panel data-testid="proposal" data-kind={p.kind}>
      <div className="flex items-baseline justify-between gap-3 border-b border-border px-4 py-2.5">
        <h3 className="text-row font-semibold">{p.title}</h3>
        <span className="shrink-0 text-meta text-muted">{p.kind === "record_update" ? "Proposed update" : "Draft, not sent"}</span>
      </div>
      <div className="space-y-3 p-4">
        {p.change ? <Change p={p} /> : <Draft p={p} />}
        {missing.length > 0 && (
          <p className="text-meta text-risk" data-testid="proposal-missing">
            Not on record: {missing.join("; ")}.
          </p>
        )}
        {!p.change && cited.length > 0 && (
          <details className="text-meta text-muted">
            <summary className="cursor-pointer">Sources for each figure</summary>
            <ul className="mt-1 space-y-1">
              {cited.map((f, i) => (
                <li key={i} className="flex flex-wrap items-center gap-2">
                  <span>{f.label}: <span className="text-fg">{f.value}</span></span>
                  <SourceChips sources={[f.source!]} max={1} />
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </Panel>
  );
}

/** The page's or answer's proposals; nothing when there are none. */
export function ProposalCards({ proposals }: { proposals: Proposal[] | null | undefined }) {
  if (!proposals || proposals.length === 0) return null;
  return (
    <section id="proposals" className="scroll-mt-20 space-y-3 border-t border-rule" aria-label="Proposed follow-ups" data-testid="proposals">
      <div className="pt-2.5">
        <h2 className="text-section">Drafts</h2>
        <p className="mt-0.5 text-meta text-muted">Nothing is sent or changed from here. Edit, copy, or open in Outlook.</p>
      </div>
      {proposals.map((p) => (
        <ProposalCard key={p.id} p={p} />
      ))}
    </section>
  );
}
