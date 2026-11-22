"use client";

import { useEffect, useState } from "react";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { getRecordList } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { RecordList as List } from "@/lib/types";
import { Section } from "./biz-page";

/**
 * A Records list from the business tables: a page of rows with record links, the filters the
 * data offers (each choice with how many it holds) and Show more. Changing a filter starts the
 * list again from the first row; a failed read says so with Retry, never an empty list.
 */
export function RecordList({ view, query }: { view: string; query: string }) {
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [page, setPage] = useState<List | null>(null);
  const [rows, setRows] = useState<List["table"]["rows"]>([]);
  const [problem, setProblem] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [round, setRound] = useState(0);
  const [offset, setOffset] = useState(0);
  const key = JSON.stringify([view, filters, query]);
  useEffect(() => {
    setOffset(0);
    setRows([]);
  }, [key]);
  useEffect(() => {
    const ctl = new AbortController();
    setProblem(null);
    setBusy(true);
    getRecordList(view, { ...filters, q: query, offset }, ctl.signal)
      .then((p) => {
        setPage(p);
        setRows((r) => (offset === 0 ? p.table.rows : [...r, ...p.table.rows]));
      }, (e) => !ctl.signal.aborted && setProblem(e))
      .finally(() => !ctl.signal.aborted && setBusy(false));
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, offset, round]);
  if (problem) return <LoadProblem message={plainError(problem)} onRetry={() => setRound((n) => n + 1)} />;
  if (!page) return <Pending label="Reading the list" />;
  return (
    <section aria-label={page.title} data-testid={`record-list-${view}`}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[13px]">
        {page.filters.map((f) => (
          <label key={f.key} className="flex items-center gap-1.5 text-muted">
            {f.label}
            <select
              aria-label={f.label}
              className="h-8 cursor-pointer rounded-md border border-border bg-bg px-2 text-[13px] text-fg"
              value={filters[f.key] ?? ""}
              onChange={(e) => setFilters((cur) => ({ ...cur, [f.key]: e.target.value }))}
            >
              {f.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                  {o.count != null ? ` (${o.count})` : ""}
                </option>
              ))}
            </select>
          </label>
        ))}
        <span className="tabular-nums text-muted" data-testid="record-list-total">{page.total} in all</span>
      </div>
      <Section s={{ ...page.table, title: page.title, rows }} />
      {page.more && (
        <button type="button" disabled={busy} onClick={() => setOffset(page.next_offset)} className="mt-3 cursor-pointer text-[13px] font-medium underline decoration-border-strong underline-offset-2 disabled:opacity-60">
          {busy ? "Reading" : "Show more"}
        </button>
      )}
    </section>
  );
}
