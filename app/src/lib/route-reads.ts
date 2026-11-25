import { readPath } from "./api";
import { afterReady } from "./ready";
import { EARLY_ROUTE_READS } from "./ticket-boot";
import { readAhead } from "./use-read";

// One route table: `EARLY_ROUTE_READS` (ticket-boot.ts) names each route's main read; the HTML's
// early read, this read-ahead and the idle warm-up all use it. A read-ahead only helps a page that
// draws from the kept reads (`useRead`), so the pages that do are listed in `STORED_ROUTES` and a
// test holds each to its table entry. Reads are looked up when called, not when this module loads.

/** Routes whose page draws its main read from the kept reads (`useRead`, keyed by that URL). */
export const STORED_ROUTES: readonly string[] = ["/assist", "/ops", "/data-health", "/activity", "/templates", "/meetings", "/team", "/orders", "/customers", "/suppliers", "/products", "/more"];

/** The path of `href` without its query or fragment. */
const routeOf = (href: string): string => href.split(/[?#]/)[0];

/** Read the page at `href` ahead, when it draws from the kept reads. */
export function readAheadRoute(href: string): void {
  const route = routeOf(href);
  const url = EARLY_ROUTE_READS[route];
  if (url && STORED_ROUTES.includes(route)) readAhead(url, (s) => readPath(url, s));
}

/** How long a pointer rests on a link before its page is read ahead (a pass-over costs nothing). */
export const HOVER_MS = 150;

const hovering = new Map<string, ReturnType<typeof setTimeout>>();

/** Event props for a link to `href`: read ahead on pointer-down at once, after HOVER_MS of hover,
 * and on keyboard focus. Spread them on the `Link`. */
export function aheadProps(href: string) {
  // the timer is held by the href, not the render: enter and leave may come from different renders
  const stop = () => {
    clearTimeout(hovering.get(href));
    hovering.delete(href);
  };
  return {
    onPointerDown: () => { stop(); readAheadRoute(href); },
    onPointerEnter: () => { stop(); hovering.set(href, setTimeout(() => { hovering.delete(href); readAheadRoute(href); }, HOVER_MS)); },
    onPointerLeave: stop,
    onFocus: () => readAheadRoute(href),
  };
}

/** After the page is ready and the browser idle, read the stored pages' reads, four at a time at most
 * (the first is already kept after a visit; a second one is a no-op). Returns the cancel. */
export function warmStoredRoutes(): () => void {
  let off: (() => void) | undefined;
  const cancel = afterReady(() => {
    let i = 0;
    // only the pages in this person's own menu (the server leaves the others out): a refused route is never asked
    const mine = STORED_ROUTES.filter((r) => document.querySelector(`a[href="${r}"]`) !== null);
    const next = () => {
      if (i >= mine.length) return;
      const batch = mine.slice(i, i + 4);
      i += 4;
      batch.forEach(readAheadRoute);
      off = afterReady(next);
    };
    next();
  });
  return () => {
    cancel();
    off?.();
  };
}
