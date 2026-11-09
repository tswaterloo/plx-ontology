"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getAsOf, getHistory } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import type { HistoryEntry, RecordAsOf } from "@/lib/types";
import { nodeHref } from "./link-explorer";
import { SectionTitle } from "./record/parts";

function Values({ items }: { items: string[] }) {
  if (!items.length) return <span className="text-faint">none</span>;
  return <>{items.join(", ")}</>;
}

function Entry({ e }: { e: HistoryEntry }) {
  return (
    <li className="border-b border-border py-2.5 last:border-b-0" data-testid="history-entry" data-kind={e.kind}>
      <div className="flex items-baseline gap-3">
        <span className="w-36 shrink-0 text-[12px] leading-5 tabular-nums text-muted">{formatDateTime(e.at)}</span>
        <div className="min-w-0 flex-1">
          <p className="text-[13px] leading-5">
            {e.link ? (
              <>
                {e.kind === "link_added" ? "Linked to " : "Unlinked from "}
                <Link href={nodeHref(e.link)} className="underline decoration-border-strong underline-offset-2 hover:decoration-fg">
                  {e.link.label}
                </Link>
                {e.link_label && <span className="text-muted"> · {e.link_label}</span>}
                {e.link.tier === "suggested" && <span className="text-muted"> · suggested</span>}
              </>
            ) : (
              e.title
            )}
          </p>
          {e.source && <p className="text-[12px] leading-4 text-muted">{e.link ? `By ${e.source}` : e.source}</p>}
          {(e.changes ?? []).length > 0 && (
            <dl className="mt-1.5 space-y-1 text-[12px] leading-4">
              {(e.changes ?? []).map((c) => (
                <div key={c.property} className="flex gap-2" data-testid="history-change">
                  <dt className="w-36 shrink-0 text-muted">{c.label}</dt>
                  <dd className="min-w-0 flex-1 break-words">
                    {e.kind === "changed" && (
                      <>
                        <span className="text-muted line-through decoration-faint">
                          <Values items={c.before ?? []} />
                        </span>
                        <span aria-hidden> → </span>
                        <span className="sr-only"> changed to </span>
                      </>
                    )}
                    <Values items={c.after ?? []} />
                    {c.more && <span className="text-muted"> (more values)</span>}
                  </dd>
                </div>
              ))}
              {e.more_changes && <p className="text-muted">More fields changed than are shown.</p>}
            </dl>
          )}
        </div>
      </div>
    </li>
  );
}

/** The record as recorded at a chosen date: its values and its links counted per kind. */
function AsOf({ type, id }: { type: string; id: string }) {
  const [day, setDay] = useState("");
  const [data, setData] = useState<RecordAsOf | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setDay("");
    setData(null);
    setError(null);
  }, [type, id]);

  const read = () => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
    setBusy(true);
    setError(null);
    getAsOf(type, id, day)
      .then((d) => setData(d))
      .catch((e) => {
        setData(null);
        setError(e?.status === 404 ? "This record was not yet recorded then, or you can't see it." : plainError(e));
      })
      .finally(() => setBusy(false));
  };

  return (
    <section data-testid="as-of">
      <SectionTitle>As recorded on a date</SectionTitle>
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(ev) => {
          ev.preventDefault();
          read();
        }}
      >
        <label className="text-[13px] text-muted" htmlFor="as-of-day">
          Date
        </label>
        <input id="as-of-day" type="date" value={day} onChange={(ev) => setDay(ev.target.value)} className="rounded-lg border border-border bg-transparent px-2 py-1 text-[13px]" data-testid="as-of-day" />
        <button type="submit" disabled={busy || !day} className="cursor-pointer rounded-lg border border-border px-3 py-1 text-[13px] hover:bg-hover disabled:cursor-default disabled:opacity-50" data-testid="as-of-show">
          {busy ? "Reading" : "Show"}
        </button>
      </form>
      {error && <p className="mt-2 text-[13px] text-muted" role="status" data-testid="as-of-error">{error}</p>}
      {data && (
        <div className="mt-3 space-y-3" data-testid="as-of-result">
          <p className="text-[12px] text-muted">As recorded at {formatDateTime(data.at)}</p>
          {(data.values ?? []).length > 0 ? (
            <dl className="space-y-1 text-[13px] leading-5">
              {(data.values ?? []).map((v) => (
                <div key={v.label} className="flex gap-3">
                  <dt className="w-40 shrink-0 text-muted">{v.label}</dt>
                  <dd className="min-w-0 flex-1 break-words">{(v.values ?? []).join(", ")}</dd>
                </div>
              ))}
            </dl>
          ) : (
            !data.message && <p className="text-[13px] text-muted">No values you may see were recorded then.</p>
          )}
          {(data.links ?? []).length > 0 && (
            <ul className="space-y-0.5 text-[13px] leading-5">
              {(data.links ?? []).map((l) => (
                <li key={l.label}>
                  <span className="tabular-nums">{l.count}</span> <span className="text-muted">{l.label}</span>
                </li>
              ))}
            </ul>
          )}
          {data.message && <p className="text-[13px] text-muted" role="status">{data.message}</p>}
        </div>
      )}
    </section>
  );
}

