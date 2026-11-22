"use client";

import { useCallback, useEffect, useState } from "react";
import { InsightsDashboard, type TileEntry } from "@/components/insights/dashboard";
import { LoadProblem } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { ApiError } from "@/lib/errors";
import { getInsights, keepInsights, keptInsights, streamInsights } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { InsightList, InsightResult } from "@/lib/types";

/** Insights: the built-in team, org and customer views and the charts you pinned, each read
 * live as you (the server decides whose rows you see). */
export default function InsightsPage() {
  // what the last visit showed is drawn at once; the one request below then replaces it
  const [data, setData] = useState<InsightList | null>(() => keptInsights()?.list ?? null);
  const [feed, setFeed] = useState<Record<string, TileEntry> | undefined>(() => {
    const k = keptInsights();
    return k ? Object.fromEntries(Object.entries(k.tiles).map(([id, result]) => [id, { result }])) : undefined;
  });
  const [streaming, setStreaming] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setStreaming(true);
    let list: InsightList | null = null;
    const tiles: Record<string, InsightResult> = {};
    const fresh = new Set<string>();
    const shown = keptInsights() !== undefined;
    const done = (ok: boolean) => {
      if (ok && list) keepInsights(list, tiles);
      setStreaming(false);
      // a chart the stream never delivered says so instead of reading for ever
      setFeed((f) => {
        if (!f) return f;
        const out = { ...f };
        for (const it of (list as InsightList | null)?.items ?? []) if (!fresh.has(it.id) && !out[it.id]?.result) out[it.id] = { error: { code: "read_timeout", status: 504 } };
        return out;
      });
    };
    streamInsights((line) => {
      if ("list" in line) {
        list = line.list;
        setData(line.list);
        setFeed((f) => f ?? {});
      } else if (line.result) {
        tiles[line.id] = line.result;
        fresh.add(line.id);
        setFeed((f) => ({ ...f, [line.id]: { result: line.result } }));
      } else if (line.later) {
        fresh.add(line.id);
        setFeed((f) => ({ ...f, [line.id]: { later: true } }));
      } else if (line.error) {
        fresh.add(line.id);
        setFeed((f) => ({ ...f, [line.id]: { error: line.error } }));
      }
    }, ctl.signal).then(
      () => { if (!ctl.signal.aborted) done(true); },
      (e) => {
        if (ctl.signal.aborted) return;
        if (e instanceof ApiError && e.status === 404) {
          // a server without the one-request dashboard: the list, each chart reads itself
          setFeed(undefined);
          getInsights(ctl.signal).then(setData, (err) => !ctl.signal.aborted && setError(err));
          return;
        }
        if (!list && !shown) setError(e);
        done(false);
      },
    );
    return () => ctl.abort();
  }, [round]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Insights" />
      <PageIntro path="/insights" />
      <div className="w-full px-4 py-5 lg:px-6">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : data ? (
          <InsightsDashboard list={data} onChanged={retry} feed={feed} streaming={streaming} />
        ) : (
          <Pending page label="Reading your charts" />
        )}
      </div>
    </div>
  );
}
