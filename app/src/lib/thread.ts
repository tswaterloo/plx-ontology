"use client";

import { useCallback, useSyncExternalStore } from "react";
import type { Turn } from "@/components/answer-card";
import * as apiClient from "./api";
import type { Attachment } from "./attachments";
import { noticeKind, plainError } from "./errors";
import { onSessionReset } from "./session-cache";
import type { Answer, AskActiveRun, AskEvent, AskRequest, HistoryTurn } from "./types";

/** The progress steps a turn keeps (the latest). */
export const MAX_STEPS = 8;

const ACTIVE_STORAGE_KEY = "plx.ask.active";
const REATTACH_ATTEMPTS = 2;
const REATTACH_DELAY_MS = 1200;

function optionalApi<K extends keyof typeof apiClient>(name: K): (typeof apiClient)[K] | undefined {
  try {
    const fn = apiClient[name];
    return typeof fn === "function" ? fn : undefined;
  } catch {
    return undefined;
  }
}

/** Pure: fold one stream event into a turn. */
export function applyEvent(turn: Turn, ev: AskEvent): Turn {
  switch (ev.event) {
    case "understanding":
      return { ...turn, understanding: ev.data };
    case "status": {
      // each plain-words step once, in the order the work did them
      const said = typeof ev.data?.message === "string" ? ev.data.message : "";
      const steps = turn.steps ?? [];
      // a read after words streamed: those words were the work's own narration, and the
      // work is reading again (not writing) until the next words come
      const reading = turn.phase === "streaming" && (ev.data?.step === "reading" || ev.data?.step === "searching");
      return { ...turn, phase: reading ? "thinking" : turn.phase, status: said || turn.status, steps: said && steps[steps.length - 1] !== said ? [...steps, said].slice(-MAX_STEPS) : steps };
    }
    case "verdict":
      return typeof ev.data?.text === "string" && ev.data.text.trim() ? { ...turn, verdict: ev.data.text.trim() } : turn;
    case "charts": {
      const incoming = Array.isArray(ev.data?.charts) ? ev.data.charts : [];
      return incoming.length > 0 ? { ...turn, charts: incoming, simple: true } : turn;
    }
    case "sources":
      return { ...turn, sourcesFound: Array.isArray(ev.data.sources) ? ev.data.sources.length : turn.sourcesFound };
    case "token": {
      const text = typeof ev.data?.text === "string" ? ev.data.text : "";
      // whole: the answer so far in full, as the server's gate passed it (replaces what was shown)
      return { ...turn, phase: "streaming", streamed: ev.data?.whole ? text : turn.streamed + text, simple: turn.simple || !!ev.data?.whole };
    }
    case "answer":
      return { ...turn, phase: "done", answer: ev.data, status: undefined };
    case "error":
      return { ...turn, phase: "error", error: plainError(ev.data), notice: noticeKind(ev.data) };
    default:
      return turn;
  }
}

/** What the Ask URL asks for, as one key: the same question with another scope, source, meeting, item or page is another request. */
export function askUrlKey(p: { q: string | null; object: string | null; source?: string; meeting: string | null; item: string | null; page: string | null; brief?: string | null }): string {
  return [p.q, p.object, p.source, p.meeting, p.item, p.page, p.brief ?? ""].join("\u0000");
}

/** The request a turn was sent with, kept whole so Retry sends exactly it again. */
export interface TurnRequest {
  objectId?: string;
  source?: string;
  files: Attachment[];
  extra: Pick<AskRequest, "context" | "mode" | "instruction" | "clarified" | "deep">;
}

/** Every prior turn, whole, sent as context so follow-ups ("and the other one?") resolve. */
export function historyOf(turns: Turn[]): HistoryTurn[] {
  const out: HistoryTurn[] = [];
  for (const t of turns) {
    out.push({ role: "user", text: t.question });
    if (t.phase === "done" && t.answer) out.push({ role: "assistant", text: t.answer.direct_answer });
  }
  return out;
}

