"use client";

import { AskNote } from "@/components/ask-note";
import { ArrowUp, BarChart3, Check, Clock, Code2, Copy, Download, FileText, MessagesSquare, PanelLeftOpen, PanelRightClose, PanelRightOpen, Pin, SquarePen, Table2, X } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnswerCard, isSimple } from "@/components/answer-card";
import { AnswerCharts, pinChartSpec } from "@/components/answer-chart";
import { AskBox } from "@/components/ask-box";
import { AskChatList } from "@/components/ask-chats";
import { AskStart, spaceById, SpaceSwitcher, type AskSpaceId } from "@/components/ask-start";
import { saveCsv } from "@/components/charts/export";
import { ClarifyPart } from "@/components/clarify-part";
import { ExcelExportButton } from "@/components/excel-export-button";
import { artifactsFromAnswer, type CanvasArtifact } from "@/components/simple-answer";
import { PageIntro } from "@/components/ui/page-intro";
import { ErrorNotice } from "@/components/ui/notice";
import { Sheet } from "@/components/ui/sheet";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { cn } from "@/lib/cn";
import { formatDate, plural } from "@/lib/format";
import { takePending } from "@/lib/pending";
import { askUrlKey, conversationOf, lastActiveAskId, useThread } from "@/lib/thread";
import { getWorkThread, saveInsight } from "@/lib/api";
import { FINAL_ERRORS, plainError } from "@/lib/errors";
import type { Answer } from "@/lib/types";

/** Clean centered reading measure for simple Ask conversations and composer; full-rail cards expand only when needed. */
const READING_FRAME = "mx-auto w-full max-w-[760px] px-4 sm:px-6";
const FULL_FRAME = "mx-auto w-full max-w-[1048px] px-4 sm:px-8";
const FULL_COLUMN = "xl:pr-[296px]";

const PANEL_BTN =
  "inline-flex h-7 items-center gap-1.5 rounded-lg border border-border bg-bg px-2.5 text-[12px] font-medium text-fg transition-all duration-150 hover:border-border-strong hover:bg-surface-2 active:scale-[0.98] disabled:opacity-50 cursor-pointer";

const KIND_META = {
  chart: { label: "Chart", icon: BarChart3 },
  table: { label: "Table", icon: Table2 },
  document: { label: "Document", icon: FileText },
} as const;

