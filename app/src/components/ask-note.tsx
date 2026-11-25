"use client";

import { StickyNote } from "lucide-react";
import { useState } from "react";
import { saveMemory } from "@/lib/api";
import { plainError } from "@/lib/errors";

/** One-click context or standing instruction under an answer: private to this person,
 * immediately reloaded and used by Ask, briefs and Assist. */
export function AskNote({ record, inline = false }: { record?: string; inline?: boolean }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"fact" | "instruction">("fact");
  const [text, setText] = useState("");
  const [savedKind, setSavedKind] = useState<"fact" | "instruction" | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  async function save() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await saveMemory({ kind, text: text.trim(), ...(record && kind === "fact" ? { records: [record] } : {}) });
      setSavedKind(kind);
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
          onClick={() => { setOpen(true); setSavedKind(null); }}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-bg px-2.5 text-[12.5px] font-medium text-muted hover:border-border-strong hover:bg-hover hover:text-fg cursor-pointer"
          data-testid="ask-note-open"
        >
          <StickyNote className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
          Add context
        </button>
        {savedKind && (
          <span role="status">
            {savedKind === "instruction"
              ? "Instruction saved. Private to you; applies to Ask, briefs and Assist right away."
              : "Noted. Private to you; Ask, briefs and Assist use it."}
          </span>
        )}
      </span>
    );
  }
  return (
    <div className="order-last mt-2 w-full space-y-2 rounded-lg border border-border bg-surface/60 p-3" data-testid="ask-note">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="block text-meta font-medium text-muted" htmlFor="ask-note-text">
          {kind === "instruction" ? "Standing instruction (private to you)" : "Add context (private to you)"}
        </label>
        <div className="inline-flex items-center rounded-md border border-border bg-bg p-0.5 text-[11.5px]" role="radiogroup" aria-label="Note type">
          <button
            type="button"
            role="radio"
            aria-checked={kind === "fact"}
            onClick={() => setKind("fact")}
            className={`rounded px-2 py-0.5 font-medium cursor-pointer ${kind === "fact" ? "bg-selected text-fg" : "text-muted hover:text-fg"}`}
            data-testid="ask-note-kind-fact"
          >
            Context note
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={kind === "instruction"}
            onClick={() => setKind("instruction")}
            className={`rounded px-2 py-0.5 font-medium cursor-pointer ${kind === "instruction" ? "bg-selected text-fg" : "text-muted hover:text-fg"}`}
            data-testid="ask-note-kind-instruction"
          >
            Instruction
          </button>
        </div>
      </div>
      <textarea
        id="ask-note-text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={2}
        maxLength={1000}
        className="w-full rounded-md border border-border bg-bg p-2 text-[13px] text-fg focus:border-accent focus:outline-none"
        placeholder={kind === "instruction" ? "e.g. Always show amounts in CAD first, or group by supplier" : "What you know that the records do not"}
      />
      <div className="flex items-center gap-2 text-[13px]">
        <button type="button" onClick={save} disabled={busy || !text.trim()} className="inline-flex h-8 items-center rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg hover:brightness-110 disabled:opacity-50 cursor-pointer" data-testid="ask-note-save">{busy ? "Saving" : "Save"}</button>
        <button type="button" onClick={() => setOpen(false)} className="inline-flex h-8 items-center rounded-md px-2.5 text-[12.5px] text-muted hover:bg-hover hover:text-fg cursor-pointer">Cancel</button>
      </div>
      {problem && <p role="alert" className="text-[12px] text-late">{problem}</p>}
    </div>
  );
}
