"use client";

// What the reader's scheduled insights found, on Home: one line per run that came to them
// (counts only; the insight reads its rows again, live, when they open it), each put away
// on its own. Nothing shows while the owner views as someone, or when there is none.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useViewingAs } from "@/components/view-as";
import { getInsightNews, putAwayInsightNews } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { InsightNews } from "@/lib/types";

/** Home's "Your reports" list; nothing at all when none came to you. */
export function InsightNewsList({ most = 5 }: { most?: number }) {
  const viewing = useViewingAs();
  const [items, setItems] = useState<InsightNews[] | null>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    getInsightNews(c.signal).then((n) => setItems(n.items), () => !c.signal.aborted && setItems([]));   // Home never waits on it
    return () => c.abort();
  }, []);
  useEffect(() => (viewing ? undefined : load()), [load, viewing]);
  if (viewing || !items || items.length === 0) return null;
  const away = async (runId: string) => {
    try {
      await putAwayInsightNews(runId);
      setItems((now) => (now ?? []).filter((n) => n.run_id !== runId));
    } catch {
      /* stays listed: put away again later */
    }
  };
  return (
    <section className="border-b border-border px-5 py-3" aria-labelledby="insight-news" data-testid="insight-news">
      <h2 id="insight-news" className="text-[13px] font-semibold">
        Your reports
        <span className="ml-1.5 font-normal text-muted">{items.length}</span>
      </h2>
      <ul className="mt-1">
        {items.slice(0, most).map((n) => (
          <li key={n.run_id} className="flex items-baseline gap-3 py-1" data-testid="insight-news-row">
            <p className="min-w-0 flex-1 text-[13px] leading-5">
              <Link href={`/insights#insight-${n.insight_id}`} className="font-medium hover:underline">{n.title}</Link>
              <span className="ml-2 text-muted">{n.said}</span>
              <span className="ml-2 text-[12px] text-muted">{formatDateTime(n.ran_at)}</span>
            </p>
            {n.ask && (
              <Link href={`/ask?q=${encodeURIComponent(n.ask)}`} className="h-7 shrink-0 rounded-md px-2 text-[12px] font-medium leading-7 hover:bg-hover" data-testid="insight-news-ask">
                {/^why\b/i.test(n.ask) ? "Ask why" : "Ask about it"}
              </Link>
            )}
            <button type="button" className="h-7 shrink-0 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer" onClick={() => away(n.run_id)}>
              Put away
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
