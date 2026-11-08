"use client";

import { useCallback, useEffect, useState } from "react";
import { approveWrite, executeWrite, getWrites, previewWrite } from "@/lib/api";
import { newIdempotencyKey } from "@/lib/actions";
import { plainError } from "@/lib/errors";
import type { WriteIntentView, WritePreview } from "@/lib/types";
import { Button } from "./ui/button";

/** The write entries that concern a card or record: their record id, a citation or the
 * reason names one of `match`. */
export function concerns(view: WriteIntentView, match: string[]): boolean {
  const intent = view.intent as { record_id?: string | null; citations?: string[]; precondition?: { party_id?: string } };
  const ids = match.filter(Boolean).map((m) => m.toLowerCase());
  if (ids.length === 0) return false;
  const hay = [intent.record_id ?? "", intent.precondition?.party_id ?? "", ...(intent.citations ?? [])].map((h) => h.toLowerCase());
  return ids.some((id) => hay.some((h) => h === id || h.includes(id)));
}

const show = (v: unknown) => (v === null || v === undefined || v === "" ? "—" : String(v));

/** One entry: preview (field by field), approve with a second confirm click, then run as a
 * dry run. Live writing is off; the result says what a live write still lacks. */
function WriteEntry({ view, onChange }: { view: WriteIntentView; onChange: (v: WriteIntentView) => void }) {
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
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const blocked = !preview || preview.problems.length > 0 || (preview.warnings.length > 0 && !ack);

  return (
    <li className="rounded-xl bg-bg p-3 shadow-lift" data-testid="write-entry">
      <p className="text-[13px] font-medium">{(view.intent as { operation: string }).operation.replaceAll("_", " ")}</p>
      <p className="text-[12px] text-muted" data-testid="write-status">{view.text}</p>
      {!preview && (
        <Button size="sm" variant="secondary" disabled={busy} onClick={() => run(async () => setPreview(await previewWrite(id)))} data-testid="write-preview">
          Preview
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
          {preview.problems.map((p) => <p key={p} className="text-red-700" data-testid="write-problem">{p}</p>)}
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
            <Button size="sm" disabled={busy} onClick={() => run(async () => onChange(await executeWrite(id, { idempotency_key: key, checked_bc: false })))} data-testid="write-execute">
              Write (dry run)
            </Button>
          )}
          {view.request && <p className="text-muted" data-testid="write-result">{view.text}</p>}
        </div>
      )}
      {error && <p className="text-[12px] text-red-700" role="alert">{error}</p>}
    </li>
  );
}

/** "Approve & write": the prepared business-system entries concerning this card or record. */
export function WritePanel({ match }: { match: string[] }) {
  const [items, setItems] = useState<WriteIntentView[] | null>(null);
  const key = match.join("|");
  useEffect(() => {
    const ctl = new AbortController();
    getWrites(ctl.signal)
      .then((list) => setItems(list.items.filter((v) => concerns(v, key.split("|")))))
      .catch(() => setItems([]));
    return () => ctl.abort();
  }, [key]);
  const replace = useCallback((next: WriteIntentView) => {
    const id = (next.intent as { intent_id: string }).intent_id;
    setItems((prev) => (prev ?? []).map((v) => ((v.intent as { intent_id: string }).intent_id === id ? next : v)));
  }, []);
  if (!items || items.length === 0) return null;
  return (
    <section aria-label="Approve and write" data-testid="write-panel" className="space-y-2">
      <h3 className="text-[13px] font-medium">Approve &amp; write</h3>
      <ul className="space-y-2">
        {items.map((v) => <WriteEntry key={(v.intent as { intent_id: string }).intent_id} view={v} onChange={replace} />)}
      </ul>
    </section>
  );
}
