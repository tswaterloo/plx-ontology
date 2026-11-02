// A follow-up draft's "Open in Outlook" link, built here in the browser: a mailto: URL that
// opens the person's own mail client with the text they edited. Nothing is sent by the app.

const ADDRESS = /^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;
/** Mail clients cut long mailto: URLs; past this the body is shortened and says so. */
export const MAILTO_MAX = 1900;

/** "mailto:a@b.example?subject=...&body=..."; an address that is not one plain address is left out. */
export function mailtoHref(to: string | null | undefined, subject: string, body: string): string {
  const addr = (to ?? "").trim();
  const head = `mailto:${ADDRESS.test(addr) ? encodeURIComponent(addr).replace(/%40/g, "@") : ""}`;
  const query = (b: string) => `?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(b)}`;
  if (head.length + query(body).length <= MAILTO_MAX) return head + query(body);
  const note = "\n[shortened: copy the full text from the Workbench]";
  let text = body;
  while (text.length > 0 && head.length + query(text + note).length > MAILTO_MAX) {
    text = text.slice(0, Math.floor(text.length * 0.9));
  }
  text = text.trimEnd() + note;
  return head + query(text);
}

/** The whole draft as text for the clipboard. */
export function draftText(to: string | null | undefined, subject: string, body: string): string {
  return [(to ?? "").trim() && `To: ${(to ?? "").trim()}`, `Subject: ${subject}`, "", body].filter((l, i) => i > 0 || l).join("\n");
}
