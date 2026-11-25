"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { InsightsDashboard, type TileEntry } from "@/components/insights/dashboard";
import { ChartSkeleton } from "@/components/charts/chart-skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { cn } from "@/lib/cn";
import { ApiError } from "@/lib/errors";
import { getInsights, keepInsights, keptInsights, saveInsightLayout, streamInsights } from "@/lib/api";
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
  const [editing, setEditing] = useState(false);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  // what the stream has read so far, so an arrangement is kept with the tiles already drawn
  const latest = useRef<{ list: InsightList | null; tiles: Record<string, InsightResult> }>({ list: null, tiles: {} });
  const [problem, setProblem] = useState<string | null>(null);
  /** Keep the person's own order and the charts they took off: shown at once, then as the server kept it. */
  const arrange = useCallback(async (order: string[], hidden: string[]) => {
    setProblem(null);
    const before = latest.current.list;
    try {
      const got = await saveInsightLayout(order, hidden);
      latest.current.list = got;
      setData(got);
      // a chart added back has nothing in this page's feed: it reads its own stored value
      setFeed((f) => (f ? { ...f, ...Object.fromEntries(got.items.filter((i) => !f[i.id]).map((i) => [i.id, { later: true } as TileEntry])) } : f));
      keepInsights(got, latest.current.tiles);
    } catch (e) {
      latest.current.list = before;
      setProblem(plainError(e));
    }
  }, []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setStreaming(true);
    let list: InsightList | null = null;
    const tiles: Record<string, InsightResult> = {};
    latest.current.tiles = tiles;
    const fresh = new Set<string>();
    const shown = keptInsights() !== undefined;
    const done = (ok: boolean) => {
      const now = latest.current.list ?? list;
      if (ok && now) keepInsights(now, tiles);
      setStreaming(false);
      // a chart the stream never delivered says so instead of reading for ever
      setFeed((f) => {
        if (!f) return f;
        const out = { ...f };
        for (const it of (latest.current.list ?? (list as InsightList | null))?.items ?? []) if (!fresh.has(it.id) && !out[it.id]?.result) out[it.id] = { error: { code: "read_timeout", status: 504 } };
        return out;
      });
    };
    streamInsights((line) => {
      if ("list" in line) {
        list = line.list;
        latest.current.list = line.list;
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
      <Toolbar
        heading
        title="Insights"
        actions={
          data?.saving ? (
            <button
              type="button"
              aria-pressed={editing}
              onClick={() => setEditing((e) => !e)}
              className={cn(
                "inline-flex h-7 items-center rounded-md px-2.5 text-[12.5px] font-medium transition-colors cursor-pointer",
                editing
                  ? "bg-accent text-accent-fg hover:brightness-110"
                  : "border border-border bg-bg text-fg hover:border-border-strong hover:bg-hover dark:bg-surface",
              )}
            >
              {editing ? "Done" : "Customize"}
            </button>
          ) : undefined
        }
      />
      <PageIntro path="/insights" />
      <div className="w-full px-4 py-4 lg:px-6">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : data ? (
          <>
            {problem && <p className="pb-3 text-[13px]" role="alert">{problem}</p>}
            <InsightsDashboard list={data} onChanged={retry} feed={feed} streaming={streaming} onArrange={arrange} editing={editing} onEditingChange={setEditing} />
          </>
        ) : (
          <div aria-busy data-testid="insights-skeleton">
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
              {[0, 1, 2, 3, 4, 5].map((i) => <div key={i} className="rounded-md border border-border bg-bg p-4"><div className="skeleton skeleton-shimmer mb-4 h-5 w-2/3" /><ChartSkeleton /></div>)}
            </div>
            <Pending label="Reading your charts" />
          </div>
        )}
      </div>
    </div>
  );
}
