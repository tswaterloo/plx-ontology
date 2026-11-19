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
  const readable = entries.filter((e) => e.text);
  const hidden = entries.length - readable.length;
  if (!entries.length) return null;
  return (
    <div data-testid={testId}>
      <p className="text-[12px] text-muted">{label}</p>
      {readable.length > 0 && (
        <ul className="mt-0.5 space-y-0.5 text-[12px]">
          {readable.slice(0, all ? undefined : SHOWN).map((e) => (
            <li key={e.id} className="break-words">
              {e.kind ? <span className="text-muted">{e.kind.replaceAll("_", " ")}: </span> : null}
              {e.text}
            </li>
          ))}
        </ul>
      )}
      <ShowMore hidden={readable.length - SHOWN} open={all} onToggle={() => setAll((v) => !v)} />
      {hidden > 0 && (
        <p className="text-[12px] text-muted">
          {hidden === 1 ? "1 entry" : `${hidden} entries`} you can't read: someone else's memory, or since changed or deleted
        </p>
      )}
    </div>
  );
}

/** The memory and template behind a draft, with the entries read but not applied kept apart. */
export function DraftUse({ use }: { use: DraftUseData | null }) {
  if (!use) return null;
  const memory = use.memory ?? [];
  const related = use.related ?? [];
  const template = use.template ?? null;
  if (!memory.length && !related.length && !template) return null;
  return (
    <div className="space-y-1.5" aria-label="Used for this draft" data-testid="autopilot-draft-use">
      {template && (
        <p className="text-[12px]">
          <span className="text-muted">Template: </span>
          {template.name ?? "one you can't open"}
          {template.doc_type ? <span className="text-muted"> ({template.doc_type.replaceAll("_", " ")})</span> : null}
        </p>
      )}
      <Entries label="Applied" entries={memory} testId="draft-use-applied" />
      <Entries label="Related, not applied" entries={related} testId="draft-use-related" />
    </div>
  );
}
