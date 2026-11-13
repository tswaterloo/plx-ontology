"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { getBriefingUnjudged } from "@/lib/api";
import { cn } from "@/lib/cn";
import { objectHref } from "@/lib/format";
import type { BriefingDataNote, BriefingUnjudged } from "@/lib/types";

/** What the checks could not judge, one line per check and reason; each opens its records. */
export function DataNotes({ notes, className }: { notes: BriefingDataNote[] | undefined; className?: string }) {
  if (!notes?.length) return null;
  return (
    <ul className={cn("space-y-1.5 text-[12px] leading-[18px] text-muted", className)} data-testid="briefing-data-notes">
      {notes.map((n) => (
        <DataNote key={`${n.check}:${n.reason}`} note={n} />
      ))}
    </ul>
  );
}

/** A check's records it could not judge: the count, and on opening, the records themselves. */
function DataNote({ note }: { note: BriefingDataNote }) {
  const [page, setPage] = useState<BriefingUnjudged | null>(null);
  const [rows, setRows] = useState<NonNullable<BriefingUnjudged["records"]>>([]);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = (offset: number) => {
    setBusy(true);
    setFailed(false);
    getBriefingUnjudged(note.check, note.reason, offset)
      .then((got) => {
        setPage(got);
        setRows((had) => (offset === 0 ? got.records ?? [] : [...had, ...(got.records ?? [])]));
      })
      .catch(() => setFailed(true))
      .finally(() => setBusy(false));
  };
  return (
    <li>
      <details
        className="group/note"
        onToggle={(e) => {
          if ((e.currentTarget as HTMLDetailsElement).open && !page && !busy) load(0);
        }}
      >
        <summary className="flex cursor-pointer list-none items-start gap-1 hover:text-fg [&::-webkit-details-marker]:hidden">
          <ChevronRight className="mt-[3px] size-3.5 shrink-0 transition-transform group-open/note:rotate-90" aria-hidden />
          <span>{note.words.charAt(0).toUpperCase() + note.words.slice(1)}.</span>
        </summary>
        <div className="mt-1 pl-[18px]">
          {failed && <p>Couldn&apos;t load the records. Close and open this again to retry.</p>}
          {!failed && !page && <p>Loading</p>}
          {page && rows.length === 0 && <p>None of them are records you can open.</p>}
          {rows.length > 0 && (
            <ul className="space-y-0.5" data-testid="data-note-records">
              {rows.map((r) => (
                <li key={r.id}>
                  <Link href={objectHref(r.type, r.id)} className="text-fg hover:underline">
                    {r.label}
                  </Link>
                </li>
              ))}
            </ul>
          )}
          {page?.more && (
            <button type="button" onClick={() => load(page.next_offset)} disabled={busy} className="mt-1 font-medium hover:text-fg disabled:opacity-60 cursor-pointer">
              {busy ? "Loading" : `Show ${page.total - page.next_offset} more`}
            </button>
          )}
          {page && !page.complete && <p className="mt-1">The check also skipped records it couldn&apos;t name, so this list may be short.</p>}
        </div>
      </details>
    </li>
  );
}
