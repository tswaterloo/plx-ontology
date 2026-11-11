"use client";

import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getSourceRecords } from "@/lib/api";
import { ApiError } from "@/lib/errors";
import { recordLabel, safeHref, typeLabel } from "@/lib/format";
import type { SourceRecords } from "@/lib/types";

type Row = NonNullable<SourceRecords["records"]>[number];

/** The records citing a source, read as the reader may (null while reading; "failed" when it couldn't be read). */
export function useSourceRecords(handle: string): SourceRecords | null | "failed" {
  const [got, setGot] = useState<SourceRecords | null | "failed">(null);
  useEffect(() => {
    const c = new AbortController();
    setGot(null);
    getSourceRecords(handle, c.signal)
      .then(setGot)
      // a source opened through the reader's own host (an attached original) has no record
      // list here: not found reads as nothing to show, never as a failure
      .catch((e) => !c.signal.aborted && setGot(e instanceof ApiError && e.status === 404 ? { handle, records: [], served: false, truncated: false } : "failed"));
    return () => c.abort();
  }, [handle]);
  return got;
}

/** "Read into PO 90412 and 2 more records": the one line under a source's title. */
export function citedLine(got: SourceRecords): string | null {
  const rows = got.records ?? [];
  if (!got.served || rows.length === 0) return null;
  const own = rows.find((r) => r.how === "from") ?? rows[0];
  const name = recordLabel(own.record.type, own.record.label);
  const more = rows.length - 1;
  const lead = own.how === "from" ? `Read into ${name}` : `Linked to ${name}`;
  return more > 0 ? `${lead} and ${more} more record${more === 1 ? "" : "s"}${got.truncated ? " (first 40 listed)" : ""}` : lead;
}

function RecordRow({ row }: { row: Row }) {
  const href = safeHref(row.record.open_url);
  const name = recordLabel(row.record.type, row.record.label);
  const inner = (
    <>
      <span className="w-[84px] shrink-0 truncate text-[12px] leading-4 text-muted">{typeLabel(row.record.type)}</span>
      <span className="min-w-0 flex-1 truncate text-[13px] leading-5 font-medium">{name}</span>
      {row.link && <span className="shrink-0 text-[12px] leading-4 text-faint">{row.link}</span>}
      {href && <ArrowUpRight className="size-3.5 shrink-0 text-faint" aria-hidden />}
    </>
  );
  return (
    <li>
      {href ? (
        <Link href={href} className="-mx-2 flex items-center gap-2 rounded-[3px] px-2 py-1 hover:bg-hover">
          {inner}
        </Link>
      ) : (
        <span className="flex items-center gap-2 py-1">{inner}</span>
      )}
    </li>
  );
}

/** The source page's "On records" part: the records this source was read into, then the records linked to them. */
export function SourceRecordsPanel({ got }: { got: SourceRecords | null | "failed" }) {
  if (got === null)
    return (
      <section data-testid="source-records" aria-busy="true">
        <h2 className="mb-2 text-[12px] leading-4 font-medium text-muted">On records</h2>
        <p className="text-[12px] leading-4 text-faint">Reading which records cite it</p>
      </section>
    );
  if (got === "failed")
    return (
      <section data-testid="source-records">
        <h2 className="mb-2 text-[12px] leading-4 font-medium text-muted">On records</h2>
        <p className="text-[12px] leading-4 text-muted">The records citing it couldn&apos;t be read just now.</p>
      </section>
    );
  if (!got.served) return null;
  const records = got.records ?? [];
  const from = records.filter((r) => r.how === "from");
  const linked = records.filter((r) => r.how !== "from");
  return (
    <section data-testid="source-records">
      <h2 className="mb-2 text-[12px] leading-4 font-medium text-muted">
        On records <span className="num font-normal text-faint">{records.length}</span>
      </h2>
      {records.length === 0 && <p className="text-[12px] leading-4 text-muted">No record you can open cites it.</p>}
      {from.length > 0 && (
        <>
          <h3 className="mt-1 text-[11px] leading-4 tracking-[0.02em] text-faint">Read into</h3>
          <ul data-testid="source-records-from">
            {from.map((r) => (
              <RecordRow key={r.record.object_id} row={r} />
            ))}
          </ul>
        </>
      )}
      {linked.length > 0 && (
        <>
          <h3 className="mt-2 text-[11px] leading-4 tracking-[0.02em] text-faint">Linked to {from.length > 0 ? "those" : "it"}</h3>
          <ul data-testid="source-records-linked">
            {linked.map((r) => (
              <RecordRow key={r.record.object_id} row={r} />
            ))}
          </ul>
        </>
      )}
      {got.truncated && <p className="mt-2 text-[12px] leading-4 text-muted">More records cite it; the first 40 are listed.</p>}
    </section>
  );
}
