"use client";

import { Bookmark, BookmarkCheck, CheckSquare, Database, FileText, Mail, MessageSquare, MessagesSquare, Users } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { getObject } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { formatDate, formatDateTime, money, objectHref, plural, safeHref, SYSTEM_LABEL, typeLabel } from "@/lib/format";
import { toggleSaved, useSaved } from "@/lib/saved";
import type { LandedCost, LateLine, LinkedItem, ObjectPage, Person, Source, TimelineEvent, Tone } from "@/lib/types";
import { ConflictCard, FactList, FreshnessLabel, HiddenNotice, NotFoundSection } from "./answer-parts";
import { SourceChip } from "./source-chip";
import { Badge } from "./ui/badge";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";
import { ErrorNotice } from "./ui/notice";
import { Skeleton } from "./ui/skeleton";

/** Status words the API uses, mapped to a colour; anything unknown stays neutral. */
export function toneOf(state: string): Tone {
  const s = state.toLowerCase();
  if (/late|overdue|blocked|failed|rejected|on_hold|hold/.test(s)) return "bad";
  if (/risk|unconfirmed|partial|pending|waiting/.test(s)) return "warn";
  if (/received|complete|closed|shipped|done|ok|on_time|confirmed/.test(s)) return "good";
  if (/open|released|in_progress/.test(s)) return "info";
  return "neutral";
}

function Chips({ sources, max = 3 }: { sources?: Source[]; max?: number }) {
  if (!sources || sources.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap gap-1 align-middle">
      {sources.slice(0, max).map((s, i) => (
        <SourceChip key={i} source={s} className="max-w-[12rem]" />
      ))}
    </span>
  );
}

function ObjectSkeleton() {
  return (
    <div className="space-y-6" role="status" aria-label="Loading">
      <div className="space-y-3">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
        <Skeleton className="h-48 rounded-2xl lg:col-span-2" />
        <Skeleton className="h-48 rounded-2xl" />
      </div>
      <Skeleton className="h-64 rounded-2xl" />
    </div>
  );
}

