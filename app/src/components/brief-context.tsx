"use client";

import { MessageCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { saveMemory } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { Brief } from "@/lib/types";

/** What a note on a brief can be about: the meeting itself, then each party it is with. */
export function noteTargets(brief: Brief): { id: string; label: string }[] {
  const out: { id: string; label: string }[] = [];
  if (brief.subject) out.push({ id: brief.subject.object_id, label: brief.subject.type === "CalendarEvent" || brief.subject.type === "Meeting" ? "This meeting" : brief.subject.label });
  for (const p of brief.parties ?? []) if (!out.some((t) => t.id === p.object_id)) out.push({ id: p.object_id, label: p.label });
  return out;
}

/** Where "Ask about this brief" goes: Ask, with the brief as the question's context. */
export function askAboutBriefHref(brief: Brief): string | null {
  if (!brief.subject) return null;
  const q = `Using this brief, what should I know and do before ${brief.title}?`;
  return `/ask?q=${encodeURIComponent(q)}&object=${encodeURIComponent(brief.subject.object_id)}&brief=${encodeURIComponent(brief.subject.type)}`;
}

/** One-click context on a brief: a private note about the meeting or a party. It is read back
 * in this brief, in Ask and in Assist (the person's own memory, never shared). */
export function AddContext({ brief, onSaved }: { brief: Brief; onSaved: () => void }) {
  const targets = noteTargets(brief);
  const [text, setText] = useState("");
  const [about, setAbout] = useState(targets[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (targets.length === 0) return null;
  const ask = askAboutBriefHref(brief);
  async function save() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await saveMemory({ kind: "fact", text: text.trim(), records: [about] });
      setText("");
      onSaved();
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-2 border-t border-border pt-3" data-testid="add-context">
      <label className="block text-meta text-muted" htmlFor="brief-note">Add context (private to you; used in this brief, in Ask and in Assist)</label>
      <textarea id="brief-note" value={text} onChange={(e) => setText(e.target.value)} rows={2} maxLength={1000} className="w-full rounded-lg border border-border bg-transparent p-2 text-[13px]" placeholder="What you know that the records do not: a decision, a promise, something already handled" />
      <div className="flex flex-wrap items-center gap-3">
        {targets.length > 1 && (
          <select aria-label="About" value={about} onChange={(e) => setAbout(e.target.value)} className="h-8 rounded-lg border border-border bg-transparent px-2 text-[13px]">
            {targets.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
          </select>
        )}
        <button type="button" onClick={save} disabled={busy || !text.trim()} className="inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium disabled:opacity-50" data-testid="add-context-save">
          {busy ? "Saving" : "Add context"}
        </button>
        {ask && (
          <Link href={ask} className="inline-flex h-8 items-center gap-1.5 text-[13px] font-medium underline underline-offset-2" data-testid="ask-about-brief">
            <MessageCircle className="size-[15px]" strokeWidth={2} aria-hidden />
            Ask about this brief
          </Link>
        )}
      </div>
      {problem && <p role="alert" className="text-[12px] text-muted">{problem}</p>}
    </div>
  );
}
