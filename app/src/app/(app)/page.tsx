"use client";

import { ArrowRight, CheckCircle2 } from "lucide-react";
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
  return now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: TZ });
}

/** "{error} didn't load. Try again." with the incident kept for the Reference line. */
function didntLoad(thing: string, err: unknown): string {
  const msg = plainError(err);
  if (/^Something went wrong on our side/.test(msg)) {
    const inc = /incident ([0-9a-f]+)/.exec(msg)?.[1];
    return `${thing} didn't load. Try again.${inc ? ` (incident ${inc})` : ""}`;
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
        setError(didntLoad("Home", e));
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
        setBriefingError(didntLoad("Your briefing", e));
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

  const loadingLine = !home && !error ? "Reading Business Central for late orders" : !briefing && !briefingError ? (slow ? "Checking mail, Teams and files for what changed" : "Ranking what needs you today") : null;

  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-8 sm:pt-8">
      <header className="border-b-2 border-rule pb-5" data-testid="home-header">
        <p className="text-eyebrow uppercase text-muted">
          {today()}
        </p>
        {head ? (
          <h1 className="mt-2 max-w-[44ch] text-display text-balance" data-testid="headline">
            {head.text}
          </h1>
        ) : (
          <h1 className="mt-2 text-display">Today</h1>
        )}
        {head?.sub && <p className="mt-2 max-w-[68ch] text-body text-muted">{head.sub}</p>}
        {loadingLine && <Pending label={loadingLine} className="mt-2 pb-0" />}
      </header>

      <section aria-label="Key numbers" className={cn(lateAll && lateAll.length > 0 && "grid grid-cols-1 border-b border-border-strong lg:grid-cols-[240px_minmax(0,1fr)] lg:gap-x-10")}>
        {error ? null : !home ? (
          <SkeletonStats />
        ) : lateRead && lateTotal > 0 ? (
          <StatRow className={cn("border-t-0", lateAll && lateAll.length > 0 && "border-b-0 lg:border-r lg:border-border lg:pr-6")} stack={!!lateAll && lateAll.length > 0}>
            <Stat label="Late POs" value={lateTotal} status="late" caption="See the list →" href="/late" testId="stat-late" />
            {oldest && typeof oldest.days_late === "number" && (
              <Stat
                label="Oldest"
                value={`${oldest.days_late} days`}
                status="late"
                caption={[recordLabel("PurchaseOrder", oldest.label), oldest.supplier].filter(Boolean).join(" · ")}
                href={safeHref(oldest.open_url) ?? objectHref("PurchaseOrder", oldest.object_id)}
                testId="stat-oldest"
              />
            )}
            {unconfirmed > 0 && (
              <Stat
                label="No confirmed date"
                value={`${unconfirmed} of ${late.length}`}
                status="risk"
                caption={late.length < lateTotal ? `among the ${late.length} oldest` : "vendor hasn't confirmed"}
                href="/late"
                testId="stat-unconfirmed"
              />
            )}
            {suspect > 0 && (
              <Stat label="Check the data" value={suspect} status="suspect" caption="totals or systems disagree" href="/briefing" testId="stat-suspect" />
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
              ? "The late-order check isn't switched on for this server yet."
              : "Business Central couldn't be read just now, so late orders aren't shown. Try again in a few minutes."}
          </p>
        )}
      </section>

      <div className="mt-10 grid grid-cols-1 gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 space-y-10">
          <Card data-testid="briefing" ref={needsYou}>
            <CardHeader>
              <CardTitle>Needs you today</CardTitle>
              {briefing?.status === "read" && briefing.count > 0 && (
                <Link href="/briefing" className="inline-flex items-center gap-1 text-meta font-medium text-accent hover:underline" data-testid="see-all">
                  See all {briefing.count} <ArrowRight className="size-3.5" aria-hidden />
                </Link>
              )}
            </CardHeader>
            <CardBody>
              {briefingError ? (
                <LoadProblem message={briefingError} busy={briefingBusy} onRetry={loadBriefing} />
              ) : !briefing ? (
                <SkeletonRows label="Ranking what needs you today" />
              ) : briefing.status !== "read" ? (
                <p className="text-row text-muted" data-testid="briefing-unavailable">
                  {briefing.status === "not_configured"
                    ? "The watch list hasn't run on this server yet."
                    : "Your briefing couldn't be read just now. Try again in a few minutes."}
                </p>
              ) : top.length === 0 && (briefing.refreshing || na?.refreshing) ? (
                <p className="text-row text-muted" data-testid="briefing-refreshing">
                  {refreshingLine(briefing.checked_on ?? na?.checked_on)}
                </p>
              ) : top.length === 0 ? (
                <EmptyState icon={CheckCircle2} title="Nothing needs you today." line="Late orders, slipped dates and vendors who haven't replied will show here." />
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
                        {briefing.test_records_hidden ?? na?.test_records_hidden} and left out of the counts.
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

        <aside className="min-w-0 space-y-10" aria-label="Ask and recent">
          <section className="border-t border-rule" aria-labelledby="ask-anything">
            <h2 id="ask-anything" className="flex min-h-11 items-center text-section">
              Ask anything
            </h2>
            <AskBox size="md" onSubmit={ask} placeholder="Ask about any order, supplier, customer or lot…" />
            <ul className="mt-3 divide-y divide-border border-t border-border" aria-label="Example questions">
              {examples.map((q) => (
                <li key={q}>
                  <button
                    type="button"
                    onClick={() => ask(q)}
                    className="group flex w-full items-baseline gap-2 py-2 text-left text-row text-muted transition-colors hover:text-fg cursor-pointer"
                  >
                    <ArrowRight className="size-3.5 shrink-0 translate-y-0.5 text-faint group-hover:text-accent" aria-hidden />
                    <span>{q}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>

          <Card>
            <CardHeader>
              <CardTitle>Recent questions</CardTitle>
              {home && recent.length > 0 && <span className="tabular-nums">{recent.length}</span>}
            </CardHeader>
            <CardBody>
              {error ? (
                <p className="text-meta text-muted">Your recent questions show here once Today loads.</p>
              ) : !home ? (
                <p className="text-meta text-faint">Your recent questions show here.</p>
              ) : recent.length === 0 ? (
                <p className="text-meta text-muted">Questions you ask show up here.</p>
              ) : (
                <ul className="divide-y divide-border">
                  {recent.slice(0, 5).map((r, i) => (
                    <li key={i}>
                      <button
                        type="button"
                        onClick={() => ask(r.question)}
                        className="flex w-full items-baseline justify-between gap-3 py-2 text-left text-row transition-colors hover:text-accent cursor-pointer"
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
                <p className="text-meta text-muted">Save an answer or a record with its bookmark to keep it here.</p>
              ) : (
                <ul className="divide-y divide-border">
                  {saved.slice(0, 5).map((s) => (
                    <li key={s.key}>
                      <Link href={s.href} className="block truncate py-2 text-row hover:text-accent">
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
