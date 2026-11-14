import { getAutopilot, getMyWork, getOpsAlarms, getRdBoard, getTrust } from "./api";
import { readAhead } from "./use-read";

// A page's main read by its link, so hovering or focusing the link reads it ahead: the page
// then paints its data on arrival. Keys match the pages' own `useRead` keys. The
// reads are looked up when called, not when this module loads.
const READS: Record<string, [string, (signal: AbortSignal) => Promise<unknown>]> = {
  "/my-work": ["my-work", (s) => getMyWork(s)],
  "/autopilot": ["autopilot", (s) => getAutopilot(s)],
  "/ops": ["ops", (s) => getOpsAlarms(s)],
  "/trust": ["trust", (s) => getTrust(s)],
  "/rd-projects": ["rd-projects", (s) => getRdBoard(s)],
};

/** Read the page at `href` ahead, when it has a main read to read. */
export function readAheadRoute(href: string): void {
  const r = READS[href];
  if (r) readAhead(r[0], r[1]);
}
