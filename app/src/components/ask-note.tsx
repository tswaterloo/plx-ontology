"use client";

import { useState } from "react";
import { saveMemory } from "@/lib/api";
import { plainError } from "@/lib/errors";

/** One-click context under an answer: a private note (about the record the question was asked on,
 * when there is one) that Ask, the briefs and Assist read back for this person only. */
export function AskNote({ record }: { record?: string }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  async function save() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await saveMemory({ kind: "fact", text: text.trim(), ...(record ? { records: [record] } : {}) });
      setSaved(true);
      setText("");
      setOpen(false);
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  }
  if (!open) {
    return (
      <p className="mt-2 text-[12px] text-muted" data-testid="ask-note">
        <button type="button" onClick={() => { setOpen(true); setSaved(false); }} className="underline underline-offset-2" data-testid="ask-note-open">Add context</button>
        {saved && <span role="status"> Noted. Private to you; Ask, briefs and Assist use it.</span>}
      </p>
    );
  }
  return (
    <div className="mt-2 space-y-2" data-testid="ask-note">
      <label className="block text-meta text-muted" htmlFor="ask-note-text">Add context (private to you)</label>
      <textarea id="ask-note-text" value={text} onChange={(e) => setText(e.target.value)} rows={2} maxLength={1000} className="w-full rounded-lg border border-border bg-transparent p-2 text-[13px]" placeholder="What you know that the records do not" />
      <div className="flex gap-3 text-[13px]">
        <button type="button" onClick={save} disabled={busy || !text.trim()} className="font-medium underline underline-offset-2 disabled:opacity-50" data-testid="ask-note-save">{busy ? "Saving" : "Save"}</button>
        <button type="button" onClick={() => setOpen(false)} className="text-muted underline underline-offset-2">Cancel</button>
      </div>
      {problem && <p role="alert" className="text-[12px] text-muted">{problem}</p>}
    </div>
  );
}
