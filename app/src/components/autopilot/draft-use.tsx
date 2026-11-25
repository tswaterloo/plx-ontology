"use client";

import { useState } from "react";
import { ShowMore } from "@/components/ui/show-more";

// What an autopilot draft was written with (operator_api autopilot_area._apa_used):
// "memory" holds the entries that changed the draft, "related" the ones read for it that
// changed nothing. An entry's text comes only to the person whose memory it is, while they
// keep it; anyone else gets the id alone. Typed here until the API field reaches main's
// generated types.
export type MemoryUse = { id: number; kind?: string | null; text?: string | null };
export type DraftUseData = {
  memory?: MemoryUse[];
  related?: MemoryUse[];
  template?: { id: number; name?: string | null; doc_type?: string | null } | null;
};

export function draftUseOf(item: unknown): DraftUseData | null {
  const got = (item as { used_for_draft?: DraftUseData | null } | null)?.used_for_draft;
  return got && typeof got === "object" ? got : null;
}

const SHOWN = 5;

function Entries({ label, entries, testId }: { label: string; entries: MemoryUse[]; testId: string }) {
  const [all, setAll] = useState(false);
  // what the person may read, in the words they saved it in; never a stored key or a count of
  // entries they cannot open
  const readable = entries.filter((e) => e.text);
  if (!readable.length) return null;
  return (
    <div data-testid={testId}>
      <p className="text-[12px] text-muted">{label}</p>
      <ul className="mt-0.5 space-y-0.5 text-[12px]">
        {readable.slice(0, all ? undefined : SHOWN).map((e) => (
          <li key={e.id} className="break-words">{e.text}</li>
        ))}
      </ul>
      <ShowMore hidden={readable.length - SHOWN} open={all} onToggle={() => setAll((v) => !v)} />
    </div>
  );
}

/** What changed the draft: the template and the notes applied. What was only read for it is
 * not listed (raw stored memory is internal, and none of it changed the draft). */
export function DraftUse({ use }: { use: DraftUseData | null }) {
  if (!use) return null;
  const memory = (use.memory ?? []).filter((e) => e.text);
  const template = use.template ?? null;
  if (!memory.length && !template) return null;
  return (
    <div className="space-y-1.5" aria-label="Used for this draft" data-testid="autopilot-draft-use">
      {template && (
        <p className="text-[12px]">
          <span className="text-muted">Template: </span>
          {template.name ?? "one you can't open"}
          {template.doc_type ? <span className="text-muted"> ({template.doc_type.replaceAll("_", " ")})</span> : null}
        </p>
      )}
      <Entries label="Used for this draft" entries={memory} testId="draft-use-applied" />
    </div>
  );
}
