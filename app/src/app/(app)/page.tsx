"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { useListKeys } from "@/lib/list-keys";
import { FreshnessLabel, HiddenNotice } from "@/components/answer-parts";
import { AskBox, type Pin } from "@/components/ask-box";
import { BriefingCounts, BriefingRow } from "@/components/briefing";
import { LateShape } from "@/components/late-shape";
import { ProactiveSuggestions } from "@/components/proactive-suggestions";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending, SkeletonRows, SkeletonStats } from "@/components/ui/skeleton";
import { Stat, StatRow } from "@/components/ui/stat";
import { cn } from "@/lib/cn";
import { getBriefing, getHome, getLateList, markBriefingSeen } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { VERIFIED_EXAMPLES } from "@/lib/examples";
import { ago, formatDateTime, objectHref, recordLabel, safeHref } from "@/lib/format";
import { headline, refreshingLine } from "@/lib/home";
import { stashPending } from "@/lib/pending";
import { useSaved } from "@/lib/saved";
import type { Briefing, HomeResponse, LatePo } from "@/lib/types";

const TZ = "America/Toronto";

function today(now = new Date()): string {
  return now.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: TZ });
}

/** "{error} didn't load. Try again." with the incident kept for the Reference line. */
function didntLoad(thing: string, err: unknown): string {
  const msg = plainError(err);
  if (/^Server error/.test(msg)) {
    const inc = /incident ([0-9a-f]+)/.exec(msg)?.[1];
    return `Couldn't load ${thing}. Try again.${inc ? ` (incident ${inc})` : ""}`;
  }
  return msg;
}

/** Late POs with no supplier confirmation, among those in hand. */
const UNCONFIRMED = /no supplier confirmation|no confirmed date|not confirmed/i;

