"use client";

import { ArrowUpRight, Bookmark, BookmarkCheck, Loader2, Search, Sparkles } from "lucide-react";
import Link from "next/link";
import { cn } from "@/lib/cn";
import { objectHref, plural, safeHref, typeLabel } from "@/lib/format";
import { toggleSaved, useSaved } from "@/lib/saved";
import type { Answer, ObjectRef, Understanding } from "@/lib/types";
import { ConflictCard, FactList, FreshnessLabel, HiddenNotice, NotFoundSection } from "./answer-parts";
import { ProfitabilityView } from "./cost-breakdown";
import { EvidenceTrailCard } from "./evidence-trail";
import { Feedback } from "./feedback";
import { Badge } from "./ui/badge";
import type { NoticeKind } from "@/lib/errors";
import { ErrorNotice } from "./ui/notice";
import { Skeleton } from "./ui/skeleton";

export type TurnPhase = "thinking" | "streaming" | "done" | "error";

export interface Turn {
  id: string;
  question: string;
  phase: TurnPhase;
  understanding?: Understanding;
  status?: string;
  sourcesFound?: number;
  streamed: string;
  answer?: Answer;
  error?: string;
  /** Set when the error is a server state (busy, ask switched off): shown as a banner. */
  notice?: NoticeKind;
}

/** What the person picked to disambiguate: an object id plus its label. */
export interface Pick {
  object_id: string;
  type: string;
  label: string;
}

function refHref(r: ObjectRef): string {
  return safeHref(r.open_url) ?? objectHref(r.type, r.object_id);
}

const OUTCOME: Record<string, { label: string; tone: "warn" | "info" | "neutral" } | undefined> = {
  partial: { label: "Partial answer", tone: "warn" },
  not_found: { label: "Not found", tone: "neutral" },
  abstained: { label: "No supported answer", tone: "neutral" },
  refused: { label: "Can't answer this", tone: "neutral" },
};

function UnderstandingBar({ u, onPick }: { u: Understanding; onPick?: (p: Pick) => void }) {
  const entities = u.entities ?? [];
  const resolved = entities.filter((e) => e.resolved && !e.ambiguous).map((e) => e.resolved!);
  const ambiguous = entities.filter((e) => e.ambiguous && (e.candidates ?? []).length > 1);
  return (
    <div className="space-y-2" data-testid="understanding">
      {(resolved.length > 0 || u.time_frame) && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <Search className="size-3.5" aria-hidden />
          <span>Looking at</span>
          {resolved.map((r) => (
            <Link
              key={r.object_id}
              href={refHref(r)}
              className="rounded-md bg-surface-2 px-1.5 py-0.5 font-medium text-fg hover:bg-border"
              data-testid="entity-link"
            >
              {r.label}
            </Link>
          ))}
          {u.time_frame && <span className="rounded-md bg-surface-2 px-1.5 py-0.5">{u.time_frame}</span>}
        </div>
      )}
      {ambiguous.map((e, i) => (
        <DidYouMean
          key={i}
          text={e.text}
          choices={(e.candidates ?? []).slice(0, 5).map((c) => ({ object_id: c.object_id, type: c.type, label: c.label }))}
          onPick={onPick}
        />
      ))}
    </div>
  );
}

