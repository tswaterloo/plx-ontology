"use client";

import { useEffect, useState } from "react";
import { getLabelChanges } from "@/lib/api";
import { plural } from "@/lib/format";
import type { LabelChanges } from "@/lib/types";
import { SectionTitle } from "./record/parts";

const STATE_WORDS: Record<string, string> = { requested: "Requested", approved: "Approved", done: "Done" };

// The item page's label and artwork changes (your own alert list): requested, approved,
// done, stale, or changed with no approval recorded. Hidden when there are none.
export function LabelChangesView({ data }: { data: LabelChanges }) {
  if (data.total === 0) return null;
  return (
    <section data-testid="label-changes" className="space-y-2">
      <SectionTitle>Label changes</SectionTitle>
      <p className="text-[13px] text-muted">
        {plural(data.total, "change")}
        {data.stale ? ` · ${data.stale} stale` : ""}
        {data.no_approval ? ` · ${data.no_approval} with no approval` : ""}
      </p>
      <ul className="space-y-1 text-[13px]">
        {(data.changes ?? []).map((c) => (
          <li key={c.id} data-testid="label-change">
            <span className="font-medium">{STATE_WORDS[c.state ?? ""] ?? "Requested"}</span>
            {c.requested_on ? ` · requested ${c.requested_on}` : ""}
            {c.approved_on ? ` · approved ${c.approved_on}` : ""}
            {c.done_on ? ` · done ${c.done_on}${c.done_by === "file" ? " (label file changed)" : ""}` : ""}
            {c.stale && <span className="ml-1 text-amber-700">· stale</span>}
            {c.no_approval && <span className="ml-1 text-red-700">· no approval recorded</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function LabelChangesSection({ objectId }: { objectId: string }) {
  const [data, setData] = useState<LabelChanges | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    getLabelChanges(objectId, ctl.signal).then(setData, () => setData(null));
    return () => ctl.abort();
  }, [objectId]);
  return data ? <LabelChangesView data={data} /> : null;
}
