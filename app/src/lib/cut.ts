/**
 * The one place a text is cut for display, and the note that says so (guardrail 1: no silent
 * limit). A bare `.slice(0, N)`, `.substring(0, N)` or `line-clamp-N` in app/src fails
 * tests/test_guardrails.py; cut with this and show `note`, or render a list with `ShowAll`.
 */
export type Cut = { text: string; cut: boolean; total: number; leftOut: number; note: string };

export function cutText(text: string, limit: number, say = ""): Cut {
  const total = text.length;
  if (total <= limit) return { text, cut: false, total, leftOut: 0, note: "" };
  const leftOut = total - limit;
  const why = say ? ` (${say})` : "";
  const note = `${limit.toLocaleString("en-US")} of ${total.toLocaleString("en-US")} characters shown${why}; not shown: ${leftOut.toLocaleString("en-US")}`;
  return { text: text.slice(0, limit), cut: true, total, leftOut, note };
}
