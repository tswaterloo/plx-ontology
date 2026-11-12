// A follow-up draft's "Open in Outlook" link, built here in the browser: a mailto: URL that
// opens the person's own mail client with the text they edited. Nothing is sent by the app.

const ADDRESS = /^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;
/** Mail clients cut long mailto: URLs; past this the body is shortened and says so. */
export const MAILTO_MAX = 1900;

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;
/** A string that always encodes: a lone half of an emoji becomes U+FFFD. */
const wellFormed = (s: string) => s.replace(LONE_SURROGATE, "\uFFFD");
/** The first `n` characters (whole code points: an emoji is never cut in half). */
const first = (s: string, n: number) => Array.from(s).slice(0, n).join("");
const length = (s: string) => Array.from(s).length;

/**
 * "mailto:a@b.example?subject=...&body=..."; an address that is not one plain address is left
 * out. The whole link is held to MAILTO_MAX: the body is shortened (with a note), then the
 * subject, always on a character boundary, so it never throws and never exceeds the bound.
 */
export function mailtoHref(to: string | null | undefined, subject: string, body: string): string {
  const addr = (to ?? "").trim();
  const head = `mailto:${ADDRESS.test(addr) ? encodeURIComponent(addr).replace(/%40/g, "@") : ""}`;
  let subj = wellFormed(subject);
  const text0 = wellFormed(body);
  const query = (sj: string, b: string) => `?subject=${encodeURIComponent(sj)}&body=${encodeURIComponent(b)}`;
  const fits = (sj: string, b: string) => head.length + query(sj, b).length <= MAILTO_MAX;
  if (fits(subj, text0)) return head + query(subj, text0);
  const note = "\n[shortened: copy the full text from the Workbench]";
  // room for the note: a subject that alone fills the link is shortened first
  while (length(subj) > 0 && !fits(subj, note)) subj = first(subj, Math.floor(length(subj) * 0.9));
  let text = text0;
  while (length(text) > 0 && !fits(subj, text + note)) text = first(text, Math.floor(length(text) * 0.9));
  return head + query(subj, text.trimEnd() + note);
}

/** The whole draft as text for the clipboard. */
export function draftText(to: string | null | undefined, subject: string, body: string): string {
  return [(to ?? "").trim() && `To: ${(to ?? "").trim()}`, `Subject: ${subject}`, "", body].filter((l, i) => i > 0 || l).join("\n");
}
