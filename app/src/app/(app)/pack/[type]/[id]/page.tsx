"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { CardActions } from "@/components/card-actions";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getRecordPack } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { ActionResult, RecordPack } from "@/lib/types";

// A record's document pack: what a customer or supplier usually asks for, found around the
// record as you may read it, and a drafted reply that goes out only through your confirm.
export default function PackPage() {
  const params = useParams<{ type: string; id: string }>();
  const type = decodeURIComponent(params.type);
  const id = decodeURIComponent(params.id);
  const [pack, setPack] = useState<RecordPack | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const [state, setState] = useState<ActionResult | null>(null);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setPack(null);
    getRecordPack(type, id, ctl.signal).then(setPack, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [type, id, round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!pack) return <Skeleton className="h-64 w-full" />;
  return (
    <div className="space-y-4 text-[13px]" data-testid="record-pack">
      <h1 className="text-lg font-semibold">Documents for {pack.label}</h1>
      <p className="text-muted">{pack.basis}</p>
      <ul className="space-y-1">
        {(pack.documents ?? []).map((d) => (
          <li key={d.citation}>
            <span className="font-medium">{d.label}</span>: {d.title ?? d.kind}
            {d.date ? ` · ${d.date.slice(0, 10)}` : ""} · {d.kind.replace(/_/g, " ")}
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
        <h2 className="font-medium">Drafted reply (not sent)</h2>
        <div>To: {pack.draft.to?.address ?? "not recorded for you"}</div>
        <div>Subject: {pack.draft.subject}</div>
        <pre className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]">{pack.draft.body}</pre>
        {(pack.draft_missing ?? []).length > 0 && <p className="text-muted">{(pack.draft_missing ?? []).join("; ")}.</p>}
        {pack.card_id && <CardActions card={{ id: pack.card_id, subject: { label: pack.draft.subject }, actions: pack.offers }} state={state} onDone={setState} />}
      </section>
    </div>
  );
}
