"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getActionStates, getAlertPlaybook } from "@/lib/api";
import { actionChip } from "@/lib/actions";
import { plainError } from "@/lib/errors";
import type { ActionResult, AlertPlaybook } from "@/lib/types";
import { CardActions } from "./card-actions";

/** One alert's next steps (``/api/alerts/{id}/playbook``): the drafted email and Teams note,
 * links to the record here and in its source system, the change to make there yourself, and
 * the actions. Nothing is sent or written from here: every action opens the preview dialog,
 * and only your own confirm in it does anything. */
export function AlertPlaybookPanel({ alertId }: { alertId: string }) {
  const [book, setBook] = useState<AlertPlaybook | null>(null);
  const [state, setState] = useState<ActionResult | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    getAlertPlaybook(alertId, ctl.signal)
      .then((b) => {
        setBook(b);
        return getActionStates(ctl.signal).then((s) => setState(s.cards?.[b.id] ?? null));
      })
      .catch((e) => !ctl.signal.aborted && setFailed(plainError(e)));
    return () => ctl.abort();
  }, [alertId]);
  if (failed) return <p role="alert" className="text-[12px] text-fg">{failed}</p>;
  if (!book) return <p className="text-[12px] text-muted">Preparing next steps…</p>;
  const chip = state ? actionChip(state, false) : null;
  const card = { id: book.id, subject: { label: book.summary ?? "Alert" }, actions: book.offers };
  return (
    <section className="space-y-2 text-[12px] leading-5" data-testid="alert-playbook">
      {chip && <p className="text-muted" title={chip.title}>{chip.label}</p>}
      {book.email && (
        <div>
          <p className="font-medium text-fg">
            Email to the {book.email.party ?? "outside party"}
            {book.email.to ? ` (${book.email.to.address})` : " (add the recipient)"}: {book.email.subject}
          </p>
          <pre className="whitespace-pre-wrap font-sans text-fg/80">{book.email.body}</pre>
        </div>
      )}
      {book.teams && (
        <p>
          <span className="font-medium text-fg">Teams note{book.teams.owner ? ` to ${book.teams.owner.name}` : ""}: </span>
          {book.teams.body}
          {!book.teams.owner_known && <span className="text-muted"> ({book.teams.why_no_owner})</span>}
        </p>
      )}
      {book.change && (
        <p data-testid="playbook-change">
          <span className="font-medium text-fg">{book.change.do_this}</span>
          <span className="text-muted">
            {" "}
            Field: {book.change.field}
            {book.change.value ? ` = ${book.change.value}` : ""}. You make this change yourself; the Workbench never writes {book.change.system}.
          </span>
        </p>
      )}
      <ul className="flex flex-wrap gap-x-3">
        {(book.links ?? []).map((l, i) =>
          l.url && l.url.startsWith("/") ? (
            <li key={i}>
              <Link href={l.url} className="font-medium text-fg hover:underline">Open in Workbench</Link>
            </li>
          ) : l.url ? (
            <li key={i}>
              <a href={l.url} target="_blank" rel="noreferrer noopener" className="font-medium text-fg hover:underline">Open in {l.system}</a>
            </li>
          ) : (
            <li key={i} className="text-muted">{l.system}: {l.reason}</li>
          ),
        )}
      </ul>
      {(book.missing ?? []).length > 0 && <p className="text-muted">Not available to you: {(book.missing ?? []).join("; ")}.</p>}
      <CardActions card={card} state={state} onDone={setState} />
    </section>
  );
}