function AskSidePanelContent({ artifact, onClose }: { artifact: CanvasArtifact; onClose?: () => void }) {
  const [showSql, setShowSql] = useState(false);
  const [copiedSql, setCopiedSql] = useState(false);
  const [pinState, setPinState] = useState<"idle" | "saving" | "pinned" | "failed">("idle");
  const [pinError, setPinError] = useState("");

  useEffect(() => {
    setShowSql(false);
    setCopiedSql(false);
    setPinState("idle");
    setPinError("");
  }, [artifact.id]);

  const meta = KIND_META[artifact.kind];
  const Icon = meta.icon;
  const pinnableChart = (artifact.charts ?? []).find((c) => !!c.sql);
  const sqlItems: { label: string; sql: string }[] = [];
  const seenSql = new Set<string>();
  for (const c of artifact.charts ?? []) {
    if (c.sql && !seenSql.has(c.sql)) {
      seenSql.add(c.sql);
      sqlItems.push({ label: c.title || artifact.title, sql: c.sql });
    }
  }
  if (artifact.table?.sql && !seenSql.has(artifact.table.sql)) {
    seenSql.add(artifact.table.sql);
    sqlItems.push({ label: artifact.title, sql: artifact.table.sql });
  }
  for (const t of artifact.cited ?? []) {
    if (t.query && !seenSql.has(t.query)) {
      seenSql.add(t.query);
      sqlItems.push({ label: t.about, sql: t.query });
    }
  }

  const exportTables = artifact.table
    ? [
        {
          title: artifact.title,
          columns: artifact.table.head,
          rows: artifact.table.rows,
          sql: artifact.table.sql ?? null,
          as_of: artifact.table.asOf ?? null,
          total_rows: artifact.table.totalRows ?? artifact.table.rows.length,
          records: artifact.table.totalRows ?? artifact.table.rows.length,
          truncated: Boolean(artifact.table.truncated),
        },
      ]
    : (artifact.cited ?? []).map((t) => ({
        title: t.about,
        columns: t.columns,
        rows: t.rows,
        sql: t.query ?? null,
        as_of: t.as_of ?? null,
        total_rows: t.records,
        records: t.records,
        truncated: t.rows.length < t.records,
      }));

  const handlePin = async () => {
    if (!pinnableChart?.sql || pinState === "saving") return;
    setPinState("saving");
    setPinError("");
    try {
      await saveInsight({
        title: (pinnableChart.title || artifact.question).slice(0, 160) || "Pinned chart",
        question: artifact.question.slice(0, 2000),
        sql: pinnableChart.sql,
        chart: pinChartSpec(pinnableChart),
        schedule: "",
        notify: "never",
      });
      setPinState("pinned");
    } catch (err) {
      setPinError(plainError(err));
      setPinState("failed");
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg-subtle">
      <div className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border border-border bg-surface text-accent" aria-hidden>
            <Icon className="size-4" strokeWidth={2} />
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-[14px] leading-5 font-semibold text-fg">{artifact.title}</h2>
              <span className="shrink-0 rounded-md border border-border bg-surface px-1.5 py-0.5 text-[11px] font-medium text-muted">
                {meta.label}
              </span>
            </div>
            <p className="truncate text-[12px] leading-4 text-muted">{artifact.subtitle}</p>
          </div>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close side panel"
            title="Close side panel"
            data-testid="close-side-panel"
            className="inline-flex size-7 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-fg cursor-pointer"
          >
            <X className="size-4" strokeWidth={2} aria-hidden />
          </button>
        )}
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b border-border/80 bg-surface/50 px-4 py-2" data-testid="side-panel-actions">
        {sqlItems.length > 0 && (
          <button
            type="button"
            onClick={() => setShowSql((v) => !v)}
            aria-expanded={showSql}
            data-testid="side-panel-sql-btn"
            className={cn(PANEL_BTN, showSql && "border-accent/40 bg-accent-soft text-accent")}
          >
            <Code2 className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
            <span>{showSql ? "Hide SQL" : "Show SQL"}</span>
          </button>
        )}
        {(exportTables.length > 0 || (artifact.charts ?? []).length > 0) && (
          <ExcelExportButton
            title={artifact.title || artifact.question}
            label="Export Excel"
            className={PANEL_BTN}
            req={() => ({
              title: artifact.title || artifact.question,
              summary: artifact.summary ?? "",
              tables: exportTables,
              charts: artifact.charts ?? [],
            })}
          />
        )}
        {pinnableChart && (
          pinState === "pinned" ? (
            <Link
              href="/insights"
              data-testid="side-panel-pin-done"
              className="inline-flex h-7 items-center gap-1.5 rounded-lg border border-ok-line bg-ok-soft px-2.5 text-[12px] font-medium text-ok hover:opacity-85"
            >
              <Check className="size-3.5 shrink-0" strokeWidth={2.2} aria-hidden />
              <span>Pinned to Dashboard</span>
            </Link>
          ) : (
            <button
              type="button"
              onClick={() => void handlePin()}
              disabled={pinState === "saving"}
              data-testid="side-panel-pin-btn"
              className={PANEL_BTN}
            >
              <Pin className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
              <span>{pinState === "saving" ? "Pinning" : "Pin to Dashboard"}</span>
            </button>
          )
        )}
        {pinState === "failed" && pinError && (
          <span className="text-[12px] text-late" role="alert">
            {pinError}
          </span>
        )}
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        {showSql && sqlItems.length > 0 && (
          <div className="space-y-2 rounded-xl border border-border bg-bg p-3 plx-panel-animate" data-testid="side-panel-sql">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">SQL query</span>
              <button
                type="button"
                onClick={() => {
                  const full = sqlItems.map((s) => s.sql).join("\n\n");
                  if (typeof navigator !== "undefined" && navigator.clipboard) {
                    void navigator.clipboard.writeText(full).then(() => {
                      setCopiedSql(true);
                      setTimeout(() => setCopiedSql(false), 1800);
                    });
                  }
                }}
                className="inline-flex h-6 items-center gap-1 rounded-md px-2 text-[11.5px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                {copiedSql ? <Check className="size-3 text-ok" strokeWidth={2.2} aria-hidden /> : <Copy className="size-3" strokeWidth={2} aria-hidden />}
                <span>{copiedSql ? "Copied" : "Copy"}</span>
              </button>
            </div>
            {sqlItems.map((item, idx) => (
              <div key={idx} className="space-y-1">
                {sqlItems.length > 1 && <p className="text-[11.5px] font-medium text-muted">{item.label}</p>}
                <pre className="overflow-x-auto whitespace-pre-wrap rounded-lg border border-border bg-surface/60 p-2.5 font-mono text-[11.5px] leading-5 text-fg">
                  {item.sql}
                </pre>
              </div>
            ))}
          </div>
        )}

        {artifact.kind === "chart" && artifact.charts && artifact.charts.length > 0 && (
          <div className="rounded-xl border border-border bg-bg p-4">
            <AnswerCharts charts={artifact.charts} question={artifact.question} caption={false} hideFooter />
          </div>
        )}

        {artifact.kind === "table" && artifact.table && (
          <div className="space-y-2.5 rounded-xl border border-border bg-bg p-3.5">
            <div className="flex items-center justify-between gap-2 text-[12px] text-muted">
              <span className="tabular-nums">
                {plural(artifact.table.rows.length, "row")}
                {artifact.table.asOf ? ` · as of ${formatDate(artifact.table.asOf)}` : ""}
              </span>
              <button
                type="button"
                onClick={() => saveCsv(artifact.table!.head, artifact.table!.rows, artifact.title)}
                className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                <Download className="size-3.5" strokeWidth={2} aria-hidden />
                <span>CSV</span>
              </button>
            </div>
            <div className="max-h-[60vh] overflow-auto rounded-lg border border-border">
              <table className="w-full border-collapse text-left text-[12.5px]">
                <thead className="sticky top-0 bg-surface">
                  <tr className="border-b border-border-strong">
                    {artifact.table.head.map((h, i) => (
                      <th key={i} className="whitespace-nowrap px-2.5 py-2 text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {artifact.table.rows.map((r, i) => (
                    <tr key={i} className="border-b border-border/60 last:border-0 hover:bg-hover/40">
                      {artifact.table!.head.map((_, j) => (
                        <td key={j} className="px-2.5 py-2 align-top text-fg tabular-nums">
                          {(r[j] ?? "").replace(/\*\*/g, "")}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {artifact.kind === "document" && artifact.documents && artifact.documents.length > 0 && (
          <div className="space-y-3">
            {artifact.documents.map((d) => (
              <div key={d.download_url} className="space-y-3 rounded-xl border border-border bg-bg p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-[14px] font-semibold text-fg">{d.name}</p>
                    <p className="text-[12px] text-muted">{Math.max(1, Math.round(d.size / 1024))} KB</p>
                  </div>
                  <a
                    href={d.download_url}
                    download={d.name}
                    className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg bg-fg px-3 text-[12px] font-medium text-bg hover:opacity-90"
                  >
                    <Download className="size-3.5" strokeWidth={2} aria-hidden />
                    <span>Download</span>
                  </a>
                </div>
                {(d.changes ?? []).length > 0 && (
                  <div className="space-y-1.5 border-t border-border pt-3 text-[12.5px]">
                    <p className="text-[11px] font-semibold uppercase tracking-[0.04em] text-faint">
                      {(d.changes ?? []).length === 1 ? "1 revision" : `${(d.changes ?? []).length} revisions`}
                    </p>
                    <ol className="space-y-1">
                      {(d.changes ?? []).map((c, idx) => (
                        <li key={idx}>
                          <span className="text-muted">{c.field}: </span>
                          {c.before && <del className="text-late">{c.before}</del>}
                          {c.before && c.after && " "}
                          {c.after && <ins className="text-ok no-underline">{c.after}</ins>}
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Ask() {
  const params = useSearchParams();
  const router = useRouter();
  const {
    turns,
    queued = [],
    run,
    queue,
    steer,
    removeQueued,
    sendQueuedNow,
    retry,
    deeper,
    followUp,
    stop,
    reset,
    busy,
    seed,
    select,
    claimUrlKey,
    conversationId,
  } = useThread();
  const started = useRef<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const lastTurn = useRef<HTMLElement>(null);
  const lastQueued = useRef<HTMLDivElement>(null);
  const [spaceId, setSpaceId] = useState<AskSpaceId>("all");
  const [draftQuery, setDraftQuery] = useState("");
  const [activeArtifact, setActiveArtifact] = useState<CanvasArtifact | null>(null);
  const [panelWidth, setPanelWidth] = useState(480);
  const resizing = useRef(false);
  const activeSpace = spaceById(spaceId);
  const spaceOpts = activeSpace.promptPrefix
    ? { promptPrefix: activeSpace.promptPrefix, spaceLabel: activeSpace.label }
    : undefined;

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
    const claimed = typeof claimUrlKey === "function" ? claimUrlKey(key) : started.current !== key;
    if (q && started.current !== key && claimed) {
      started.current = key;
      if (turns.length > 0) reset();
      void run(q, scope ?? takePending(q)[0]?.object_id, source, [], page);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- page follows the same URL params
  }, [q, scope, source, meetingId, itemId, pagePath, briefType, run, reset, claimUrlKey, turns.length]);

  // "Continue" from the work log: the session's turns as they were answered; a follow-up
  // carries their history and conversation id like any other. When the conversation is
  // already live in the client store (e.g. clicking away and back), switch to it immediately
  // without refetching or interrupting its stream.
  const resumeId = params.get("c");
  const resumed = useRef<string | null>(null);
  // a conversation that could not be opened says so, with a way to try again (never a blank page)
  const [resumeFailed, setResumeFailed] = useState<string | null>(null);
  const [resumeTry, setResumeTry] = useState(0);
  const selectRef = useRef(select);
  selectRef.current = select;
  useEffect(() => {
    const key = `${resumeId}#${resumeTry}`;
    if (!resumeId || q || resumed.current === key) return;
    resumed.current = key;
    setResumeFailed(null);
    if (typeof selectRef.current === "function" && selectRef.current(resumeId)) return;
    const c = new AbortController();
    let settled = false;
    getWorkThread(`ask:${resumeId}`, c.signal)
      .finally(() => {
        settled = true;
      })
      .then((t) =>
        seed(
          (t.resume.turns ?? []).map((x) => ({ question: x.question, answer: (x.answer ?? null) as unknown as Answer | null, withheld: x.withheld })),
          { conversationId: resumeId, activeRun: t.resume.active_run ?? null },
        ),
      )
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

  // After a full browser reload on /ask (no ?c= or ?q=), reattach to the last active
  // conversation if the server still has it running or already saved its finished answer.
  const reloadedRef = useRef(false);
  useEffect(() => {
    if (reloadedRef.current || resumeId || q || turns.length > 0) return;
    reloadedRef.current = true;
    const savedId = typeof lastActiveAskId === "function" ? lastActiveAskId() : null;
    if (!savedId) return;
    if (typeof selectRef.current === "function" && selectRef.current(savedId)) return;
    const c = new AbortController();
    getWorkThread(`ask:${savedId}`, c.signal)
      .then((t) => {
        if (c.signal.aborted) return;
        const prior = (t.resume.turns ?? []).map((x) => ({
          question: x.question,
          answer: (x.answer ?? null) as unknown as Answer | null,
          withheld: x.withheld,
        }));
        if (prior.length > 0 || t.resume.active_run) {
          seed(prior, { conversationId: savedId, activeRun: t.resume.active_run ?? null });
        }
      })
      .catch(() => undefined);
    return () => c.abort();
  }, [resumeId, q, turns.length, seed]);

  // Smooth scroll-to-latest so newly submitted or queued follow-ups immediately appear in view.
  useEffect(() => {
    if (queued.length > 0) {
      lastQueued.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } else if (turns.length > 1) {
      lastTurn.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [turns.length, queued.length]);

  const empty = turns.length === 0;
  const [chatsOpen, setChatsOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(true);
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(max-width: 1023px)");
    const sync = () => setIsMobile(mq.matches);
    sync();
    mq.addEventListener?.("change", sync);
    return () => mq.removeEventListener?.("change", sync);
  }, []);

  const latestArtifact = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i--) {
      const t = turns[i];
      if (!isSimple(t)) continue;
      const list = artifactsFromAnswer(t.answer, t.charts, t.question, t.id);
      if (list.length > 0) return list[0];
    }
    return null;
  }, [turns]);

  // Drag resize for the desktop Split Canvas Panel.
  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (!resizing.current) return;
      const next = Math.min(720, Math.max(360, window.innerWidth - e.clientX));
      setPanelWidth(next);
    };
    const onUp = () => {
      resizing.current = false;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, []);

  const newChat = useCallback(() => {
    reset();
    started.current = null;
    setChatsOpen(false);
    setActiveArtifact(null);
    router.replace("/ask");
  }, [reset, router]);

  // Cmd/Ctrl+Shift+O starts a fresh chat cleanly from anywhere on /ask.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && !e.altKey && e.key.toLowerCase() === "o") {
        e.preventDefault();
        newChat();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newChat]);

  // the chat open now: the live store's conversation, else the answered one or ?c=
  const current = conversationId ?? conversationOf(turns) ?? resumeId;
  // the list is read again as each answer starts or lands (a new chat appears, its time moves)
  const answered = turns.filter((t) => t.phase === "done").length;
  const hasFullCard = turns.some((t) => !isSimple(t));
  const streamFrame = hasFullCard ? FULL_FRAME : READING_FRAME;
  const streamColumn = hasFullCard ? FULL_COLUMN : "";
  // when the latest turn asks a clarifying question, render its quick-pick choices directly
  // above the ONE unified composer so there are never two competing input boxes
  const latestTurn = turns[turns.length - 1];
  const activeClarify = !busy && latestTurn?.phase === "done" && latestTurn.answer?.clarify ? latestTurn.answer : null;

  const composer = (
    <AskBox
      size={empty ? "lg" : "md"}
      dropUp={!empty}
      onDraftChange={empty ? setDraftQuery : undefined}
      onSubmit={(question, pins, files, opts) => {
        if (activeClarify && pins.length === 0 && files.length === 0 && !opts?.deep) {
          void run(question, undefined, undefined, [], { clarified: { answer_id: activeClarify.answer_id, text: question } }, spaceOpts);
          return;
        }
        void run(
          question,
          pins[0]?.object_id,
          pins.length ? undefined : source,
          files,
          { ...(pins.length ? {} : page), ...(opts?.deep ? { deep: true } : {}) },
          spaceOpts,
        );
      }}
      onQueue={(question, pins, files, opts) => {
        queue(
          question,
          pins[0]?.object_id,
          pins.length ? undefined : source,
          files,
          { ...(pins.length ? {} : page), ...(opts?.deep ? { deep: true } : {}) },
          spaceOpts,
        );
      }}
      onSteer={(question, pins, files, opts) => {
        void steer(
          question,
          pins[0]?.object_id,
          pins.length ? undefined : source,
          files,
          { ...(pins.length ? {} : page), ...(opts?.deep ? { deep: true } : {}) },
          spaceOpts,
        );
      }}
      attachable
      deepable
      draftKey="plx.ask.draft"
      busy={busy}
      onStop={stop}
      autoFocus={!q}
      placeholder={
        activeClarify
          ? "Pick an option above, or type your answer"
          : busy
            ? "Type a follow-up to queue (Enter) or steer now (Cmd+Enter)"
            : turns.length
              ? `Ask a follow-up${activeSpace.id !== "all" ? ` in ${activeSpace.label}` : ""}`
              : activeSpace.id !== "all"
                ? `Ask ${activeSpace.label}, @ for objects, / for skills...`
                : "Ask a question, @ for objects, / for skills..."
      }
    />
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg" data-own-scroll>
      <Toolbar
        title="Ask"
        actions={
          <>
            {!empty && (
              <SpaceSwitcher
                value={spaceId}
                onChange={setSpaceId}
                compact
                className="mr-1.5 hidden md:inline-flex"
              />
            )}
            {!empty && latestArtifact && (
              <ToolbarButton
                onClick={() => setActiveArtifact((cur) => (cur ? null : latestArtifact))}
                title={activeArtifact ? "Hide side panel" : "Open side panel"}
                aria-label={activeArtifact ? "Hide side panel" : "Open side panel"}
                data-testid="toggle-side-panel"
                className="hidden lg:inline-flex"
              >
                {activeArtifact ? <PanelRightClose strokeWidth={2} aria-hidden /> : <PanelRightOpen strokeWidth={2} aria-hidden />}
                <span className="text-[12.5px]">Panel</span>
              </ToolbarButton>
            )}
            {!railOpen && (
              <ToolbarButton
                onClick={() => setRailOpen(true)}
                title="Show chats"
                aria-label="Show chats"
                data-testid="expand-ask-chats"
                className="hidden md:inline-flex"
              >
                <PanelLeftOpen strokeWidth={2} aria-hidden />
                <span className="text-[12.5px]">Chats</span>
              </ToolbarButton>
            )}
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
        <AskChatList current={current} refreshKey={`${current}#${answered}#${busy ? 1 : 0}`} onNew={newChat} className="h-full" />
      </Sheet>
      {activeArtifact && isMobile && (
        <Sheet open={!!activeArtifact} onOpenChange={(v) => { if (!v) setActiveArtifact(null); }} title={activeArtifact.title} bodyClassName="p-0">
          <AskSidePanelContent artifact={activeArtifact} />
        </Sheet>
      )}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {railOpen && (
          <aside className="hidden w-[260px] shrink-0 border-r border-border md:flex" data-testid="ask-chats-rail">
            <AskChatList
              current={current}
              refreshKey={`${current}#${answered}#${busy ? 1 : 0}`}
              onNew={newChat}
              onCollapse={() => setRailOpen(false)}
              className="w-full"
            />
          </aside>
        )}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          {/* relative: the thread's screen-reader lines (absolute) belong to this scroller, so they
              never stretch the page under it and make it scroll as a whole on a phone */}
          <div ref={scroller} className="relative min-h-0 flex-1 overflow-y-auto">
            {empty ? (
              <div className="mx-auto flex min-h-full w-full max-w-[740px] flex-col justify-center px-4 pt-10 pb-[max(56px,13vh)] sm:px-6">
                {resumeFailed && resumeId && !q && (
                  <ErrorNotice className="mb-6" message={`This conversation couldn't be opened. ${resumeFailed}`} onRetry={FINAL_ERRORS.has(resumeFailed) ? undefined : () => setResumeTry((n) => n + 1)} />
                )}
                <AskStart
                  spaceId={spaceId}
                  onSpaceChange={setSpaceId}
                  draftQuery={draftQuery}
                  onAsk={(question, objectId) => void run(question, objectId, undefined, [], {}, spaceOpts)}
                  subtitle={<PageIntro path="/ask" className="mx-auto mt-1.5 max-w-[54ch] px-0 pt-0 text-center lg:px-0" />}
                >
                  {composer}
                </AskStart>
              </div>
            ) : (
              <div className={cn(streamFrame, "space-y-8 pt-7 pb-28")} aria-live="off">
                <h1 className="sr-only">Ask</h1>
                {turns.map((t, i) => (
                  <section
                    key={t.id}
                    ref={i === turns.length - 1 ? lastTurn : undefined}
                    className={cn(
                      "scroll-mt-6 space-y-6",
                      i > 0 && "border-t border-border/60 pt-8",
                      i === turns.length - 1 && turns.length > 1 && queued.length === 0 && "min-h-[calc(100dvh-260px)]",
                    )}
                  >
                    <div className={cn("flex flex-col items-end", streamColumn)}>
                      {t.spaceLabel && (
                        <span
                          className="mb-1.5 inline-flex items-center rounded-md border border-border bg-surface px-2 py-0.5 text-[11px] font-medium text-muted"
                          data-testid="turn-space-label"
                        >
                          {t.spaceLabel}
                        </span>
                      )}
                      <h2
                        className="max-w-[85%] rounded-2xl border border-border bg-surface-2 px-4 py-2.5 text-[14px] leading-[21px] font-medium text-fg break-words"
                        data-testid="user-question"
                      >
                        {t.question}
                      </h2>
                    </div>
                    <AnswerCard
                      turn={t}
                      onRetry={() => retry(t)}
                      onPick={busy ? undefined : (c) => run(`I mean ${c.label}`, c.object_id, undefined, [], {}, spaceOpts)}
                      onAsk={busy ? undefined : (next) => followUp(next, t)}
                      onInstruct={busy ? undefined : (next, extra) => run(next, extra.object_id ?? undefined, extra.source ?? undefined, [], { context: extra.context, mode: extra.mode, instruction: extra.instruction }, spaceOpts)}
                      onClarify={busy ? undefined : (words, reply) => run(words, undefined, undefined, [], { clarified: reply }, spaceOpts)}
                      onDeeper={busy ? undefined : () => deeper(t)}
                      onOpenArtifact={setActiveArtifact}
                      activeArtifactId={activeArtifact?.id ?? null}
                      noteRecord={t.request?.objectId ?? scope ?? undefined}
                      showNote
                      hideClarify={activeClarify?.answer_id === t.answer?.answer_id && i === turns.length - 1}
                    />
                    {t.phase === "done" && !isSimple(t) && <AskNote record={t.request?.objectId ?? scope ?? undefined} />}
                  </section>
                ))}

                {queued.length > 0 && (
                  <div className={cn("space-y-3", streamColumn)} aria-label="Queued follow-ups" data-testid="queued-followups">
                    {queued.map((item, idx) => (
                      <div
                        key={item.id}
                        ref={idx === queued.length - 1 ? lastQueued : undefined}
                        data-testid="queued-followup-card"
                        className="rounded-2xl border border-border-strong bg-surface/85 px-4 py-3 shadow-sm"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="inline-flex items-center gap-1.5 text-[11.5px] font-medium text-muted">
                            <Clock className="size-3.5 text-muted" strokeWidth={2} aria-hidden />
                            <span>{idx === 0 ? "Queued: will run next" : `Queued #${idx + 1}`}</span>
                            {item.spaceLabel && <span className="text-faint">· {item.spaceLabel}</span>}
                          </span>
                          <div className="flex items-center gap-1.5">
                            <button
                              type="button"
                              onClick={() => void sendQueuedNow(item.id)}
                              data-testid="queued-send-now"
                              className="inline-flex h-7 items-center gap-1 rounded-lg bg-fg px-2.5 text-[12px] font-medium text-bg transition-all duration-150 hover:opacity-90 active:scale-[0.97] cursor-pointer"
                            >
                              <ArrowUp className="size-3" strokeWidth={2.4} aria-hidden />
                              <span>Send now</span>
                            </button>
                            <button
                              type="button"
                              onClick={() => removeQueued(item.id)}
                              data-testid="queued-remove"
                              aria-label="Remove queued follow-up"
                              className="inline-flex h-7 items-center gap-1 rounded-lg border border-transparent px-2 text-[12px] text-muted transition-all duration-150 hover:border-border hover:bg-surface-2 hover:text-fg active:scale-[0.98] cursor-pointer"
                            >
                              <X className="size-3" strokeWidth={2} aria-hidden />
                              <span>Remove</span>
                            </button>
                          </div>
                        </div>
                        <p className="mt-2 text-[14px] leading-5 font-medium text-fg break-words">
                          {item.question}
                        </p>
                      </div>
                    ))}
                  </div>
                )}

                {turns[turns.length - 1]?.phase === "done" && (
                  <p className="sr-only" role="status">
                    {turns[turns.length - 1]?.answer?.assistant_unavailable ? "The assistant could not answer." : "Answer ready."}
                  </p>
                )}
              </div>
            )}
          </div>
          {!empty && (
            <div className="relative z-10 shrink-0 border-t border-border/50 bg-bg/95 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:pb-4" data-testid="composer-dock">
              <div className={streamFrame}>
                <div className={cn("space-y-2.5", streamColumn)}>
                  {activeClarify?.clarify && (
                    <div className="rounded-xl border border-border bg-surface/85 px-3.5 py-2.5 shadow-sm" data-testid="composer-clarify">
                      {activeClarify.clarify.question && (
                        <p className="mb-2 text-[13.5px] font-medium text-fg">{activeClarify.clarify.question}</p>
                      )}
                      <ClarifyPart
                        a={activeClarify}
                        hideInput
                        onReply={busy ? undefined : (words, reply) => run(words, undefined, undefined, [], { clarified: reply }, spaceOpts)}
                      />
                    </div>
                  )}
                  {composer}
                </div>
              </div>
            </div>
          )}
        </div>
        {!empty && activeArtifact && (
          <>
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize side panel"
              onPointerDown={() => {
                resizing.current = true;
              }}
              className="hidden w-1 shrink-0 cursor-col-resize bg-border/60 transition-colors hover:bg-accent/60 lg:block"
            />
            <aside
              style={{ width: `${panelWidth}px` }}
              className="hidden min-h-0 shrink-0 flex-col border-l border-border bg-bg-subtle plx-drawer-animate lg:flex"
              aria-label="Deliverable preview"
              data-testid="ask-side-panel"
            >
              <AskSidePanelContent artifact={activeArtifact} onClose={() => setActiveArtifact(null)} />
            </aside>
          </>
        )}
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
