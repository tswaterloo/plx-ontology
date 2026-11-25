"use client";

import { AskNote } from "@/components/ask-note";
import { MessagesSquare, SquarePen } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { AnswerCard, isSimple } from "@/components/answer-card";
import { AskBox } from "@/components/ask-box";
import { AskChatList } from "@/components/ask-chats";
import { AskStart } from "@/components/ask-start";
import { PageIntro } from "@/components/ui/page-intro";
import { ErrorNotice } from "@/components/ui/notice";
import { Sheet } from "@/components/ui/sheet";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { cn } from "@/lib/cn";
import { takePending } from "@/lib/pending";
import { askUrlKey, conversationOf, useThread } from "@/lib/thread";
import { getWorkThread } from "@/lib/api";
import { FINAL_ERRORS, plainError } from "@/lib/errors";
import type { Answer } from "@/lib/types";

/** The conversation column and the answer's rail share this frame, and the composer lines up with the column. */
const FRAME = "mx-auto w-full max-w-[1048px] px-4 sm:px-8";
const COLUMN = "xl:pr-[296px]";

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
  // asked from a brief: the brief (its record's type, the record in ``object``) goes with the line
  const briefType = params.get("brief");
  const page = briefType && scope
    ? { context: { kind: "brief" as const, object_type: briefType, object_id: scope } }
    : meetingId
    ? { context: { kind: "meeting" as const, meeting_id: meetingId } }
    : itemId
      ? { context: { kind: "autopilot" as const, item_id: itemId } }
      : pagePath
        ? { context: { kind: "page" as const, page: pagePath.slice(0, 300) } }
        : {};

  useEffect(() => {
    // the whole of what the URL asks: the question and every scope or page it carries
    const key = askUrlKey({ q, object: scope, source, meeting: meetingId, item: itemId, page: pagePath, brief: briefType });
    if (q && started.current !== key) {
      started.current = key;
      if (turns.length > 0) reset();
      void run(q, scope ?? takePending(q)[0]?.object_id, source, [], page);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- page follows the same URL params
  }, [q, scope, source, meetingId, itemId, pagePath, briefType, run, reset, turns.length]);

  // "Continue" from the work log: the session's turns as they were answered; a follow-up
  // carries their history and conversation id like any other.
  const resumeId = params.get("c");
  const resumed = useRef<string | null>(null);
  // a conversation that could not be opened says so, with a way to try again (never a blank page)
  const [resumeFailed, setResumeFailed] = useState<string | null>(null);
  const [resumeTry, setResumeTry] = useState(0);
  useEffect(() => {
    const key = `${resumeId}#${resumeTry}`;
    if (!resumeId || q || resumed.current === key) return;
    resumed.current = key;
    setResumeFailed(null);
    const c = new AbortController();
    let settled = false;
    getWorkThread(`ask:${resumeId}`, c.signal)
      .finally(() => {
        settled = true;
      })
      .then((t) => seed((t.resume.turns ?? []).map((x) => ({ question: x.question, answer: (x.answer ?? null) as unknown as Answer | null, withheld: x.withheld }))))
      .catch((err) => {
        // say why (gone, not yours, the server failed), never only "couldn't be opened"
        if (!c.signal.aborted) setResumeFailed(plainError(err));
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
      onSubmit={(question, pins, files, opts) => run(question, pins[0]?.object_id, pins.length ? undefined : source, files, { ...(pins.length ? {} : page), ...(opts?.deep ? { deep: true } : {}) })}
      attachable
      deepable
      draftKey="plx.ask.draft"
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
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          {/* relative: the thread's screen-reader lines (absolute) belong to this scroller, so they
              never stretch the page under it and make it scroll as a whole on a phone */}
          <div ref={scroller} className="relative min-h-0 flex-1 overflow-y-auto">
            {empty ? (
              <div className={cn(FRAME, "flex min-h-full max-w-[768px] flex-col justify-center pt-6 pb-[max(48px,12vh)]")}>
                {resumeFailed && resumeId && !q && (
                  <ErrorNotice className="mb-6" message={`This conversation couldn't be opened. ${resumeFailed}`} onRetry={FINAL_ERRORS.has(resumeFailed) ? undefined : () => setResumeTry((n) => n + 1)} />
                )}
                <AskStart onAsk={(question, objectId) => run(question, objectId)}>{composer}</AskStart>
                <PageIntro path="/ask" className="mt-8 px-3 pt-0 text-center lg:px-3" />
              </div>
            ) : (
              <div className={cn(FRAME, "pt-5 pb-24")} aria-live="off">
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
                      noteRecord={t.request?.objectId ?? scope ?? undefined}
                      showNote
                    />
                    {t.phase === "done" && !isSimple(t) && <AskNote record={t.request?.objectId ?? scope ?? undefined} />}
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
            <div className="relative z-10 shrink-0 border-t border-border bg-bg pt-2.5 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:pb-3.5" data-testid="composer-dock">
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