/** The conversation id the server gave the last finished answer, if any. */
export function conversationOf(turns: Turn[]): string | undefined {
  for (let i = turns.length - 1; i >= 0; i--) {
    const id = turns[i].answer?.conversation_id;
    if (id && /^[A-Za-z0-9_-]{1,80}$/.test(id)) return id;
  }
  return undefined;
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function newConversationId(): string {
  if (typeof crypto !== "undefined" && "getRandomValues" in crypto) {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`.slice(0, 16);
}

function isTurnBusy(turn: Turn | undefined): boolean {
  return !!turn && (turn.phase === "thinking" || turn.phase === "streaming");
}

function saveActiveAskId(id: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (id) window.sessionStorage.setItem(ACTIVE_STORAGE_KEY, id);
    else window.sessionStorage.removeItem(ACTIVE_STORAGE_KEY);
  } catch {
    // private browsing / storage unavailable
  }
}

/** The conversation id that was active on /ask before a full browser reload, if any. */
export function lastActiveAskId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const id = window.sessionStorage.getItem(ACTIVE_STORAGE_KEY);
    return id && /^[A-Za-z0-9_-]{1,80}$/.test(id) ? id : null;
  } catch {
    return null;
  }
}

export interface RunningChatInfo {
  conversation_id: string;
  title: string;
  started_at: string;
}

interface ThreadRecord {
  conversationId: string;
  title: string;
  startedAt: string;
  turns: Turn[];
  ctrl: AbortController | null;
  runSeq: number;
}

interface ScopeSnapshot {
  currentId: string | null;
  turns: Turn[];
  busy: boolean;
  anyRunning: boolean;
  runningIds: ReadonlySet<string>;
  runningChats: readonly RunningChatInfo[];
}

const EMPTY_TURNS: Turn[] = [];
const EMPTY_SET: ReadonlySet<string> = new Set();
const EMPTY_CHATS: readonly RunningChatInfo[] = [];

class ThreadStore {
  readonly scope: string;
  private currentId: string | null = null;
  private threads = new Map<string, ThreadRecord>();
  private listeners = new Set<() => void>();
  private startedUrlKey: string | null = null;
  private snapshot: ScopeSnapshot = {
    currentId: null,
    turns: EMPTY_TURNS,
    busy: false,
    anyRunning: false,
    runningIds: EMPTY_SET,
    runningChats: EMPTY_CHATS,
  };

  constructor(scope: string) {
    this.scope = scope;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): ScopeSnapshot => this.snapshot;

  private emit(): void {
    const current = this.currentId ? this.threads.get(this.currentId) : undefined;
    const turns = current ? current.turns : EMPTY_TURNS;
    const busy = isTurnBusy(turns[turns.length - 1]);
    const runningIds = new Set<string>();
    const runningChats: RunningChatInfo[] = [];
    for (const rec of this.threads.values()) {
      if (isTurnBusy(rec.turns[rec.turns.length - 1])) {
        runningIds.add(rec.conversationId);
        runningChats.push({
          conversation_id: rec.conversationId,
          title: rec.title,
          started_at: rec.startedAt,
        });
      }
    }
    this.snapshot = {
      currentId: this.currentId,
      turns,
      busy,
      anyRunning: runningIds.size > 0,
      runningIds: runningIds.size > 0 ? runningIds : EMPTY_SET,
      runningChats: runningChats.length > 0 ? runningChats : EMPTY_CHATS,
    };
    for (const fn of [...this.listeners]) fn();
  }

  private rememberCurrent(): void {
    if (this.scope === "ask") saveActiveAskId(this.currentId);
  }

  hasThread(conversationId: string): boolean {
    return this.threads.has(conversationId);
  }

  isRunning(conversationId: string): boolean {
    const rec = this.threads.get(conversationId);
    return !!rec && isTurnBusy(rec.turns[rec.turns.length - 1]);
  }

  claimUrlKey(key: string): boolean {
    if (this.startedUrlKey === key) return false;
    this.startedUrlKey = key;
    return true;
  }

  clearUrlKey(): void {
    this.startedUrlKey = null;
  }

  select(conversationId: string): boolean {
    if (!this.threads.has(conversationId)) return false;
    if (this.currentId !== conversationId) {
      this.currentId = conversationId;
      this.rememberCurrent();
      this.emit();
    }
    return true;
  }

  /** Switch to a fresh empty chat view without aborting any background running chats. */
  reset(): void {
    this.startedUrlKey = null;
    if (this.currentId !== null) {
      this.currentId = null;
      this.rememberCurrent();
      this.emit();
    }
  }

  /** Explicitly cancel the active run in the currently viewed chat (Stop button only). */
  stop(): void {
    if (!this.currentId) return;
    const rec = this.threads.get(this.currentId);
    if (!rec) return;
    const cid = rec.conversationId;
    const ctrl = rec.ctrl;
    rec.ctrl = null;
    rec.runSeq += 1;
    ctrl?.abort();
    const last = rec.turns[rec.turns.length - 1];
    if (last && isTurnBusy(last)) {
      rec.turns = rec.turns.map((t) =>
        t.id === last.id ? { ...t, phase: "error", error: "Stopped. Ask again whenever you're ready." } : t,
      );
      this.emit();
    }
    const stopRun = optionalApi("stopAskRun");
    if (stopRun) {
      void stopRun(cid).catch(() => undefined);
    }
  }

  /** Abort all runs and wipe store (sign-out / session switch / test teardown). */
  clearAll(): void {
    for (const rec of this.threads.values()) {
      rec.ctrl?.abort();
    }
    this.threads.clear();
    this.currentId = null;
    this.startedUrlKey = null;
    this.rememberCurrent();
    this.emit();
  }

  private updateTurn(conversationId: string, turnId: string, fn: (t: Turn) => Turn): void {
    const rec = this.threads.get(conversationId);
    if (!rec) return;
    let nextCid = conversationId;
    rec.turns = rec.turns.map((t) => {
      if (t.id !== turnId) return t;
      const updated = fn(t);
      const serverCid = updated.answer?.conversation_id;
      if (serverCid && /^[A-Za-z0-9_-]{1,80}$/.test(serverCid) && serverCid !== conversationId) {
        nextCid = serverCid;
      }
      return updated;
    });
    if (nextCid !== conversationId) {
      this.threads.delete(conversationId);
      rec.conversationId = nextCid;
      this.threads.set(nextCid, rec);
      if (this.currentId === conversationId) {
        this.currentId = nextCid;
        this.rememberCurrent();
      }
    }
    this.emit();
  }

  private async consume(
    conversationId: string,
    turnId: string,
    seq: number,
    c: AbortController,
    streamFactory: (signal: AbortSignal) => AsyncGenerator<AskEvent>,
    allowReattach: boolean,
  ): Promise<void> {
    let cid = conversationId;
    const isCurrent = () => {
      const rec = this.threads.get(cid);
      return !!rec && rec.runSeq === seq;
    };
    const isDone = () => {
      const rec = this.threads.get(cid);
      const turn = rec?.turns.find((t) => t.id === turnId);
      return !turn || turn.phase === "done";
    };

    const drain = async (gen: AsyncGenerator<AskEvent>): Promise<"done" | "interrupted" | "error"> => {
      for await (const ev of gen) {
        if (!isCurrent()) return "done";
        if (ev.event === "error" && ev.data?.code === "stream_interrupted") {
          return "interrupted";
        }
        if (ev.event === "answer" && ev.data?.conversation_id && /^[A-Za-z0-9_-]{1,80}$/.test(ev.data.conversation_id)) {
          const prevCid = cid;
          cid = ev.data.conversation_id;
          this.updateTurn(prevCid, turnId, (t) => applyEvent(t, ev));
          return "done";
        }
        this.updateTurn(cid, turnId, (t) => applyEvent(t, ev));
        if (ev.event === "error") return "error";
      }
      return isDone() ? "done" : "interrupted";
    };

    let outcome: "done" | "interrupted" | "error" = "interrupted";
    let caught: unknown = null;
    try {
      outcome = await drain(streamFactory(c.signal));
    } catch (e) {
      caught = e;
      outcome = c.signal.aborted ? "error" : "interrupted";
    }

    // Transient drop while server run may still be alive: try reattaching to the server run,
    // then check work_log in case the run finished while disconnected.
    const attachFn = allowReattach ? optionalApi("attachAskRun") : undefined;
    if (outcome === "interrupted" && !c.signal.aborted && isCurrent() && attachFn) {
      for (let attempt = 0; attempt < REATTACH_ATTEMPTS && !c.signal.aborted && isCurrent(); attempt++) {
        await new Promise<void>((resolve) => {
          const t = setTimeout(resolve, REATTACH_DELAY_MS);
          c.signal.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
        });
        if (c.signal.aborted || !isCurrent()) break;
        try {
          outcome = await drain(attachFn(cid, c.signal));
          caught = null;
          if (outcome !== "interrupted") break;
        } catch (e) {
          caught = e;
        }
      }
      const getThreadFn = optionalApi("getWorkThread");
      if (outcome === "interrupted" && !c.signal.aborted && isCurrent() && getThreadFn) {
        try {
          const wt = await getThreadFn(`ask:${cid}`, c.signal);
          const turns = wt.resume?.turns ?? [];
          const rec = this.threads.get(cid);
          const turn = rec?.turns.find((t) => t.id === turnId);
          const matched = turns.length > 0 ? turns[turns.length - 1] : null;
          if (turn && matched?.answer && matched.question === turn.question) {
            this.updateTurn(cid, turnId, (t) => applyEvent(t, { event: "answer", data: matched.answer as unknown as Answer }));
            outcome = "done";
            caught = null;
          }
        } catch {
          // fall through to show the original error
        }
      }
    }

    if (!isCurrent()) return;
    if (outcome !== "done") {
      if (c.signal.aborted) {
        this.updateTurn(cid, turnId, (t) =>
          t.phase === "done" ? t : { ...t, phase: "error", error: "Stopped. Ask again whenever you're ready." },
        );
      } else if (caught !== null) {
        this.updateTurn(cid, turnId, (t) =>
          t.phase === "done" ? t : { ...t, phase: "error", error: plainError(caught), notice: noticeKind(caught) },
        );
      } else if (outcome === "interrupted") {
        this.updateTurn(cid, turnId, (t) =>
          t.phase === "done" ? t : applyEvent(t, { event: "error", data: { code: "stream_interrupted", message: "" } }),
        );
      }
    }
    const rec = this.threads.get(cid);
    if (rec && rec.runSeq === seq) {
      rec.ctrl = null;
      this.emit();
    }
  }

  async run(
    question: string,
    objectId?: string,
    source?: string,
    files: Attachment[] = [],
    extra: Pick<AskRequest, "context" | "mode" | "instruction" | "clarified" | "deep"> = {},
  ): Promise<void> {
    let rec = this.currentId ? this.threads.get(this.currentId) : undefined;
    if (!rec) {
      const cid = newConversationId();
      rec = {
        conversationId: cid,
        title: question.trim().slice(0, 120) || "Untitled",
        startedAt: new Date().toISOString(),
        turns: [],
        ctrl: null,
        runSeq: 0,
      };
      this.threads.set(cid, rec);
      this.currentId = cid;
      this.rememberCurrent();
    }
    const prior = rec.turns;
    rec.ctrl?.abort();
    const c = new AbortController();
    rec.ctrl = c;
    rec.runSeq += 1;
    const seq = rec.runSeq;
    const id = newId();

    // API-009: continuing an instruction sends its first turn's files again (the server
    // keeps none of them); after a reload the server asks for them by name instead
    const from = extra.instruction?.instruction_id;
    if (from && !files.length) files = prior.find((t) => t.answer?.answer_id === from)?.request?.files ?? [];
    const request: TurnRequest = { ...(objectId ? { objectId } : {}), ...(source ? { source } : {}), files, extra };
    const turn: Turn = { id, question, phase: "thinking", streamed: "", request };
    rec.turns = [...prior, turn];
    rec.startedAt = new Date().toISOString();
    const cid = rec.conversationId;
    this.emit();

    const conversation_id = conversationOf(prior) ?? cid;
    await this.consume(
      cid,
      id,
      seq,
      c,
      (signal) =>
        apiClient.ask(
          {
            question,
            history: historyOf(prior),
            conversation_id,
            ...(objectId ? { object_id: objectId } : {}),
            ...(source ? { source: source.slice(0, 4096) } : {}),
            ...(files.length ? { attachments: files.map((f) => ({ name: f.name, upload: f.upload })) } : {}),
            ...extra,
          },
          signal,
        ),
      this.scope === "ask",
    );
  }

  seed(
    prior: { question: string; answer: Answer | null; withheld?: string | null }[],
    opts?: { conversationId?: string; activeRun?: AskActiveRun | null },
  ): void {
    const restored: Turn[] = prior.map((t) =>
      t.answer
        ? { id: newId(), question: t.question, phase: "done", streamed: t.answer.direct_answer, answer: t.answer }
        : { id: newId(), question: t.question, phase: "error", streamed: "", error: t.withheld ?? "Ask again to see this answer." },
    );
    const cid = opts?.conversationId ?? opts?.activeRun?.conversation_id ?? conversationOf(restored) ?? newConversationId();
    const existing = this.threads.get(cid);
    // Never clobber a conversation that is already actively streaming in this tab.
    if (existing && isTurnBusy(existing.turns[existing.turns.length - 1])) {
      this.currentId = cid;
      this.rememberCurrent();
      this.emit();
      return;
    }

    const active = opts?.activeRun ?? null;
    let activeTurnId: string | null = null;
    if (active) {
      activeTurnId = newId();
      restored.push({
        id: activeTurnId,
        question: active.question,
        phase: active.streamed ? "streaming" : "thinking",
        status: active.status ?? undefined,
        steps: active.steps && active.steps.length > 0 ? active.steps.slice(-MAX_STEPS) : undefined,
        streamed: active.streamed ?? "",
        simple: !!active.simple,
      });
    }

    const title = (restored[0]?.question || active?.question || "Untitled").trim().slice(0, 120);
    const rec: ThreadRecord = {
      conversationId: cid,
      title,
      startedAt: active?.started_at ?? new Date().toISOString(),
      turns: restored,
      ctrl: null,
      runSeq: (existing?.runSeq ?? 0) + 1,
    };
    this.threads.set(cid, rec);
    this.currentId = cid;
    this.rememberCurrent();

    const attachFn = optionalApi("attachAskRun");
    if (active && activeTurnId && attachFn) {
      const c = new AbortController();
      rec.ctrl = c;
      const seq = rec.runSeq;
      this.emit();
      void this.consume(cid, activeTurnId, seq, c, (signal) => attachFn(cid, signal), true);
      return;
    }
    this.emit();
  }
}

const stores = new Map<string, ThreadStore>();
function storeFor(scope = "ask"): ThreadStore {
  let s = stores.get(scope);
  if (!s) {
    s = new ThreadStore(scope);
    stores.set(scope, s);
  }
  return s;
}

onSessionReset(() => {
  for (const s of stores.values()) s.clearAll();
});

/** Test-only helper to reset the module-level Ask stores between unit tests. */
export function resetThreadStoreForTests(): void {
  for (const s of stores.values()) s.clearAll();
  stores.clear();
}

/** Reactive summary of Ask conversations running in the background (for nav + chat rail pulsing dots). */
export function useAskRunning(): {
  anyRunning: boolean;
  runningIds: ReadonlySet<string>;
  runningChats: readonly RunningChatInfo[];
  currentId: string | null;
} {
  const store = storeFor("ask");
  const snap = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  return {
    anyRunning: snap.anyRunning,
    runningIds: snap.runningIds,
    runningChats: snap.runningChats,
    currentId: snap.currentId,
  };
}

export function useThread(scope = "ask") {
  const store = storeFor(scope);
  const snap = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);

  /** Ask; `objectId` scopes the question to one record (a pick, or an object page),
   *  `source` to one document (a mail, attachment or file the reader has open); `extra`
   *  carries the page's context and an instruction's continuation (the one box's "do"). */
  const run = useCallback(
    (question: string, objectId?: string, source?: string, files: Attachment[] = [], extra: Pick<AskRequest, "context" | "mode" | "instruction" | "clarified" | "deep"> = {}) =>
      store.run(question, objectId, source, files, extra),
    [store],
  );

  /** Send a turn's request again exactly as it was (its scope, page context and attachments). */
  const retry = useCallback(
    (t: Turn) => store.run(t.question, t.request?.objectId, t.request?.source, t.request?.files ?? [], t.request?.extra ?? {}),
    [store],
  );

  /** The same question again, thought through (AskRequest.deep): its scope, page context and files as it was asked. */
  const deeper = useCallback(
    (t: Turn) => store.run(t.question, t.request?.objectId, t.request?.source, t.request?.files ?? [], { ...(t.request?.extra ?? {}), deep: true }),
    [store],
  );

  /** A suggested follow-up: asked in the same scope as the turn it follows (not its files). */
  const followUp = useCallback(
    (next: string, from: Turn) => {
      // a new question: never the reply to a question asked back
      const extra = { ...(from.request?.extra ?? {}) };
      delete extra.clarified;
      return store.run(next, from.request?.objectId, from.request?.source, [], extra);
    },
    [store],
  );

  const stop = useCallback(() => store.stop(), [store]);
  const reset = useCallback(() => store.reset(), [store]);
  const select = useCallback((conversationId: string) => store.select(conversationId), [store]);
  const claimUrlKey = useCallback((key: string) => store.claimUrlKey(key), [store]);

  /** Reopen a session's earlier turns as they were answered (a "continue" from the work log);
   *  a turn whose source is no longer open to you shows why instead of its answer. */
  const seed = useCallback(
    (
      prior: { question: string; answer: Answer | null; withheld?: string | null }[],
      opts?: { conversationId?: string; activeRun?: AskActiveRun | null },
    ) => store.seed(prior, opts),
    [store],
  );

  return {
    turns: snap.turns,
    run,
    retry,
    deeper,
    followUp,
    stop,
    reset,
    busy: snap.busy,
    seed,
    select,
    claimUrlKey,
    conversationId: snap.currentId,
    runningIds: snap.runningIds,
  };
}
