"use client";

import { useCallback, useEffect, useState } from "react";
import { approveWrite, executeWrite, getWrites, previewWrite } from "@/lib/api";
import { newIdempotencyKey } from "@/lib/actions";
import { ApiError, plainError } from "@/lib/errors";
import type { WriteIntentView, WritePreview } from "@/lib/types";
import { Button } from "./ui/button";

/** The write entries that concern a card or record: their record id, a citation or the
 * reason names one of `match`. */
export function concerns(view: WriteIntentView, match: string[]): boolean {
  const intent = view.intent as { record_id?: string | null; citations?: string[]; precondition?: { party_id?: string }; intent_id?: string };
  const ids = match.filter(Boolean).map((m) => m.toLowerCase());
  if (ids.length === 0) return false;
  // the entry's own id is matched whole (a short id is never inside another entry's id)
  const own = (intent.intent_id ?? "").toLowerCase();
  const hay = [intent.record_id ?? "", intent.precondition?.party_id ?? "", ...(intent.citations ?? [])].map((h) => h.toLowerCase());
  return ids.some((id) => own === id || hay.some((h) => h === id || h.includes(id)));
}

const show = (v: unknown) => (v === null || v === undefined || v === "" ? "—" : String(v));

/** One entry: preview (field by field), approve with a second confirm click, then run: a
 * dry run (saying what a live write still lacks) unless the preview says it writes live,
 * and the button says which. */
function WriteEntry({ view, onChange, onStale }: { view: WriteIntentView; onChange: (v: WriteIntentView) => void; onStale: () => void }) {
  const [preview, setPreview] = useState<WritePreview | null>(null);
  const [armed, setArmed] = useState(false);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [key] = useState(newIdempotencyKey);
  const id = (view.intent as { intent_id: string }).intent_id;

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // the entry or the record changed under the preview or approval: nothing stays
        // approved on the page's say-so; preview again, acknowledge again, approve again
        setPreview(null);
        setArmed(false);
        setAck(false);
        onStale(); // the entry may now need approving again: read where it stands
        setError(`${e.detail ?? "The entry or the record changed since your preview."} Preview it again before you go on.`);
      } else {
        setError(plainError(e));
      }
    } finally {
      setBusy(false);
    }
  };
  const blocked = !preview || preview.problems.length > 0 || (preview.warnings.length > 0 && !ack);
  // live only when the server's preview says nothing is missing; the server refuses a live
  // run the page did not show as live
  const live = !!preview && preview.live_missing.length === 0;

  return (
    <li className="rounded-xl bg-bg p-3 shadow-lift" data-testid="write-entry">
      <p className="text-[13px] font-medium">{(view.intent as { operation: string }).operation.replaceAll("_", " ")}</p>
      <p className="text-[12px] text-muted" data-testid="write-status">{view.text}</p>
      {!preview && (
        <Button size="sm" variant="secondary" disabled={busy} onClick={() => run(async () => setPreview(await previewWrite(id)))} data-testid="write-preview">
          {error ? "Preview again" : "Preview"}
        </Button>
      )}
      {preview && (
        <div className="mt-2 space-y-2 text-[12px]">
          <table className="w-full" data-testid="write-changes">
            <thead>
              <tr className="text-muted"><th className="text-left">Field</th><th className="text-left">Now in BC</th><th className="text-left">After</th></tr>
            </thead>
            <tbody>
              {preview.changes.map((c) => (
                <tr key={c.field} className={c.changed ? "font-medium" : "text-muted"}>
                  <td>{c.field}</td><td>{show(c.current)}</td><td>{show(c.proposed)}</td>
                </tr>
              ))}
              {preview.lines.map((l) => (
                <tr key={`line-${l.n}`}>
                  <td>Line {l.n}</td><td>new</td><td>{Object.entries(l.fields).map(([k, v]) => `${k} ${show(v)}`).join(", ")}{l.amount ? ` = ${l.amount}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {preview.problems.map((p) => <p key={p} className="text-late" data-testid="write-problem">{p}</p>)}
          {preview.warnings.map((w) => <p key={w.code + w.text} className="text-amber-700" data-testid="write-warning">{w.text}</p>)}
          {preview.warnings.length > 0 && (
            <label className="flex items-center gap-2"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} data-testid="write-ack" />I checked these warnings</label>
          )}
          <p className="font-mono text-[11px] break-all text-muted" data-testid="write-request">{show(preview.request.method ?? "not writable")} {show(preview.request.path ?? "")}</p>
          <p className="text-muted">Undo: {preview.undo}</p>
          {view.status === "prepared" && !armed && (
            <Button size="sm" disabled={busy || blocked} onClick={() => setArmed(true)} data-testid="write-approve">Approve &amp; write</Button>
          )}
          {view.status === "prepared" && armed && (
            <Button size="sm" disabled={busy || blocked} onClick={() => run(async () => {
              onChange(await approveWrite(id, { confirm: true, preview_hash: preview.preview_hash, acknowledge_warnings: ack }));
              setArmed(false);
            })} data-testid="write-confirm">Confirm approval</Button>
          )}
          {view.status === "approved" && (
            <>
              {live ? (
                <p className="text-late" data-testid="write-mode">This writes to Business Central now. It cannot be taken back except by the undo above.</p>
              ) : (
                <p className="text-muted" data-testid="write-mode">Dry run: nothing is written to Business Central{preview?.live_missing.length ? ` (${preview.live_missing.join("; ")})` : ""}.</p>
              )}
              <Button size="sm" disabled={busy} onClick={() => run(async () => onChange(await executeWrite(id, { idempotency_key: key, checked_bc: false, live })))} data-testid="write-execute">
                {live ? "Write to Business Central" : "Write (dry run)"}
              </Button>
            </>
          )}
          {view.request && <p className="text-muted" data-testid="write-result">{view.text}</p>}
        </div>
      )}
      {error && <p className="text-[12px] text-late" role="alert">{error}</p>}
    </li>
  );
}

/** "Approve & write": the prepared business-system entries concerning this card or record. */
export function WritePanel({ match }: { match: string[] }) {
  const [items, setItems] = useState<WriteIntentView[] | null>(null);
  const key = match.join("|");
  const [reads, setReads] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    getWrites(ctl.signal)
      .then((list) => setItems(list.items.filter((v) => concerns(v, key.split("|")))))
      .catch(() => !ctl.signal.aborted && setItems((cur) => cur ?? []));
    return () => ctl.abort();
  }, [key, reads]);
  const stale = useCallback(() => setReads((n) => n + 1), []);
  const replace = useCallback((next: WriteIntentView) => {
    const id = (next.intent as { intent_id: string }).intent_id;
    setItems((prev) => (prev ?? []).map((v) => ((v.intent as { intent_id: string }).intent_id === id ? next : v)));
  }, []);
  if (!items || items.length === 0) return null;
  return (
    <section aria-label="Approve and write" data-testid="write-panel" className="space-y-2">
      <h3 className="text-[13px] font-medium">Approve &amp; write</h3>
      <ul className="space-y-2">
        {items.map((v) => <WriteEntry key={(v.intent as { intent_id: string }).intent_id} view={v} onChange={replace} onStale={stale} />)}
      </ul>
    </section>
  );
}
