"use client";

import { AlertTriangle, ArrowRight, Bookmark, Clock, History, Timer } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { HiddenNotice } from "@/components/answer-parts";
import { AskBox, type Pin } from "@/components/ask-box";
import { useAuth } from "@/components/auth";
import { Badge } from "@/components/ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/notice";
import { Skeleton } from "@/components/ui/skeleton";
import { getHome } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { ago, formatDate, formatDateTime, objectHref, plural, safeHref } from "@/lib/format";
import { stashPending } from "@/lib/pending";
import { useSaved } from "@/lib/saved";
import type { HomeResponse } from "@/lib/types";

const EXAMPLES = [
  "Which purchase orders are late, and why?",
  "whats the landed cost on the last shea butter lot",
  "who's handling the g2 beauty quote",
  "did the screen-print order ship yet",
];

function greeting(now = new Date()): string {
  const h = Number(now.toLocaleString("en-CA", { hour: "numeric", hour12: false, timeZone: "America/Toronto" }));
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function AttentionSkeleton() {
  return (
    <div className="space-y-3" role="status" aria-label="Loading what needs attention">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex items-center gap-3">
          <Skeleton className="size-9 rounded-lg" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-3.5 w-1/2" />
            <Skeleton className="h-3 w-1/3" />
          </div>
        </div>
      ))}
    </div>
  );
}

