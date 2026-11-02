"use client";

import { ArrowRight, CheckCircle2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { HiddenNotice } from "@/components/answer-parts";
import { AskBox, type Pin } from "@/components/ask-box";
import { useAuth } from "@/components/auth";
import { BriefingCounts, BriefingRow } from "@/components/briefing";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorNotice } from "@/components/ui/notice";
import { Skeleton, SkeletonRows, SkeletonStats } from "@/components/ui/skeleton";
import { Stat, StatRow } from "@/components/ui/stat";
import { getBriefing, getHome, markBriefingSeen } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { VERIFIED_EXAMPLES } from "@/lib/examples";
import { ago, formatDateTime, objectHref, recordLabel, safeHref } from "@/lib/format";
import { headline } from "@/lib/home";
import { stashPending } from "@/lib/pending";
import { useSaved } from "@/lib/saved";
import type { Briefing, HomeResponse, LatePo } from "@/lib/types";

const TZ = "America/Toronto";

function greeting(now = new Date()): string {
  const h = Number(now.toLocaleString("en-CA", { hour: "numeric", hour12: false, timeZone: TZ }));
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

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
  const { state } = useAuth();
  const saved = useSaved();
  const [home, setHome] = useState<HomeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    getHome(c.signal)
      .then(setHome)
      .catch((e) => !c.signal.aborted && setError(didntLoad("Home", e)));
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);

  // The briefing loads apart (its top items read how-we-know trails); once shown, it is
  // marked seen so the next "since your last visit" starts now.
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [briefingError, setBriefingError] = useState<string | null>(null);
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
      .catch((e) => !c.signal.aborted && setBriefingError(didntLoad("Your briefing", e)))
      .finally(() => clearTimeout(t));
    return () => {
      clearTimeout(t);
      c.abort();
    };
  }, []);
  useEffect(() => loadBriefing(), [loadBriefing]);

  function ask(q: string, pins: Pin[] = []) {
    stashPending(q, pins);
    router.push(`/ask?q=${encodeURIComponent(q)}`);
  }

  const first = home?.greeting_name || (state.status === "signed-in" ? state.me.display_name.split(/\s+/)[0] : undefined);
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
  const recent = home?.recent_questions ?? [];

  return (
    <div className="mx-auto w-full max-w-[1200px] px-4 pt-6 pb-16 sm:px-6 sm:pt-8 xl:px-8">
      <header className="space-y-1" data-testid="home-header">
        <p className="text-meta text-muted">
          {today()} · {greeting()}
          {first ? `, ${first}` : ""}
        </p>
        {head ? (
          <h1 className="text-display" data-testid="headline">
            {head.text}
          </h1>
        ) : error || (na && !lateRead) ? (
          <h1 className="text-display">Today</h1>
        ) : (
          <>
            <h1 className="sr-only">Today</h1>
            <Skeleton className="h-[30px] w-3/5 sm:h-[34px]" />
          </>
        )}
        {head?.sub && <p className="text-answer text-muted">{head.sub}</p>}
        {slow && !briefing && !briefingError && <p className="text-meta text-muted">Checking the sources for what changed…</p>}
      </header>

      <section className="mt-6" aria-label="Key numbers">
        {error ? null : !home ? (
          <SkeletonStats />
        ) : lateRead && lateTotal > 0 ? (
          <StatRow>
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
                caption={late.length < lateTotal ? `of the ${late.length} oldest; vendor hasn't confirmed` : "vendor hasn't confirmed"}
                href="/late"
                testId="stat-unconfirmed"
              />
            )}
            {suspect > 0 && (
              <Stat label="Check the data" value={suspect} status="suspect" caption="totals or systems disagree" href="/briefing" testId="stat-suspect" />
            )}
          </StatRow>
        ) : null}
        {error && <ErrorNotice message={error} onRetry={load} />}
        {na && !lateRead && (
          <p className="text-row text-muted" data-testid="attention-unavailable">
            {na.status === "not_configured"
              ? "The late-order check isn't switched on for this server yet."
              : "Business Central couldn't be read just now, so late orders aren't shown. Try again in a few minutes."}
          </p>
        )}
      </section>

      <Card className="mt-6" data-testid="briefing">
        <CardHeader>
          <CardTitle>Needs you today</CardTitle>
          {briefing?.status === "read" && briefing.count > 0 && (
            <Link href="/briefing" className="inline-flex items-center gap-1 text-row font-medium text-accent hover:underline" data-testid="see-all">
              See all {briefing.count} <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          )}
        </CardHeader>
        <CardBody>
          {briefingError ? (
            <ErrorNotice message={briefingError} onRetry={loadBriefing} />
          ) : !briefing ? (
            <SkeletonRows rows={5} label="Loading what needs you today" />
          ) : briefing.status !== "read" ? (
            <p className="text-row text-muted" data-testid="briefing-unavailable">
              {briefing.status === "not_configured"
                ? "The watch list hasn't run on this server yet."
                : "Your briefing couldn't be read just now. Try again in a few minutes."}
            </p>
          ) : top.length === 0 ? (
            <EmptyState icon={CheckCircle2} title="Nothing needs you today." line="Late orders, slipped dates and vendors who haven't replied will show here." />
          ) : (
            <>
              <ol className="divide-y divide-border" data-testid="needs-you">
                {top.map((i) => (
                  <BriefingRow key={i.id} item={i} latePo={byId.get(i.object_id)} dense />
                ))}
              </ol>
              <div className="mt-2 space-y-1">
                <BriefingCounts briefing={briefing} compact />
                {(briefing.test_records_hidden || na?.test_records_hidden) && (
                  <p className="text-meta text-muted" data-testid="briefing-tests-hidden">
                    {briefing.test_records_hidden ?? na?.test_records_hidden} (test and placeholder records are left out)
                  </p>
                )}
              </div>
            </>
          )}
          {lateRead && <HiddenNotice hidden={na?.hidden} />}
        </CardBody>
      </Card>

      <section className="mt-8 max-w-[760px]" aria-labelledby="ask-anything">
        <h2 id="ask-anything" className="mb-2 text-section">
          Ask anything
        </h2>
        <AskBox size="md" onSubmit={ask} placeholder="Ask about any order, supplier, customer or lot…" />
        <div className="mt-3 flex flex-wrap gap-2" aria-label="Example questions">
          {examples.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => ask(q)}
              className="min-h-9 rounded-full border border-border bg-surface px-3 py-1.5 text-row text-muted transition-colors hover:border-control hover:text-fg cursor-pointer"
            >
              {q}
            </button>
          ))}
        </div>
      </section>

      <div className="mt-8 grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Recent questions{home && recent.length > 0 ? ` (${recent.length})` : ""}</CardTitle>
          </CardHeader>
          <CardBody>
            {error ? (
              <p className="text-row text-muted">—</p>
            ) : !home ? (
              <div className="space-y-2.5" role="status" aria-label="Loading recent questions">
                <Skeleton className="h-3.5" />
                <Skeleton className="h-3.5 w-4/5" />
              </div>
            ) : recent.length === 0 ? (
              <p className="text-row text-muted">Your questions will show up here.</p>
            ) : (
              <ul className="-mx-2">
                {recent.slice(0, 5).map((r, i) => (
                  <li key={i}>
                    <button
                      type="button"
                      onClick={() => ask(r.question)}
                      className="flex w-full items-baseline justify-between gap-3 rounded-lg px-2 py-1.5 text-left text-row transition-colors hover:bg-surface-2 cursor-pointer"
                    >
                      <span className="line-clamp-1">{r.question}</span>
                      <span className="shrink-0 text-meta text-muted">{ago(r.asked_at)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </CardBody>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Saved{saved.length > 0 ? ` (${saved.length})` : ""}</CardTitle>
          </CardHeader>
          <CardBody>
            {saved.length === 0 ? (
              <p className="text-row text-muted">Save an answer or a page with the bookmark button.</p>
            ) : (
              <ul className="-mx-2">
                {saved.slice(0, 5).map((s) => (
                  <li key={s.key}>
                    <Link href={s.href} className="block truncate rounded-lg px-2 py-1.5 text-row hover:bg-surface-2">
                      {s.label}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardBody>
        </Card>
      </div>

      {(home?.freshness?.as_of || na?.as_of) && (
        <p className="mt-8 text-meta text-muted" data-testid="home-freshness">
          Data as of {formatDateTime(home?.freshness?.as_of ?? na?.as_of)}
        </p>
      )}
    </div>
  );
}
