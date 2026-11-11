"use client";

// Your Ask session as saved work: every Ask is kept in your work log (operator_api/work_log.py);
// here you pin the session, name it, file it under a case, or follow its records on your watchlist.

import { FolderInput } from "lucide-react";
import { useState } from "react";
import { organiseWorkThread, setFollowing } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { ObjectRef } from "@/lib/types";
import { cn } from "@/lib/cn";

const input = "h-8 min-w-0 flex-1 rounded-md bg-bg px-2 text-[13px] shadow-lift focus:outline-none";

export function AskOrganizer({ conversationId, records, className }: { conversationId: string; records: ObjectRef[]; className?: string }) {
  const [name, setName] = useState("");
  const [kase, setKase] = useState("");
  const [said, setSaid] = useState<string | null>(null);
  const keep = () =>
    organiseWorkThread(`ask:${conversationId}`, { name: name.trim() || undefined, case_id: kase.trim().replace(/[^A-Za-z0-9_.:-]+/g, "-") || undefined, pinned: true }).then(
      () => setSaid("Kept: find it in your work log, on its records' pages, or by its case."),
      (e) => setSaid(plainError(e)),
    );
  const watch = () =>
    Promise.all(records.map((r) => setFollowing(r.object_id, true))).then(
      () => setSaid(`Following ${records.length} record${records.length === 1 ? "" : "s"} on your watchlist.`),
      (e) => setSaid(plainError(e)),
    );
  return (
    <details className={cn("group", className)} data-testid="ask-organize">
      <summary className="inline-flex h-8 cursor-pointer list-none items-center gap-1.5 rounded-lg px-2.5 text-[13px] text-muted hover:bg-hover hover:text-fg [&::-webkit-details-marker]:hidden">
        <FolderInput className="size-[15px]" strokeWidth={2} aria-hidden />
        <span className="max-sm:hidden">Organize</span>
      </summary>
      <div className="mt-2 flex w-full flex-wrap items-center gap-2">
        <input className={input} value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" aria-label="Name" maxLength={120} />
        <input className={input} value={kase} onChange={(e) => setKase(e.target.value)} placeholder="Case" aria-label="Case" maxLength={80} />
        <button type="button" onClick={() => void keep()} className="h-8 rounded-md px-2.5 text-[13px] font-medium hover:bg-hover cursor-pointer">
          Keep
        </button>
        {records.length > 0 && (
          <button type="button" onClick={() => void watch()} className="h-8 rounded-md px-2.5 text-[13px] text-muted hover:bg-hover hover:text-fg cursor-pointer">
            Watch its records
          </button>
        )}
        {said && <p className="w-full text-[12px] leading-4 text-muted">{said}</p>}
      </div>
    </details>
  );
}
