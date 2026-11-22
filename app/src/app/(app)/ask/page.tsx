"use client";

import { MessagesSquare, SquarePen } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { AnswerCard } from "@/components/answer-card";
import { AskBox } from "@/components/ask-box";
import { AskChatList } from "@/components/ask-chats";
import { loadHome } from "@/components/shell";
import { ErrorNotice } from "@/components/ui/notice";
import { Sheet } from "@/components/ui/sheet";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { cn } from "@/lib/cn";
import { ago, formatDateTime } from "@/lib/format";
import { takePending } from "@/lib/pending";
import { askUrlKey, conversationOf, useThread } from "@/lib/thread";
import { getWorkThread } from "@/lib/api";
import type { Answer, RecentQuestion } from "@/lib/types";

/** The conversation column and the answer's rail share this frame, and the composer lines up with the column. */
const FRAME = "mx-auto w-full max-w-[1048px] px-4 sm:px-8";
const COLUMN = "xl:pr-[296px]";

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-2 text-[12px] leading-4 font-medium text-muted">{children}</h2>;
}

/** The start page under the box: the reader's own recent questions, newest first, each once. Nothing while they load or if they can't be read: the box works without them. */
function RecentQuestions({ onAsk }: { onAsk: (q: string, objectId?: string) => void }) {
  const [recent, setRecent] = useState<RecentQuestion[]>([]);

  useEffect(() => {
    let live = true;
    loadHome()
      .then((h) => {
        if (!live) return;
        setRecent(
          [...(h.recent_questions ?? [])]
            .sort((a, b) => b.asked_at.localeCompare(a.asked_at))
            .filter((r, i, all) => all.findIndex((x) => x.question.trim().toLowerCase() === r.question.trim().toLowerCase()) === i)
            .slice(0, 8),
        );
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  if (recent.length === 0) return null;
  const row = "group flex min-h-9 w-full items-center gap-3 rounded-lg px-2 py-1 text-left text-[13px] hover:bg-hover cursor-pointer";
  return (
    <section data-testid="ask-recent" className="max-w-[640px]">
      <SectionTitle>Asked lately</SectionTitle>
      <ul className="-mx-2" aria-label="Your recent questions">
        {recent.map((r) => (
          <li key={`${r.asked_at}-${r.question}`}>
            {r.conversation_id ? (
              <Link href={`/ask?c=${encodeURIComponent(r.conversation_id)}`} className={row} title={`Asked ${formatDateTime(r.asked_at)}: open the answer`}>
                <span className="min-w-0 flex-1 truncate">{r.question}</span>
                <span className="shrink-0 text-[12px] text-muted tabular-nums">{ago(r.asked_at)}</span>
              </Link>
            ) : (
              <button type="button" onClick={() => onAsk(r.question)} className={row} title={`Asked ${formatDateTime(r.asked_at)}: ask again`}>
                <span className="min-w-0 flex-1 truncate">{r.question}</span>
                <span className="shrink-0 text-[12px] text-muted tabular-nums">{ago(r.asked_at)}</span>
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Ask() {
  const params = useSearchParams();
  const router = useRouter();
  const { turns, run, retry, deeper, followUp, stop, reset, busy, seed } = useThread();
  const started = useRef<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const lastTurn = useRef<HTMLElement>(null);
  const q = params.get("q");
  const scope = params.get("object");
  // the document the question is about ("Ask about this" on a source page)
  const source = params.get("source") ?? undefined;
  // the meeting, autopilot item or page the box was opened on: its context goes with the line
  const meetingId = params.get("meeting");
  const itemId = params.get("item");
  const pagePath = params.get("page");
  const page = meetingId
    ? { context: { kind: "meeting" as const, meeting_id: meetingId } }
    : itemId
      ? { context: { kind: "autopilot" as const, item_id: itemId } }
      : pagePath
        ? { context: { kind: "page" as const, page: pagePath.slice(0, 300) } }
        : {};

  useEffect(() => {
    // the whole of what the URL asks: the question and every scope or page it carries
    const key = askUrlKey({ q, object: scope, source, meeting: meetingId, item: itemId, page: pagePath });
    if (q && started.current !== key) {
      started.current = key;
      if (turns.length > 0) reset();
      void run(q, scope ?? takePending(q)[0]?.object_id, source, [], page);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- page follows the same URL params
  }, [q, scope, source, meetingId, itemId, pagePath, run, reset, turns.length]);

  // "Continue" from the work log: the session's turns as they were answered; a follow-up
  // carries their history and conversation id like any other.
  const resumeId = params.get("c");
  const resumed = useRef<string | null>(null);
  // a conversation that could not be opened says so, with a way to try again (never a blank page)
  const [resumeFailed, setResumeFailed] = useState(false);
  const [resumeTry, setResumeTry] = useState(0);
  useEffect(() => {
    const key = `${resumeId}#${resumeTry}`;
    if (!resumeId || q || resumed.current === key) return;
    resumed.current = key;
    setResumeFailed(false);
    const c = new AbortController();
    let settled = false;
    getWorkThread(`ask:${resumeId}`, c.signal)
      .finally(() => {
        settled = true;
      })
      .then((t) => seed((t.resume.turns ?? []).map((x) => ({ question: x.question, answer: (x.answer ?? null) as unknown as Answer | null, withheld: x.withheld }))))
      .catch(() => {
        if (!c.signal.aborted) setResumeFailed(true);
      });
    return () => {
      c.abort();
      // an aborted read (strict mode, a new address) is read again on the next run
      if (!settled && resumed.current === key) resumed.current = null;
    };
  }, [resumeId, resumeTry, q, seed]);

  // A new question scrolls to its own top: the answer then grows below it, never under the reader's eyes.
  useEffect(() => {
    if (turns.length > 1) lastTurn.current?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [turns.length]);

  const empty = turns.length === 0;
  const newChat = () => {
    reset();
    started.current = null;
    setChatsOpen(false);
    router.replace("/ask");
  };
  // the chat open now: the one reopened, else the one this page's answers belong to
  const current = conversationOf(turns) ?? resumeId;
  // the list is read again as each answer lands (a new chat appears, its time moves)
  const answered = turns.filter((t) => t.phase === "done").length;
  const [chatsOpen, setChatsOpen] = useState(false);
  const composer = (
    <AskBox
      size={empty ? "lg" : "md"}
      dropUp={!empty}
      onSubmit={(question, pins, files) => run(question, pins[0]?.object_id, pins.length ? undefined : source, files, pins.length ? {} : page)}
      attachable
      busy={busy}
      onStop={stop}
      autoFocus={!q}
      placeholder={turns.length ? "Ask a follow-up" : "Ask a question, or ask for a chart, document or quote"}
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-own-scroll>
      <Toolbar
        title="Ask"
        actions={
          <>
            <ToolbarButton onClick={() => setChatsOpen(true)} title="Chats" aria-label="Chats" className="md:hidden">
              <MessagesSquare strokeWidth={2} aria-hidden />
            </ToolbarButton>
            {!empty && (
              <ToolbarButton onClick={newChat} title="New chat" aria-label="New chat" className="md:hidden">
                <SquarePen strokeWidth={2} aria-hidden />
              </ToolbarButton>
            )}
          </>
        }
      />
      <Sheet open={chatsOpen} onOpenChange={setChatsOpen} title="Chats" bodyClassName="p-0">
        <AskChatList current={current} refreshKey={`${current}#${answered}`} onNew={newChat} className="h-full" />
      </Sheet>
      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-[248px] shrink-0 border-r border-border md:flex" data-testid="ask-chats-rail">
          <AskChatList current={current} refreshKey={`${current}#${answered}`} onNew={newChat} className="w-full" />
        </aside>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {/* relative: the thread's screen-reader lines (absolute) belong to this scroller, so they
              never stretch the page under it and make it scroll as a whole on a phone */}
          <div ref={scroller} className="relative min-h-0 flex-1 overflow-y-auto">
            {empty ? (
              <div className={cn(FRAME, "flex min-h-full max-w-[920px] flex-col justify-center pt-6 pb-[max(48px,8vh)]")}>
                <h1 className="sr-only">Ask</h1>
                {resumeFailed && resumeId && !q && (
                  <ErrorNotice className="mb-6" message="This conversation couldn't be opened just now." onRetry={() => setResumeTry((n) => n + 1)} />
                )}
                <PageIntro path="/ask" className="mb-3 px-0 pt-0 lg:px-0" />
                {composer}
                <div className="mt-10 md:hidden">
                  <RecentQuestions onAsk={(question, objectId) => run(question, objectId)} />
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
                      onRetry={() => retry(t)}
                      onPick={busy ? undefined : (c) => run(`I mean ${c.label}`, c.object_id)}
                      onAsk={busy ? undefined : (next) => followUp(next, t)}
                      onInstruct={busy ? undefined : (next, extra) => run(next, extra.object_id ?? undefined, extra.source ?? undefined, [], { context: extra.context, mode: extra.mode, instruction: extra.instruction })}
                      onClarify={busy ? undefined : (words, reply) => run(words, undefined, undefined, [], { clarified: reply })}
                      onDeeper={busy ? undefined : () => deeper(t)}
                    />
                  </section>
                ))}
                {turns[turns.length - 1]?.phase === "done" && (
                  <p className="sr-only" role="status">
                    {turns[turns.length - 1]?.answer?.assistant_unavailable ? "The assistant could not answer." : "Answer ready."}
                  </p>
                )}
              </div>
            )}
          </div>
          {!empty && (
            <div className="relative shrink-0 border-t border-border bg-bg pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:pb-4" data-testid="composer-dock">
              <div className={FRAME}>
                <div className={COLUMN}>{composer}</div>
              </div>
            </div>
          )}
        </div>
      </div>
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