function DidYouMean({ text, choices, onPick }: { text?: string; choices: Pick[]; onPick?: (p: Pick) => void }) {
  if (choices.length === 0) return null;
  return (
    <div className="rounded-xl border border-accent/30 bg-accent-soft/50 p-3 text-sm" data-testid="did-you-mean">
      <p className="mb-2">
        <span className="font-medium">Did you mean</span>
        {text ? <> “{text}” as</> : null}…
      </p>
      <div className="flex flex-wrap gap-1.5">
        {choices.map((c) => (
          <button
            key={c.object_id}
            type="button"
            onClick={() => onPick?.(c)}
            disabled={!onPick}
            className="rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium hover:border-accent hover:text-accent disabled:cursor-default disabled:opacity-60 cursor-pointer"
          >
            {c.label}
            <span className="font-normal text-muted"> · {typeLabel(c.type)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function SaveButton({ question, href }: { question: string; href: string }) {
  const saved = useSaved();
  const key = `q:${question}`;
  const on = saved.some((s) => s.key === key);
  const Icon = on ? BookmarkCheck : Bookmark;
  return (
    <button
      type="button"
      aria-pressed={on}
      aria-label={on ? "Remove from saved" : "Save this question"}
      onClick={() => toggleSaved({ key, kind: "question", label: question, href })}
      className={cn("rounded-md p-1.5 transition-colors hover:bg-surface-2 cursor-pointer", on ? "text-accent" : "text-muted hover:text-fg")}
    >
      <Icon className="size-4" aria-hidden />
    </button>
  );
}

export function AnswerCard({ turn, onPick, onRetry }: { turn: Turn; onPick?: (p: Pick) => void; onRetry?: () => void }) {
  const a = turn.answer;
  const text = a ? a.direct_answer : turn.streamed;
  const outcome = a ? OUTCOME[a.outcome] : undefined;
  const busy = turn.phase === "thinking" || turn.phase === "streaming";
  return (
    <article className="space-y-4 animate-fade-in" aria-busy={busy} data-testid="answer-card">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg">
          <Sparkles className="size-3.5" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 space-y-4">
          {turn.understanding && <UnderstandingBar u={turn.understanding} onPick={onPick} />}

          {busy && (
            <p className="flex items-center gap-2 text-sm text-muted" role="status" aria-live="polite">
              <Loader2 className="size-3.5 animate-spin" aria-hidden />
              {turn.status ?? "Reading your question…"}
              {turn.sourcesFound ? <span>· {plural(turn.sourcesFound, "source")} found</span> : null}
            </p>
          )}

          {outcome && <Badge tone={outcome.tone}>{outcome.label}</Badge>}
          {a?.access_label && (
            <Badge tone="info" data-testid="access-label">{a.access_label}</Badge>
          )}

          {text ? (
            <p className="text-[17px] leading-relaxed font-medium text-fg" data-testid="direct-answer">
              {text}
              {turn.phase === "streaming" && (
                <span className="ml-0.5 inline-block h-5 w-[2px] translate-y-1 bg-accent animate-blink" aria-hidden />
              )}
            </p>
          ) : turn.phase === "thinking" ? (
            <div className="space-y-2.5" aria-hidden>
              <Skeleton className="h-5 w-11/12" />
              <Skeleton className="h-5 w-3/4" />
            </div>
          ) : null}

          {turn.phase === "error" && (
            <ErrorNotice
              message={turn.error ?? "Something went wrong."}
              tone={turn.notice ? "notice" : "error"}
              onRetry={turn.notice === "off" ? undefined : onRetry}
            />
          )}

          {a && (
            <div className="space-y-4">
              <DidYouMean
                choices={(a.did_you_mean ?? []).slice(0, 6).map((r) => ({ object_id: r.object_id, type: r.type, label: r.label }))}
                onPick={onPick}
              />
              <FactList facts={a.facts} />
              {a.profitability && <ProfitabilityView p={a.profitability} />}
              <EvidenceTrailCard trail={a.trail} />
              {(a.conflicts ?? []).map((c, i) => (
                <ConflictCard key={i} conflict={c} />
              ))}
              <NotFoundSection items={a.not_found} />
              <HiddenNotice hidden={a.hidden} />
              {(a.subjects ?? []).length > 0 && (
                <div className="flex flex-wrap gap-2" aria-label="Open a record">
                  {a.subjects!.map((r) => (
                    <Link
                      key={r.object_id}
                      href={refHref(r)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm font-medium hover:bg-surface-2"
                      data-testid="subject-link"
                    >
                      Open {r.label}
                      <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
                    </Link>
                  ))}
                </div>
              )}
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
                <FreshnessLabel freshness={a.freshness} />
                <SaveButton question={turn.question} href={`/ask?q=${encodeURIComponent(turn.question)}`} />
              </div>
              <Feedback answerId={a.answer_id} />
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