export default function HomePage() {
  const router = useRouter();
  const saved = useSaved();
  const [home, setHome] = useState<HomeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getHome(c.signal)
      .then(setHome)
      .catch((e) => {
        if (c.signal.aborted) return;
        setBusy(noticeKind(e) === "busy");
        setError(didntLoad("Today", e));
      });
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);

  // The briefing loads apart (its top items read how-we-know trails); once shown, it is
  // marked seen so the next "since your last visit" starts now.
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [briefingError, setBriefingError] = useState<string | null>(null);
  const [briefingBusy, setBriefingBusy] = useState(false);
  const [slow, setSlow] = useState(false);
  const loadBriefing = useCallback(() => {
    const c = new AbortController();
    setBriefingError(null);
    setSlow(false);
    const t = setTimeout(() => setSlow(true), 5000);
    getBriefing(c.signal)
      .then((b) => {
        setBriefing(b);
        if (b.status === "read") markBriefingSeen().catch(() => undefined);
      })
      .catch((e) => {
        if (c.signal.aborted) return;
        setBriefingBusy(noticeKind(e) === "busy");
        setBriefingError(didntLoad("the briefing", e));
      })
      .finally(() => clearTimeout(t));
    return () => {
      clearTimeout(t);
      c.abort();
    };
  }, []);
  useEffect(() => loadBriefing(), [loadBriefing]);

  const needsYou = useRef<HTMLDivElement>(null);
  useListKeys(needsYou);
  // every late order (a page or three), for the drawing; the figures above use Today's own count
  const [lateAll, setLateAll] = useState<LatePo[] | null>(null);
  useEffect(() => {
    const c = new AbortController();
    (async () => {
      const all: LatePo[] = [];
      let offset = 0;
      for (let i = 0; i < 3; i++) {
        const page = await getLateList(offset, c.signal, 100);
        all.push(...(page.items ?? []));
        if (!page.more || page.next_offset == null) break;
        offset = page.next_offset;
      }
      setLateAll(all);
    })().catch(() => !c.signal.aborted && setLateAll([]));
    return () => c.abort();
  }, []);

  function ask(q: string, pins: Pin[] = []) {
    stashPending(q, pins);
    router.push(`/ask?q=${encodeURIComponent(q)}`);
  }

  const na = home?.needs_attention;
  const lateRead = na?.status === "read";
  const late: LatePo[] = [...(na?.late_pos ?? [])].sort((a, b) => (b.days_late ?? -1) - (a.days_late ?? -1));
  const lateTotal = na?.late_total ?? late.length;
  const oldest = late[0];
  const unconfirmed = late.filter((p) => UNCONFIRMED.test(p.reason)).length;
  const items = briefing?.status === "read" ? (briefing.items ?? []) : [];
  const suspect = items.filter((i) => i.amount && i.amount_suspect).length;
  const examples = home?.examples?.length ? home.examples.slice(0, 4) : VERIFIED_EXAMPLES;
  const head = headline(home, briefing);
  const top = items.slice(0, 5);
  const byId = new Map(late.map((p) => [p.object_id, p]));
  // a question asked again is listed once (its latest time)
  const recent = (home?.recent_questions ?? []).filter((r, i, all) => all.findIndex((x) => x.question.trim().toLowerCase() === r.question.trim().toLowerCase()) === i);

  const loadingLine = !home && !error ? "Reading late POs from Business Central" : !briefing && !briefingError ? (slow ? "Reading mail, Teams and files" : "Loading the briefing") : null;

  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-8 sm:pt-8">
      <header className="border-b border-border pb-3" data-testid="home-header">
        <h1 className="text-display">
          Today <span className="font-normal text-muted">{today()}</span>
        </h1>
        {head && (
          <p className="mt-0.5 text-row text-fg" data-testid="headline">
            {head.text}
            {head.sub && <span className="text-muted"> {head.sub}</span>}
          </p>
        )}
        {loadingLine && <Pending label={loadingLine} className="pb-0" />}
      </header>

      <section aria-label="Late POs in figures" className={cn(lateAll && lateAll.length > 0 && "grid grid-cols-1 border-b border-border lg:grid-cols-[200px_minmax(0,1fr)] lg:gap-x-10")}>
        {error ? null : !home ? (
          <SkeletonStats />
        ) : lateRead && lateTotal > 0 ? (
          <StatRow className={cn(lateAll && lateAll.length > 0 && "lg:flex-col lg:gap-y-4 lg:border-r lg:border-border lg:pr-6")}>
            <Stat label="Late POs" value={lateTotal} status="late" href="/late" testId="stat-late" />
            {oldest && typeof oldest.days_late === "number" && (
              <Stat
                label="Oldest"
                value={`${oldest.days_late} days`}
                status="late"
                caption={[recordLabel("PurchaseOrder", oldest.label), oldest.supplier].filter(Boolean).join(", ")}
                href={safeHref(oldest.open_url) ?? objectHref("PurchaseOrder", oldest.object_id)}
                testId="stat-oldest"
              />
            )}
            {unconfirmed > 0 && (
              <Stat
                label="No supplier date"
                value={`${unconfirmed} of ${late.length}`}
                status="risk"
                caption={late.length < lateTotal ? `of the ${late.length} oldest` : undefined}
                href="/late"
                testId="stat-unconfirmed"
              />
            )}
            {suspect > 0 && (
              <Stat label="Totals to check" value={suspect} status="suspect" caption="systems disagree" href="/briefing" testId="stat-suspect" />
            )}
          </StatRow>
        ) : null}
        {lateAll && lateAll.length > 0 && (
          <div className="min-w-0 pb-2 max-lg:border-t max-lg:border-border">
            <LateShape items={lateAll} embedded />
          </div>
        )}
        {error && <LoadProblem message={error} busy={busy} onRetry={load} className="mt-4" />}
        {na && !lateRead && (
          <p className="mt-4 text-row text-muted" data-testid="attention-unavailable">
            {na.status === "not_configured"
              ? "The late-PO check is off on this server."
              : "Couldn't read Business Central, so late POs aren't shown. Try again in a few minutes."}
          </p>
        )}
      </section>

      <div className="mt-6 grid grid-cols-1 gap-x-10 gap-y-8 lg:grid-cols-[minmax(0,1fr)_280px]">
        <div className="min-w-0 space-y-8">
          <Card data-testid="briefing" ref={needsYou}>
            <CardHeader>
              <CardTitle>Needs action</CardTitle>
              {briefing?.status === "read" && briefing.count > 0 && (
                <Link href="/briefing" className="text-meta text-accent hover:underline" data-testid="see-all">
                  All {briefing.count} in the briefing
                </Link>
              )}
            </CardHeader>
            <CardBody>
              {briefingError ? (
                <LoadProblem message={briefingError} busy={briefingBusy} onRetry={loadBriefing} />
              ) : !briefing ? (
                <SkeletonRows label="Loading the briefing" />
              ) : briefing.status !== "read" ? (
                <p className="text-row text-muted" data-testid="briefing-unavailable">
                  {briefing.status === "not_configured"
                    ? "The briefing hasn't run on this server."
                    : "Couldn't load the briefing. Try again in a few minutes."}
                </p>
              ) : top.length === 0 && (briefing.refreshing || na?.refreshing) ? (
                <p className="text-row text-muted" data-testid="briefing-refreshing">
                  {refreshingLine(briefing.checked_on ?? na?.checked_on)}
                </p>
              ) : top.length === 0 ? (
                <EmptyState title="Nothing needs action." line="Late POs, moved dates and suppliers who haven't replied are listed here." />
              ) : (
                <>
                  <ol className="divide-y divide-border border-t border-border" data-testid="needs-you">
                    {top.map((i, n) => (
                      <BriefingRow key={i.id} item={i} latePo={byId.get(i.object_id)} position={n + 1} dense />
                    ))}
                  </ol>
                  <div className="mt-3 space-y-1">
                    {(briefing.refreshing || na?.refreshing) && (
                      <p className="text-meta text-muted" data-testid="briefing-refreshing">
                        {refreshingLine(briefing.checked_on ?? na?.checked_on)}
                      </p>
                    )}
                    <BriefingCounts briefing={briefing} compact />
                    {(briefing.test_records_hidden || na?.test_records_hidden) && (
                      <p className="text-meta text-muted" data-testid="briefing-tests-hidden">
                        {briefing.test_records_hidden ?? na?.test_records_hidden}, not counted.
                      </p>
                    )}
                  </div>
                </>
              )}
              {lateRead && <HiddenNotice hidden={na?.hidden} />}
            </CardBody>
          </Card>

          <ProactiveSuggestions className="mt-0" />
        </div>

        <aside className="min-w-0 space-y-8" aria-label="Ask and recent">
          <section className="border-t border-rule" aria-labelledby="ask-anything">
            <h2 id="ask-anything" className="flex min-h-10 items-center text-section">
              Ask
            </h2>
            <AskBox size="md" onSubmit={ask} placeholder="Question about a PO, SO, supplier or lot" />
            <p className="mt-3 text-meta text-muted">For example:</p>
            <ul className="mt-0.5" aria-label="Example questions">
              {examples.map((q) => (
                <li key={q}>
                  <button
                    type="button"
                    onClick={() => ask(q)}
                    className="py-0.5 text-left text-row text-accent hover:underline cursor-pointer"
                  >
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          </section>

          <Card>
            <CardHeader>
              <CardTitle>Your recent questions</CardTitle>
              {home && recent.length > 0 && <span className="tabular-nums">{recent.length}</span>}
            </CardHeader>
            <CardBody>
              {error ? (
                <p className="text-meta text-muted">Not loaded.</p>
              ) : !home ? (
                <p className="text-meta text-faint">Loading…</p>
              ) : recent.length === 0 ? (
                <p className="text-meta text-muted">None yet.</p>
              ) : (
                <ul className="divide-y divide-border">
                  {recent.slice(0, 5).map((r, i) => (
                    <li key={i}>
                      <button
                        type="button"
                        onClick={() => ask(r.question)}
                        className="flex w-full items-baseline justify-between gap-3 py-1.5 text-left text-row hover:text-accent hover:underline cursor-pointer"
                      >
                        <span className="line-clamp-1">{r.question}</span>
                        <span className="shrink-0 text-meta tabular-nums text-faint">{ago(r.asked_at)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Saved</CardTitle>
              {saved.length > 0 && <span className="tabular-nums">{saved.length}</span>}
            </CardHeader>
            <CardBody>
              {saved.length === 0 ? (
                <p className="text-meta text-muted">Nothing saved. Use Save on an answer or a record.</p>
              ) : (
                <ul className="divide-y divide-border">
                  {saved.slice(0, 5).map((s) => (
                    <li key={s.key}>
                      <Link href={s.href} className="block truncate py-1.5 text-row text-accent hover:underline">
                        {s.label}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </CardBody>
          </Card>

          {home?.freshness?.as_of ? (
            <div data-testid="home-freshness">
              <FreshnessLabel freshness={home.freshness} />
            </div>
          ) : na?.as_of ? (
            <p className="text-meta text-muted" data-testid="home-freshness">
              Data as of {formatDateTime(na.as_of)}
            </p>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
