"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { EmptyState } from "@/components/ui/empty-state";
import { Pending } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getRecordPack } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { formatDate } from "@/lib/format";
import type { ActionResult, RecordPack } from "@/lib/types";

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
      <ul className="space-y-1">
        {(pack.documents ?? []).map((d) => (
          <li key={d.citation}>
            <span className="font-medium">{d.label}</span>: {d.title ?? d.kind}
            {d.date ? ` · ${formatDate(d.date, { short: true })}` : ""} · {d.kind.replace(/_/g, " ")}
            {d.url && (
              <>
                {" "}
                <Link className="hover:underline" href={d.url}>open</Link>
              </>
            )}
            <span className="ml-1 text-muted">({d.citation})</span>
          </li>
        ))}
      </ul>
      {(pack.missing_labels ?? []).length > 0 && <p className="text-muted">Not found for you: {(pack.missing_labels ?? []).join(", ")}.</p>}
      <section className="space-y-2 rounded-lg border border-border p-3">
        <h2 className="font-medium">Drafted reply</h2>
        <div>To: {pack.draft.to?.address ?? "not recorded for you"}</div>
        <div>Subject: {pack.draft.subject}</div>
        {pack.draft.body ? <pre className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]">{pack.draft.body}</pre> : <p className="text-muted">No draft.</p>}
        {(pack.draft_missing ?? []).length > 0 && <p className="text-muted">{(pack.draft_missing ?? []).join("; ")}.</p>}
        {pack.card_id && <CardActions card={{ id: pack.card_id, subject: { label: pack.draft.subject }, actions: pack.offers }} state={state} onDone={setState} />}
      </section>
    </div>
  );
}