export default function HomePage() {
  const router = useRouter();
  const { state } = useAuth();
  const saved = useSaved();
  const [home, setHome] = useState<HomeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getHome(c.signal)
      .then(setHome)
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);

  function ask(q: string, pins: Pin[] = []) {
    stashPending(q, pins);
    router.push(`/ask?q=${encodeURIComponent(q)}`);
  }

  const first = home?.greeting_name || (state.status === "signed-in" ? state.me.display_name.split(/\s+/)[0] : undefined);
  const na = home?.needs_attention;
  // Longest late first; a PO with no known lateness sorts last.
  const late = [...(na?.late_pos ?? [])].sort((a, b) => (b.days_late ?? -1) - (a.days_late ?? -1));
  const atRisk = na?.at_risk ?? [];
  const examples = home?.examples?.length ? home.examples.slice(0, 4) : EXAMPLES;

  return (
    <div className="mx-auto w-full max-w-4xl px-4 pb-16 sm:px-6">
      <section className="flex flex-col items-center pt-[7vh] pb-10 text-center sm:pt-[10vh]">
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
          {greeting()}
          {first ? `, ${first}` : ""}
        </h1>
        <p className="mt-2 text-muted">Ask about any order, supplier, customer or lot. Typos are fine.</p>
        <div className="mt-8 w-full max-w-2xl">
          <AskBox onSubmit={ask} autoFocus />
        </div>
        <div className="mt-4 flex max-w-2xl flex-wrap justify-center gap-2" aria-label="Example questions">
          {examples.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => ask(q)}
              className="rounded-full border border-border bg-surface px-3 py-1.5 text-sm text-muted transition-colors hover:border-border-strong hover:text-fg cursor-pointer"
            >
              {q}
            </button>
          ))}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-5 md:grid-cols-5">
        <Card className="min-w-0 md:col-span-3">
          <CardHeader>
            <CardTitle>Needs attention today</CardTitle>
            {na?.as_of && (
              <span className="flex items-center gap-1 text-xs text-muted" title={formatDateTime(na.as_of)}>
                <Clock className="size-3" aria-hidden /> as of {ago(na.as_of)}
              </span>
            )}
          </CardHeader>
          <CardBody className="space-y-5">
            {error ? (
              <ErrorNotice message={error} onRetry={load} />
            ) : !home || !na ? (
              <AttentionSkeleton />
            ) : na.status !== "read" ? (
              <p className="text-sm text-muted" data-testid="attention-unavailable">
                {na.status === "not_configured"
                  ? "The late-order check isn't switched on for this server yet."
                  : "Business Central couldn't be read just now, so this list isn't shown. Try again in a few minutes."}
              </p>
            ) : (
              <>
                <div>
                  <h3 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
                    <Timer className="size-3.5" aria-hidden /> Late purchase orders
                  </h3>
                  {late.length === 0 ? (
                    <p className="text-sm text-muted">No late purchase orders you can see.</p>
                  ) : (
                    <ul className="-mx-2 divide-y divide-border" data-testid="late-pos">
                      {late.map((po) => (
                        <li key={po.object_id}>
                          <Link
                            href={safeHref(po.open_url) ?? objectHref("PurchaseOrder", po.object_id)}
                            className="flex items-center gap-3 rounded-lg px-2 py-2.5 transition-colors hover:bg-surface-2"
                          >
                            <span className="flex w-14 shrink-0 flex-col items-center rounded-lg bg-bad-soft py-1 text-bad">
                              <span className="text-base font-semibold leading-none tabular-nums">{po.days_late ?? "?"}</span>
                              <span className="text-[10px] font-medium uppercase">days</span>
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm font-medium">
                                {po.label}
                                {po.supplier ? ` · ${po.supplier}` : ""}
                              </span>
                              <span className="block truncate text-xs text-muted">
                                {[
                                  po.due_date ? `Due ${formatDate(po.due_date)}` : null,
                                  typeof po.open_lines === "number" ? `${plural(po.open_lines, "open line")}` : null,
                                  po.reason,
                                ]
                                  .filter(Boolean)
                                  .join(" · ")}
                              </span>
                            </span>
                            <ArrowRight className="size-4 shrink-0 text-muted" aria-hidden />
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div>
                  <h3 className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted">
                    <AlertTriangle className="size-3.5" aria-hidden /> At risk
                  </h3>
                  {atRisk.length === 0 ? (
                    <p className="text-sm text-muted">Nothing flagged at risk.</p>
                  ) : (
                    <ul className="-mx-2 space-y-0.5">
                      {atRisk.map((r) => (
                        <li key={r.object_id}>
                          <Link
                            href={safeHref(r.open_url) ?? objectHref(r.kind, r.object_id)}
                            className="flex items-start gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-surface-2"
                          >
                            <Badge tone="warn" className="mt-0.5 shrink-0">
                              Risk
                            </Badge>
                            <span className="min-w-0 flex-1 text-sm">
                              <span className="font-medium">{r.label}</span>
                              <span className="block text-xs text-muted">{r.reason}</span>
                            </span>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <HiddenNotice hidden={na.hidden} />
              </>
            )}
          </CardBody>
        </Card>

        <div className="min-w-0 space-y-5 md:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-1.5">
                <History className="size-4 text-muted" aria-hidden /> Recent questions
              </CardTitle>
            </CardHeader>
            <CardBody>
              {!home && !error ? (
                <div className="space-y-2.5" role="status" aria-label="Loading recent questions">
                  <Skeleton className="h-3.5" />
                  <Skeleton className="h-3.5 w-4/5" />
                </div>
              ) : (home?.recent_questions ?? []).length === 0 ? (
                <p className="text-sm text-muted">Your questions will show up here.</p>
              ) : (
                <ul className="-mx-2 space-y-0.5">
                  {home!.recent_questions.slice(0, 6).map((r, i) => (
                    <li key={i}>
                      <button
                        type="button"
                        onClick={() => ask(r.question)}
                        className="w-full rounded-lg px-2 py-1.5 text-left text-sm transition-colors hover:bg-surface-2 cursor-pointer"
                      >
                        <span className="line-clamp-1">{r.question}</span>
                        <span className="text-xs text-muted">{ago(r.asked_at)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-1.5">
                <Bookmark className="size-4 text-muted" aria-hidden /> Saved
              </CardTitle>
            </CardHeader>
            <CardBody>
              {saved.length === 0 ? (
                <p className="text-sm text-muted">Save an answer or a page with the bookmark icon.</p>
              ) : (
                <ul className="-mx-2 space-y-0.5">
                  {saved.slice(0, 6).map((s) => (
                    <li key={s.key}>
                      <Link href={s.href} className="block truncate rounded-lg px-2 py-1.5 text-sm hover:bg-surface-2">
                        {s.label}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>
        </div>
      </div>
    </div>
  );
}
