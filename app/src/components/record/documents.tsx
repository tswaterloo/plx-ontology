"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { EmptyState } from "@/components/ui/empty-state";
import { Pending } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getRecordPack } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate, safeHref } from "@/lib/format";
import type { ActionResult, RecordPack } from "@/lib/types";

/** Where a document was found, as the reader names it. */
const DOC_KIND: Record<string, string> = { attachment: "Email attachment", sharepoint_file: "SharePoint file", original: "Original", email: "Email", record: "Record" };

// The record page's Documents section (was /pack): what a customer or supplier usually asks
// for, found around the record as you may read it, and a drafted reply that goes out only
// through your confirm.
export function DocumentsSection({ type, id }: { type: string; id: string }) {
  const [pack, setPack] = useState<RecordPack | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [state, setState] = useState<ActionResult | null>(null);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  // gathered on demand (it reads attachments around the record): the header's Documents
  // link (#documents) or the section's own button opens it
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const on = () => window.location.hash === "#documents" && setOpen(true);
    on();
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  useEffect(() => {
    if (!open) return;
    const ctl = new AbortController();
    setError(null);
    setPack(null);
    getRecordPack(type, id, ctl.signal).then(setPack, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [type, id, round, open]);
  return (
    <section id="documents" className="scroll-mt-16 space-y-3 text-[13px]" data-testid="record-pack">
      <h2 className="text-[15px] font-semibold">Documents</h2>
      {!open ? (
        <button type="button" onClick={() => setOpen(true)} className="text-[13px] font-medium text-accent hover:underline cursor-pointer">
          Gather the documents
        </button>
      ) : error ? (
        <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
      ) : !pack ? (
        <Pending label="Finding the documents" />
      ) : (
        <PackBody pack={pack} state={state} setState={setState} />
      )}
    </section>
  );
}

function PackBody({ pack, state, setState }: { pack: RecordPack; state: ActionResult | null; setState: (r: ActionResult) => void }) {
  if ((pack.documents ?? []).length === 0 && (pack.missing_labels ?? []).length === 0) return <EmptyState title="No documents found." />;
  return (
    <div className="space-y-3">
      <p className="text-muted">{pack.basis}</p>
      <ul className="divide-y divide-border" aria-label="Documents found">
        {(pack.documents ?? []).map((d) => {
          // the document opens where it lives, else in the source reader by its citation; the
          // citation itself is a handle, never shown
          const href = safeHref(d.url) ?? `/source?handle=${encodeURIComponent(d.citation)}`;
          const name = d.title && d.title !== d.label ? `${d.label}: ${d.title}` : d.label;
          const where = DOC_KIND[d.kind] ?? d.kind.replace(/_/g, " ");
          return (
            <li key={d.citation} className="flex flex-wrap items-baseline gap-x-2 py-1.5" data-testid="pack-document">
              <Link className="min-w-0 font-medium break-words hover:underline" href={href}>
                {name}
              </Link>
              <span className="text-[12px] text-muted">
                {where}
                {d.date ? ` · ${formatDate(d.date, { short: true })}` : ""}
              </span>
            </li>
          );
        })}
      </ul>
      {(pack.missing_labels ?? []).length > 0 && <p className="text-muted">Not found for you: {(pack.missing_labels ?? []).join(", ")}.</p>}
      <section className="space-y-2 rounded-lg border border-border p-3">
        <h3 className="font-medium">
          Drafted reply <span className="font-normal text-muted">· not sent</span>
        </h3>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
          <dt className="text-muted">To</dt>
          <dd className="break-words">{pack.draft.to?.address ?? <span className="text-muted">No address on record that you can see</span>}</dd>
          <dt className="text-muted">Subject</dt>
          <dd className="break-words">{pack.draft.subject}</dd>
        </dl>
        {pack.draft.body ? <p className="whitespace-pre-wrap rounded-lg bg-surface p-2.5 leading-5">{pack.draft.body}</p> : <p className="text-muted">No draft.</p>}
        {(pack.draft_missing ?? []).length > 0 && <p className="text-muted">{(pack.draft_missing ?? []).join("; ")}.</p>}
        {pack.card_id && <CardActions card={{ id: pack.card_id, subject: { label: pack.draft.subject }, actions: pack.offers }} state={state} onDone={setState} />}
      </section>
    </div>
  );
}