function LateLines({ lines }: { lines: LateLine[] }) {
  if (lines.length === 0) return null;
  const sorted = [...lines].sort((a, b) => (b.days_late ?? -1) - (a.days_late ?? -1));
  return (
    <Card data-testid="late-lines">
      <CardHeader>
        <CardTitle>Late lines</CardTitle>
        <Badge tone="bad">{plural(lines.length, "line")} late</Badge>
      </CardHeader>
      <CardBody className="px-0">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Late lines, longest late first</caption>
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted">
                <th scope="col" className="px-5 py-2 font-medium">Line</th>
                <th scope="col" className="py-2 font-medium">Item</th>
                <th scope="col" className="py-2 text-right font-medium">Qty</th>
                <th scope="col" className="py-2 pl-4 font-medium">Due</th>
                <th scope="col" className="px-5 py-2 text-right font-medium">Late</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((l, i) => (
                <tr key={i} className="border-b border-border align-top last:border-0">
                  <td className="px-5 py-2.5 tabular-nums text-muted">{l.line}</td>
                  <td className="py-2.5">
                    <span className="font-medium">{l.item ?? "—"}</span>
                    {l.sources && l.sources.length > 0 && (
                      <span className="mt-1 block">
                        <Chips sources={l.sources} max={1} />
                      </span>
                    )}
                  </td>
                  <td className="whitespace-nowrap py-2.5 text-right tabular-nums">{l.quantity ?? "—"}</td>
                  <td className="whitespace-nowrap py-2.5 pl-4">{l.due_date ? formatDate(l.due_date) : "—"}</td>
                  <td className="whitespace-nowrap px-5 py-2.5 text-right font-medium tabular-nums text-bad">
                    {typeof l.days_late === "number" ? `${l.days_late} d` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardBody>
    </Card>
  );
}

const LC_STATUS: Record<string, { label: string; tone: Tone }> = {
  computed: { label: "Complete", tone: "good" },
  partial: { label: "Partial", tone: "warn" },
  not_established: { label: "Not established", tone: "neutral" },
};

function LandedCostCard({ lc }: { lc: LandedCost }) {
  const st = LC_STATUS[lc.status] ?? { label: lc.status, tone: "neutral" as Tone };
  const lines = lc.lines ?? [];
  return (
    <Card data-testid="landed-cost">
      <CardHeader>
        <CardTitle>Landed cost</CardTitle>
        <Badge tone={st.tone}>{st.label}</Badge>
      </CardHeader>
      <CardBody>
        <dl className="space-y-2 text-sm">
          {lines.map((l, i) => (
            <div key={i} className="flex items-start justify-between gap-3">
              <dt className="min-w-0 text-muted">
                <span className="block">{l.label}</span>
                <Chips sources={l.sources} max={1} />
              </dt>
              <dd className="shrink-0 tabular-nums">{money(l.amount)}</dd>
            </div>
          ))}
          {lc.total ? (
            <div className="flex items-center justify-between gap-3 border-t border-border pt-2 font-semibold">
              <dt>Total</dt>
              <dd className="tabular-nums">{money(lc.total)}</dd>
            </div>
          ) : (
            <div className="border-t border-border pt-2 text-muted">No total until every cost is on record.</div>
          )}
          {lc.per_unit && (
            <div className="flex items-center justify-between gap-3 text-muted">
              <dt>Per unit</dt>
              <dd className="tabular-nums">{money(lc.per_unit)}</dd>
            </div>
          )}
        </dl>
        {(lc.missing ?? []).length > 0 && (
          <div className="mt-3 rounded-lg bg-bg-subtle p-3 text-xs">
            <p className="mb-1 font-medium">Missing</p>
            <ul className="list-disc space-y-0.5 pl-4 text-muted">
              {lc.missing!.map((m, i) => (
                <li key={i}>{m}</li>
              ))}
            </ul>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function Timeline({ events }: { events: TimelineEvent[] }) {
  if (events.length === 0) return null;
  const sorted = [...events].sort((a, b) => ((a.at ?? "") < (b.at ?? "") ? 1 : (a.at ?? "") > (b.at ?? "") ? -1 : 0));
  return (
    <Card data-testid="timeline">
      <CardHeader>
        <CardTitle>Timeline across systems</CardTitle>
      </CardHeader>
      <CardBody>
        <ol className="relative ml-1.5 space-y-5 border-l border-border pl-5">
          {sorted.map((e, i) => (
            <li key={i} className="relative">
              <span className="absolute -left-[25.5px] top-1.5 size-2.5 rounded-full border-2 border-surface bg-accent" aria-hidden />
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
                {e.at && <time dateTime={e.at}>{formatDateTime(e.at)}</time>}
                {e.at && <span aria-hidden>·</span>}
                <span>{SYSTEM_LABEL[e.system] ?? e.system}</span>
              </div>
              <p className="mt-0.5 text-sm font-medium">{e.title}</p>
              {e.summary && <p className="text-sm text-muted">{e.summary}</p>}
              {e.source && (
                <div className="mt-1">
                  <SourceChip source={e.source} />
                </div>
              )}
            </li>
          ))}
        </ol>
      </CardBody>
    </Card>
  );
}

const LINK_ICON = { email: Mail, teams: MessagesSquare, document: FileText, record: Database, task: CheckSquare } as const;

function Linked({ items }: { items: LinkedItem[] }) {
  if (items.length === 0) return null;
  return (
    <Card data-testid="linked">
      <CardHeader>
        <CardTitle>Emails, threads and documents</CardTitle>
        <span className="text-xs text-muted">{items.length}</span>
      </CardHeader>
      <CardBody>
        <ul className="-mx-2 space-y-0.5">
          {items.map((l, i) => {
            const Icon = LINK_ICON[l.kind] ?? MessageSquare;
            const href = safeHref(l.source.open_url);
            const title = <span className="block truncate text-sm font-medium">{l.label}</span>;
            return (
              <li key={i} className="flex items-start gap-3 rounded-lg px-2 py-2">
                <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted">
                  <Icon className="size-3.5" aria-hidden />
                </span>
                <span className="min-w-0 flex-1">
                  {href ? (
                    <Link href={href} className="hover:underline">
                      {title}
                    </Link>
                  ) : (
                    title
                  )}
                  <span className="block truncate text-xs text-muted">
                    {[l.source.label, l.at ? formatDate(l.at) : null].filter(Boolean).join(" · ")}
                  </span>
                  {l.source.quote && <span className="mt-1 line-clamp-2 block text-xs text-muted">“{l.source.quote}”</span>}
                </span>
              </li>
            );
          })}
        </ul>
      </CardBody>
    </Card>
  );
}

function People({ people }: { people: Person[] }) {
  if (people.length === 0) return null;
  return (
    <Card data-testid="people">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          <Users className="size-4 text-muted" aria-hidden /> People
        </CardTitle>
      </CardHeader>
      <CardBody>
        <ul className="space-y-3">
          {people.map((p, i) => (
            <li key={i} className="flex items-center gap-3">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-accent-soft text-xs font-semibold text-accent" aria-hidden>
                {p.name
                  .split(/\s+/)
                  .map((x) => x[0])
                  .slice(0, 2)
                  .join("")
                  .toUpperCase()}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-sm font-medium">{p.name}</span>
                <span className="block truncate text-xs text-muted">{[p.role, p.organisation].filter(Boolean).join(" · ")}</span>
              </span>
            </li>
          ))}
        </ul>
      </CardBody>
    </Card>
  );
}

export function ObjectView({ type, id }: { type: string; id: string }) {
  const [obj, setObj] = useState<ObjectPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const saved = useSaved();
  const load = useCallback(() => {
    const c = new AbortController();
    setError(null);
    setObj(null);
    getObject(type, id, c.signal)
      .then(setObj)
      .catch((e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, [type, id]);
  useEffect(() => load(), [load]);

  const kind = typeLabel(obj?.type ?? type);
  const key = `o:${type}/${id}`;
  const isSaved = saved.some((s) => s.key === key);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6">
      {error ? (
        <ErrorNotice message={error} onRetry={load} />
      ) : !obj ? (
        <ObjectSkeleton />
      ) : (
        <div className="space-y-6 animate-fade-in">
          <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div className="min-w-0 space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-muted">{kind}</p>
              <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl" data-testid="object-title">
                {obj.label}
              </h1>
              {obj.summary && <p className="text-sm text-muted">{obj.summary}</p>}
              {obj.status && (
                <div className="flex flex-wrap items-center gap-2 pt-1" data-testid="object-status">
                  <Badge tone={toneOf(obj.status.state)}>{obj.status.text}</Badge>
                  <Chips sources={obj.status.sources} max={2} />
                </div>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <button
                type="button"
                aria-pressed={isSaved}
                onClick={() => toggleSaved({ key, kind: "object", label: obj.label, href: objectHref(type, id) })}
                className={cn(
                  "inline-flex h-9 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-surface-2 cursor-pointer",
                  isSaved && "text-accent",
                )}
              >
                {isSaved ? <BookmarkCheck className="size-4" aria-hidden /> : <Bookmark className="size-4" aria-hidden />}
                {isSaved ? "Saved" : "Save"}
              </button>
              <Link
                href={`/ask?q=${encodeURIComponent(`What's the latest on ${obj.label}?`)}&object=${encodeURIComponent(obj.object_id)}`}
                className="inline-flex h-9 items-center rounded-lg bg-accent px-3 text-sm font-medium text-accent-fg hover:opacity-90"
              >
                Ask about this
              </Link>
            </div>
          </header>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
            <div className="min-w-0 space-y-5 lg:col-span-2">
              {(obj.facts ?? []).length > 0 && (
                <Card data-testid="summary">
                  <CardHeader>
                    <CardTitle>Summary</CardTitle>
                  </CardHeader>
                  <CardBody>
                    <FactList facts={obj.facts} />
                  </CardBody>
                </Card>
              )}
              {(obj.conflicts ?? []).map((c, i) => (
                <ConflictCard key={i} conflict={c} />
              ))}
              <LateLines lines={obj.late_lines ?? []} />
              <Timeline events={obj.timeline ?? []} />
            </div>
            <div className="min-w-0 space-y-5">
              {obj.landed_cost && <LandedCostCard lc={obj.landed_cost} />}
              <People people={obj.people ?? []} />
              <Linked items={obj.linked ?? []} />
              {(obj.related ?? []).length > 0 && (
                <Card>
                  <CardHeader>
                    <CardTitle>Related</CardTitle>
                  </CardHeader>
                  <CardBody>
                    <ul className="-mx-2 space-y-0.5">
                      {obj.related!.map((r) => (
                        <li key={r.object_id}>
                          <Link
                            href={safeHref(r.open_url) ?? objectHref(r.type, r.object_id)}
                            className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-surface-2"
                          >
                            <span className="truncate">{r.label}</span>
                            <span className="shrink-0 text-xs text-muted">{typeLabel(r.type)}</span>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </CardBody>
                </Card>
              )}
            </div>
          </div>
          <NotFoundSection items={obj.not_found} />
          <HiddenNotice hidden={obj.hidden} />
          <FreshnessLabel freshness={obj.freshness} />
        </div>
      )}
    </div>
  );
}
