"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Turn } from "@/components/answer-card";
import { ask } from "./api";
import type { Attachment } from "./attachments";
import { noticeKind, plainError } from "./errors";
import type { Answer, AskEvent, HistoryTurn } from "./types";

/** The progress steps a turn keeps (the latest). */
export const MAX_STEPS = 8;

/** Pure: fold one stream event into a turn. */
export function applyEvent(turn: Turn, ev: AskEvent): Turn {
  switch (ev.event) {
    case "understanding":
      return { ...turn, understanding: ev.data };
    case "status": {
      // each plain-words step once, in the order the work did them
      const said = typeof ev.data?.message === "string" ? ev.data.message : "";
      const steps = turn.steps ?? [];
      return { ...turn, status: said || turn.status, steps: said && steps[steps.length - 1] !== said ? [...steps, said].slice(-MAX_STEPS) : steps };
    }
    case "verdict":
      return typeof ev.data?.text === "string" && ev.data.text.trim() ? { ...turn, verdict: ev.data.text.trim() } : turn;
    case "sources":
      return { ...turn, sourcesFound: Array.isArray(ev.data.sources) ? ev.data.sources.length : turn.sourcesFound };
    case "token":
      return { ...turn, phase: "streaming", streamed: turn.streamed + (typeof ev.data?.text === "string" ? ev.data.text : "") };
    case "answer":
      return { ...turn, phase: "done", answer: ev.data, status: undefined };
    case "error":
      return { ...turn, phase: "error", error: plainError(ev.data), notice: noticeKind(ev.data) };
    default:
      return turn;
  }
}

/** API limits (operator_api AskRequest): at most 12 history turns of 4,000 characters. */
export const MAX_HISTORY = 12;
const MAX_TEXT = 4000;

/** Prior turns sent as context so follow-ups ("and the other one?") resolve. */
export function historyOf(turns: Turn[]): HistoryTurn[] {
  const out: HistoryTurn[] = [];
  for (const t of turns) {
    out.push({ role: "user", text: t.question.slice(0, MAX_TEXT) });
    if (t.phase === "done" && t.answer) out.push({ role: "assistant", text: t.answer.direct_answer.slice(0, MAX_TEXT) });
  }
  return out.slice(-MAX_HISTORY);
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

export function useThread() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const turnsRef = useRef<Turn[]>([]);
  const ctrl = useRef<AbortController | null>(null);
  const last = turns[turns.length - 1];
  const busy = !!last && (last.phase === "thinking" || last.phase === "streaming");

  useEffect(() => {
    turnsRef.current = turns;
  }, [turns]);
  // Leaving the page stops the answer. React's development double mount runs this cleanup
  // and mounts again at once: only a cleanup not followed by a mount aborts.
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const c = ctrl.current;
      setTimeout(() => {
        if (!mounted.current) c?.abort();
      }, 0);
    };
  }, []);

  const update = useCallback((id: string, fn: (t: Turn) => Turn) => {
    setTurns((ts) => ts.map((t) => (t.id === id ? fn(t) : t)));
  }, []);

  /** Ask; `objectId` scopes the question to one record (a pick, or an object page),
   *  `source` to one document (a mail, attachment or file the reader has open). */
  const run = useCallback(
    async (question: string, objectId?: string, source?: string, files: Attachment[] = []) => {
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      const id = newId();
      const prior = turnsRef.current;
      const turn: Turn = { id, question, phase: "thinking", streamed: "" };
      turnsRef.current = [...prior, turn];
      setTurns((ts) => [...ts, turn]);
      try {
        const conversation_id = conversationOf(prior);
        for await (const ev of ask(
          {
            question: question.slice(0, 1000),
            history: historyOf(prior),
            ...(conversation_id ? { conversation_id } : {}),
            ...(objectId ? { object_id: objectId } : {}),
            ...(source ? { source: source.slice(0, 4096) } : {}),
            ...(files.length ? { attachments: files.map((f) => ({ name: f.name, data: f.data })) } : {}),
          },
          c.signal,
        )) {
          update(id, (t) => applyEvent(t, ev));
        }
      } catch (e) {
        if (c.signal.aborted) {
          update(id, (t) => (t.phase === "done" ? t : { ...t, phase: "error", error: "Stopped. Ask again whenever you're ready." }));
        } else {
          update(id, (t) => ({ ...t, phase: "error", error: plainError(e), notice: noticeKind(e) }));
        }
      }
    },
    [update],
  );

  const stop = useCallback(() => ctrl.current?.abort(), []);

  const reset = useCallback(() => {
    ctrl.current?.abort();
    turnsRef.current = [];
    setTurns([]);
  }, []);

  /** Reopen a session's earlier turns as they were answered (a "continue" from the work log);
   *  a turn whose source is no longer open to you shows why instead of its answer. */
  const seed = useCallback((prior: { question: string; answer: Answer | null; withheld?: string | null }[]) => {
    ctrl.current?.abort();
    const restored: Turn[] = prior.map((t) =>
      t.answer
        ? { id: newId(), question: t.question, phase: "done", streamed: t.answer.direct_answer, answer: t.answer }
        : { id: newId(), question: t.question, phase: "error", streamed: "", error: t.withheld ?? "Ask again to see this answer." },
    );
    turnsRef.current = restored;
    setTurns(restored);
  }, []);

  return { turns, run, stop, reset, busy, seed };
}
