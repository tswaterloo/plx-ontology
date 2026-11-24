"use client";

import { useEffect } from "react";
import { onPageReadsChange, pageReadsOpen } from "./api";

// W6: `document.body.dataset.ready = "1"` says the page is complete for the person: every
// section it shows has its data drawn. It is set only while all of these hold, and cleared the
// moment one stops holding (a new page, a new read, a placeholder drawn):
//   - no read the page itself asked for is in flight (read-aheads, sent with X-PLX-Ahead, do not count);
//   - no waiting placeholder (`Pending`, a section skeleton) is mounted (`useWaiting`);
//   - no page-declared section is still unread (`usePageReady(false)`).
// The harness's `ready_ms` and the idle reads of `afterReady` both wait for it.
let waiting = 0;
const waitListeners = new Set<() => void>();
function bump(n: number) {
  waiting += n;
  for (const f of [...waitListeners]) f();
}

/** While `active`, this component is a waiting placeholder: the page is not ready. */
export function useWaiting(active = true): void {
  useEffect(() => {
    if (!active) return;
    bump(1);
    return () => bump(-1);
  }, [active]);
}

/** The page's own statement: it is complete (`done`) or a section is still unread. Pages whose
 * sections draw no placeholder declare it; it only adds to the checks above. */
export function usePageReady(done: boolean): void {
  useWaiting(!done);
}

/** Mounted once in the shell: keeps the flag true to the three checks. A change is checked
 * twice, a task apart, so the read a render's effect starts is counted before ready is said. */
export function watchReady(): () => void {
  if (typeof document === "undefined") return () => undefined;
  let t: ReturnType<typeof setTimeout> | undefined;
  let gen = 0;
  const settled = () => waiting === 0 && pageReadsOpen() === 0;
  const check = () => {
    if (!settled()) {
      delete document.body.dataset.ready;
      gen += 1;
      clearTimeout(t);
      return;
    }
    const mine = ++gen;
    clearTimeout(t);
    t = setTimeout(() => {
      if (mine !== gen || !settled()) return;
      t = setTimeout(() => {
        if (mine === gen && settled()) document.body.dataset.ready = "1";
      }, 0);
    }, 0);
  };
  waitListeners.add(check);
  const off = onPageReadsChange(check);
  check();
  return () => {
    waitListeners.delete(check);
    off();
    clearTimeout(t);
    delete document.body.dataset.ready;
  };
}

/** A new page: not ready until its own placeholders and reads have come and gone. */
export function resetReady(): void {
  if (typeof document !== "undefined") delete document.body.dataset.ready;
  for (const f of [...waitListeners]) f();
}

/** Runs `run` once the page is ready (`usePageReady`), when the browser is idle: for reads the
 * page's own content does not need (W3, W5; they are sent with `X-PLX-Ahead: 1`). Returns the
 * cancel. A page that is already ready runs it on the next idle moment. */
export function afterReady(run: () => void): () => void {
  if (typeof document === "undefined") return () => undefined;
  let done = false;
  let obs: MutationObserver | null = null;
  const idle = (window as Window & { requestIdleCallback?: (f: () => void) => number; cancelIdleCallback?: (n: number) => void });
  let handle: number | null = null;
  const go = () => {
    if (done) return;
    obs?.disconnect();
    obs = null;
    const fire = () => {
      if (done) return;
      done = true;
      run();
    };
    if (idle.requestIdleCallback) handle = idle.requestIdleCallback(fire);
    else fire();
  };
  if (document.body.dataset.ready === "1") go();
  else {
    obs = new MutationObserver(() => {
      if (document.body.dataset.ready === "1") go();
    });
    obs.observe(document.body, { attributes: true, attributeFilter: ["data-ready"] });
  }
  return () => {
    done = true;
    obs?.disconnect();
    if (handle !== null) idle.cancelIdleCallback?.(handle);
  };
}
