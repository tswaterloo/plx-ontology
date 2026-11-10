"use client";

import { ArrowUpRight, CornerDownRight, SquarePen } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { AnswerCard } from "@/components/answer-card";
import { AskBox } from "@/components/ask-box";
import { loadHome, loadLate } from "@/components/shell";
import { AgeRing } from "@/components/ui/glyphs";
import { Monogram } from "@/components/ui/monogram";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { cn } from "@/lib/cn";
import { VERIFIED_EXAMPLES } from "@/lib/examples";
import { recordLabel } from "@/lib/format";
import { takePending } from "@/lib/pending";
import { useSaved } from "@/lib/saved";
import { useThread } from "@/lib/thread";
import type { LatePo } from "@/lib/types";

/** The conversation column and the answer's rail share this frame, and the composer lines up with the column. */
const FRAME = "mx-auto w-full max-w-[1048px] px-4 sm:px-8";
const COLUMN = "xl:pr-[296px]";

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-2 text-[12px] leading-4 font-medium text-muted">{children}</h2>;
}

/** Where to start: the oldest late orders, the questions the demo verifies, and the reader's saved ones. */
function StartingPoints({ onAsk }: { onAsk: (q: string, objectId?: string) => void }) {
  const [late, setLate] = useState<LatePo[] | null>(null);
  const [examples, setExamples] = useState<string[]>(VERIFIED_EXAMPLES);
  const saved = useSaved().filter((s) => s.kind === "question");

  useEffect(() => {
    let live = true;
    loadLate()
      .then((all) => live && setLate([...all].sort((a, b) => (b.days_late ?? 0) - (a.days_late ?? 0)).slice(0, 6)))
      .catch(() => live && setLate([]));
    loadHome()
      .then((h) => {
        if (!live) return;
        if (h.examples?.length) setExamples(h.examples.slice(0, 6));
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  // recent questions are in the sidebar already; saved ones are the reader's own
  const mine = [...new Set(saved.map((s) => s.label))].slice(0, 5);
  const row = "group flex min-h-9 w-full items-center gap-3 rounded-lg px-2 py-1 text-left text-[13px] hover:bg-hover cursor-pointer";

  return (
    <div className="grid grid-cols-1 gap-x-10 gap-y-8 md:grid-cols-2">
      <section className="min-w-0" aria-label="Late orders to ask about">
        <SectionTitle>Late orders</SectionTitle>
        {late === null ? (
          <div className="space-y-2 pt-1">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="flex items-center gap-3 px-2">
                <div className="skeleton size-7 rounded-[8px]" />
                <div className="skeleton h-3.5 flex-1" />
              </div>
            ))}
          </div>
        ) : late.length === 0 ? (
          <p className="px-2 text-[13px] text-muted">No late POs.</p>
        ) : (
          <ul className="-mx-2">
            {late.map((p) => {
              const po = recordLabel("PurchaseOrder", p.label);
              const q = p.supplier ? `Why is ${po} from ${p.supplier} late?` : `Why is ${po} late?`;
              return (
                <li key={p.object_id}>
                  <button type="button" onClick={() => onAsk(q, p.object_id)} className={row} title={q}>
                    <Monogram name={p.supplier} size={28} />
                    <span className="min-w-0 flex-1 truncate">
                      <span className="font-medium">{po}</span>
                      {p.supplier && <span className="text-muted"> {p.supplier}</span>}
                    </span>
                    {p.days_late != null && (
                      <span className="flex shrink-0 items-center gap-1.5 text-[12px] font-medium tabular-nums">
                        {p.days_late}d
                        <AgeRing days={p.days_late} size={13} />
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        <Link href="/late" className="-mx-2 mt-1 inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg">
          All late orders
          <ArrowUpRight className="size-3.5" aria-hidden />
        </Link>
      </section>

      <div className="min-w-0 space-y-8">
        <section>
          <SectionTitle>Try asking</SectionTitle>
          <ul className="-mx-2" aria-label="Example questions">
            {examples.map((s) => (
              <li key={s}>
                <button type="button" onClick={() => onAsk(s)} className={row}>
                  <CornerDownRight className="size-3.5 shrink-0 text-faint group-hover:text-fg" strokeWidth={2} aria-hidden />
                  <span className="min-w-0 flex-1">{s}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
        {mine.length > 0 && (
          <section>
            <SectionTitle>Saved</SectionTitle>
            <ul className="-mx-2" aria-label="Saved questions">
              {mine.map((s) => (
                <li key={s}>
                  <button type="button" onClick={() => onAsk(s)} className={row}>
                    <CornerDownRight className="size-3.5 shrink-0 text-faint group-hover:text-fg" strokeWidth={2} aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{s}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}

function Ask() {
  const params = useSearchParams();
  const router = useRouter();
  const { turns, run, stop, reset, busy } = useThread();
  const started = useRef<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const lastTurn = useRef<HTMLElement>(null);
  const q = params.get("q");
  const scope = params.get("object");
  // the document the question is about ("Ask about this" on a source page)
  const source = params.get("source") ?? undefined;

  useEffect(() => {
    const key = `${q}\u0000${scope}\u0000${source}`;
    if (q && started.current !== key) {
      started.current = key;
      if (turns.length > 0) reset();
      void run(q, scope ?? takePending(q)[0]?.object_id, source);
    }
  }, [q, scope, source, run, reset, turns.length]);

  // A new question scrolls to its own top: the answer then grows below it, never under the reader's eyes.
  useEffect(() => {
    if (turns.length > 1) lastTurn.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [turns.length]);

  const empty = turns.length === 0;
  const composer = (
    <AskBox
      size={empty ? "lg" : "md"}
      dropUp={!empty}
      onSubmit={(question, pins) => run(question, pins[0]?.object_id, pins.length ? undefined : source)}
      busy={busy}
      onStop={stop}
      autoFocus={!q}
      placeholder={turns.length ? "Ask a follow-up" : "Ask about a PO, SO, lot, supplier or customer"}
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar
        title="Ask"
        actions={
          !empty && (
            <ToolbarButton
              onClick={() => {
                reset();
                started.current = null;
                router.replace("/ask");
              }}
              title="New question"
              aria-label="New question"
            >
              <SquarePen strokeWidth={2} aria-hidden />
              <span className="max-sm:hidden">New question</span>
            </ToolbarButton>
          )
        }
      />
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
        {empty ? (
          <div className={cn(FRAME, "flex min-h-full max-w-[920px] flex-col justify-center pt-6 pb-[max(48px,8vh)]")}>
            <h1 className="sr-only">Ask</h1>
            {composer}
            <div className="mt-10">
              <StartingPoints onAsk={(question, objectId) => run(question, objectId)} />
            </div>
          </div>
        ) : (
          <div className={cn(FRAME, "pt-7 pb-10")} aria-live="off">
            <h1 className="sr-only">Ask</h1>
            {turns.map((t, i) => (
              <section
                key={t.id}
                ref={i === turns.length - 1 ? lastTurn : undefined}
                className={cn("scroll-mt-6", i > 0 && "mt-14", i === turns.length - 1 && turns.length > 1 && "min-h-[calc(100dvh-260px)]")}
              >
                <h2 className={cn("mb-4 text-[19px] leading-[26px] font-semibold tracking-[-0.02em] break-words", COLUMN)} data-testid="user-question">
                  {t.question}
                </h2>
                <AnswerCard
                  turn={t}
                  onRetry={() => run(t.question)}
                  onPick={busy ? undefined : (c) => run(`I mean ${c.label}`, c.object_id)}
                  onAsk={busy ? undefined : (next) => run(next)}
                />
              </section>
            ))}
            {turns[turns.length - 1]?.phase === "done" && (
              <p className="sr-only" role="status">
                Answer ready.
              </p>
            )}
          </div>
        )}
      </div>
      {!empty && (
        <div className="relative shrink-0 bg-bg pt-1 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:pb-4" data-testid="composer-dock">
          <div className="pointer-events-none absolute inset-x-0 -top-6 h-6 bg-gradient-to-t from-bg to-transparent" aria-hidden />
          <div className={FRAME}>
            <div className={COLUMN}>{composer}</div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function AskPage() {
  return (
    <Suspense fallback={null}>
      <Ask />
    </Suspense>
  );
}
