"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Why answers are revealed here at all: the model service sends a reply written after a
 * tool call (every Ask answer: it reads first) in one piece once the whole reply is
 * written, so the words reach the browser together however promptly every hop passes them
 * on (measured: the first byte of such a reply comes when the reply is complete; a reply
 * with no tool call before it streams word by word). The answer is then shown from the
 * top at a reading pace that never takes longer than REVEAL_MS, so it reads as it is
 * written instead of appearing all at once, and a long answer is never held back long.
 */
export const REVEAL_MS = 1200;
/** The slowest pace (characters per millisecond): a short answer is still written out briskly. */
export const MIN_RATE = 0.6;
/** Growth this small (a few words) is shown at once: nothing to write out. */
export const SMALL_STEP = 24;

/** Pure: how many leading characters ``a`` and ``b`` share. */
export function sharedPrefix(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a.charCodeAt(i) === b.charCodeAt(i)) i++;
  return i;
}

/**
 * Pure: where to cut ``text`` to show about ``at`` characters of it: at the end of a word
 * (never half a word or half a figure), and at the end of a table row when the cut falls
 * in one (a row is shown whole, never as a stray "| 2 | So" line). Never past the end.
 */
export function revealCut(text: string, at: number): number {
  if (at >= text.length) return text.length;
  if (at <= 0) return 0;
  const lineStart = text.lastIndexOf("\n", at - 1) + 1;
  if (text.slice(lineStart).trimStart().startsWith("|")) {
    const end = text.indexOf("\n", at);
    return end < 0 ? text.length : end;
  }
  const space = text.slice(at).search(/\s/);
  return space < 0 ? text.length : at + space;
}

function reducedMotion(): boolean {
  try {
    return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/**
 * The part of ``text`` to show now. ``live``: the answer is being written while this is on
 * screen, so it starts from nothing; otherwise (an answer opened from a saved chat) the text
 * is shown whole at once. Later text that grows (or replaces) what is shown is written out
 * from where the two agree; reduced motion shows every text at once.
 */
export function useReveal(text: string, live: boolean): string {
  const [shown, setShown] = useState(() => (live && !reducedMotion() ? "" : text));
  const shownRef = useRef(shown);
  useEffect(() => {
    const show = (next: string) => {
      shownRef.current = next;
      setShown(next);
    };
    const from = sharedPrefix(shownRef.current, text);
    if (text.length - from <= SMALL_STEP || reducedMotion() || typeof requestAnimationFrame === "undefined") {
      show(text);
      return;
    }
    // words shown that the new text does not have go at once
    if (shownRef.current.length > from) show(text.slice(0, from));
    const rate = Math.max(MIN_RATE, (text.length - from) / REVEAL_MS);
    const started = performance.now();
    let frame = requestAnimationFrame(function step(now: number) {
      const cut = revealCut(text, from + Math.ceil((now - started) * rate));
      show(text.slice(0, cut));
      if (cut < text.length) frame = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(frame);
  }, [text]);
  return shown;
}
