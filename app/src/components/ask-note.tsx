"use client";

import { StickyNote } from "lucide-react";
import { useState } from "react";
import { saveMemory } from "@/lib/api";
import { plainError } from "@/lib/errors";

/** One-click context under an answer: a private note (about the record the question was asked on,
 * when there is one) that Ask, the briefs and Assist read back for this person only. */
export function AskNote({ record, inline = false }: { record?: string; inline?: boolean }) {
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
      <span className={inline ? "inline-flex items-center gap-2 text-[12px] text-muted" : "mt-2 inline-flex items-center gap-2 text-[12px] text-muted"} data-testid="ask-note">
        <button
          type="button"
          onClick={() => { setOpen(true); setSaved(false); }}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
          data-testid="ask-note-open"
        >
          <StickyNote className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
          Add context
        </button>
        {saved && <span role="status">Noted. Private to you; Ask, briefs and Assist use it.</span>}
      </span>
    );
  }
  return (
    <div className="order-last mt-2 w-full space-y-2 rounded-lg border border-border bg-surface/60 p-3" data-testid="ask-note">
      <label className="block text-meta font-medium text-muted" htmlFor="ask-note-text">Add context (private to you)</label>
      <textarea id="ask-note-text" value={text} onChange={(e) => setText(e.target.value)} rows={2} maxLength={1000} className="w-full rounded-md border border-border bg-bg p-2 text-[13px] text-fg focus:border-accent focus:outline-none" placeholder="What you know that the records do not" />
      <div className="flex items-center gap-2 text-[13px]">
        <button type="button" onClick={save} disabled={busy || !text.trim()} className="inline-flex h-8 items-center rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg hover:brightness-110 disabled:opacity-50 cursor-pointer" data-testid="ask-note-save">{busy ? "Saving" : "Save"}</button>
        <button type="button" onClick={() => setOpen(false)} className="inline-flex h-8 items-center rounded-md px-2.5 text-[12.5px] text-muted hover:bg-hover hover:text-fg cursor-pointer">Cancel</button>
      </div>
      {problem && <p role="alert" className="text-[12px] text-late">{problem}</p>}
    </div>
  );
}
