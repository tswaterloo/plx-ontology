"use client";

import { useCallback, useEffect, useState } from "react";
import { InsightsDashboard } from "@/components/insights/dashboard";
import { LoadProblem } from "@/components/ui/notice";
import { PageIntro } from "@/components/ui/page-intro";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { getInsights } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { InsightList } from "@/lib/types";

/** Insights: the built-in team, org and customer views and the charts you pinned, each read
 * live as you (the server decides whose rows you see). */
export default function InsightsPage() {
  const [data, setData] = useState<InsightList | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getInsights(ctl.signal).then(setData, (e) => !ctl.signal.aborted && setError(e));
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
          <InsightsDashboard list={data} onChanged={retry} />
        ) : (
          <Pending page label="Reading your charts" />
        )}
      </div>
    </div>
  );
}