/**
 * A record's History tab: every change to its values and links, newest first, with when
 * and which system wrote it, read a page at a time; and the record as recorded on a date.
 * Every read is as this reader: a change they may not see is not shown, and the tab says
 * some are hidden without a number.
 */
export function RecordHistory({ type, id }: { type: string; id: string }) {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [partial, setPartial] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const c = new AbortController();
    setEntries([]);
    setCursor(null);
    setPartial(false);
    setMessage(null);
    setError(null);
    setLoading(true);
    getHistory(type, id, null, c.signal)
      .then((d) => {
        if (c.signal.aborted) return;
        setEntries(d.entries ?? []);
        setCursor(d.next_cursor ?? null);
        setPartial(!!d.partial);
        setMessage(d.message ?? null);
      })
      .catch((e) => !c.signal.aborted && setError(e?.status === 404 ? "You can't see this record's history." : plainError(e)))
      .finally(() => !c.signal.aborted && setLoading(false));
    return () => c.abort();
  }, [type, id]);

  const older = () => {
    if (!cursor) return;
    setLoading(true);
    getHistory(type, id, cursor)
      .then((d) => {
        setEntries((xs) => [...xs, ...(d.entries ?? [])]);
        setCursor(d.next_cursor ?? null);
        setPartial((p) => p || !!d.partial);
      })
      .catch((e) => setError(plainError(e)))
      .finally(() => setLoading(false));
  };

  return (
    <div className="space-y-8" data-testid="record-history">
      <section aria-live="polite">
        <SectionTitle>Changes</SectionTitle>
        {error && <p className="text-[13px] text-muted" role="status" data-testid="history-error">{error}</p>}
        {entries.length > 0 && (
          <ul>
            {entries.map((e, i) => (
              <Entry key={`${e.at}-${e.kind}-${i}`} e={e} />
            ))}
          </ul>
        )}
        {!error && loading && <p className="mt-2 text-[13px] text-muted" role="status">Reading changes</p>}
        {!loading && !error && entries.length === 0 && message && <p className="text-[13px] text-muted" role="status">{message}</p>}
        {partial && (
          <p className="mt-2 text-[13px] text-muted" role="status" data-testid="history-partial">
            Some changes are not shown: they are of values or records you may not see.
          </p>
        )}
        {cursor && !loading && (
          <button type="button" onClick={older} className="mt-3 cursor-pointer rounded-lg border border-border px-3 py-1.5 text-[13px] hover:bg-hover" data-testid="history-older">
            Show older changes
          </button>
        )}
      </section>
      <AsOf type={type} id={id} />
    </div>
  );
}
