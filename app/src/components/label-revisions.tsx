"use client";

import { useEffect, useState } from "react";
import { getLabelRevisions } from "@/lib/api";
import { plural } from "@/lib/format";
import type { LabelRevisions } from "@/lib/types";
import { SectionTitle } from "./record/parts";

// A record's label and artwork revision history (your own alert list): each file's
// revisions newest first, the current one (the highest you can see), who approved each and
// when; on an order, the superseded revisions it still points at. Hidden when there are none.
export function LabelRevisionsView({ data }: { data: LabelRevisions }) {
  const onOrders = data.superseded_on_orders ?? [];
  if (data.revisions === 0 && onOrders.length === 0) return null;
  return (
    <section data-testid="label-revisions" className="space-y-2">
      <SectionTitle>Label revisions</SectionTitle>
      {onOrders.map((o) => (
        <p key={o.id} data-testid="label-revision-on-order" className="text-[13px] text-red-700">
          Points at {o.name ?? "a label file"} revision {o.revision}; current is {o.current}
          {o.superseded_on ? ` (since ${o.superseded_on})` : ""}
        </p>
      ))}
      {data.revisions > 0 && (
        <p className="text-[13px] text-muted">
          {plural(data.revisions, "revision")} of {plural((data.families ?? []).length, "file")}
          {data.unapproved_current ? ` · ${data.unapproved_current} current with no approval recorded` : ""}
        </p>
      )}
      {(data.families ?? []).map((f) => (
        <div key={f.family} data-testid="label-revision-family" className="text-[13px]">
          <div className="font-medium">{(f.revisions ?? [])[0]?.name ?? f.family}</div>
          <ul className="space-y-0.5">
            {(f.revisions ?? []).map((r) => (
              <li key={r.id} data-testid="label-revision">
                <span className="font-medium">{r.revision}</span>
                {r.current ? " · current" : r.superseded_on ? ` · superseded ${r.superseded_on}` : ""}
                {r.first_seen ? ` · first seen ${r.first_seen}` : ""}
                {r.approved_on ? ` · approved ${r.approved_on}${r.approved_by ? ` by ${r.approved_by}` : ""}` : ""}
                {!r.approved_on && r.current && <span className="ml-1 text-amber-700">· no approval recorded</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}

export function LabelRevisionsSection({ objectId }: { objectId: string }) {
  const [data, setData] = useState<LabelRevisions | null>(null);
  useEffect(() => {
    const ctl = new AbortController();
    getLabelRevisions(objectId, ctl.signal).then(setData, () => setData(null));
    return () => ctl.abort();
  }, [objectId]);
  return data ? <LabelRevisionsView data={data} /> : null;
}
