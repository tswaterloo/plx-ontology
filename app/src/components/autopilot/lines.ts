// A card says each thing once: the summary, the quoted message and the next step often read
// the same sentence. A line is left out when an earlier one already says it (the same words,
// or all of it inside an earlier line); a longer line that holds an earlier one adds to it and stays.

const words = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

export function saidOnce(lines: string[]): string[] {
  const seen: string[] = [];
  const out: string[] = [];
  for (const line of lines) {
    const w = words(line);
    if (!w) {
      out.push("");
      continue;
    }
    if (seen.some((s) => s === w || s.includes(w))) {
      out.push("");
      continue;
    }
    seen.push(w);
    out.push(line);
  }
  return out;
}

const SCOPE_OWNER = /(?:^|\s+)(This is on [^.\n]+?'s list\s*,\s*not yours\.?(?:\s*Only you can see this\.?)?)\s*$/i;
const SCOPE_ONLY_YOU = /(?:^|\s+)(Only you can see this\.?)\s*$/i;

function normalizeScope(raw: string): string {
  const s = raw
    .trim()
    .replace(/,\s*not yours/gi, ", not yours")
    .replace(/not yours\.\s*Only you can see this\.?/gi, "not yours. Only you can see this.")
    .replace(/\s{2,}/g, " ");
  return s && !/[.!?]$/.test(s) ? `${s}.` : s;
}

/** A card's note: its own words, and the quiet label lines the server put after them.
 * Also repairs legacy missing spaces ("list,not yours.Only you can see this.") and separates
 * inline privacy-scope sentences from body text. */
export function noteParts(note: string): { text: string; labels: string[] } {
  const rawLines = (note ?? "").split("\n").map((l) => l.trim());
  let body = (rawLines[0] ?? "")
    .replace(/,\s*not yours/gi, ", not yours")
    .replace(/not yours\.\s*Only you can see this\.?/gi, "not yours. Only you can see this.");
  const extracted: string[] = [];

  const ownerMatch = SCOPE_OWNER.exec(body);
  if (ownerMatch) {
    extracted.push(normalizeScope(ownerMatch[1]));
    body = body.slice(0, ownerMatch.index).trim();
  } else {
    const onlyMatch = SCOPE_ONLY_YOU.exec(body);
    if (onlyMatch) {
      extracted.push(normalizeScope(onlyMatch[1]));
      body = body.slice(0, onlyMatch.index).trim();
    }
  }

  const tailLabels = rawLines.slice(1).filter(Boolean).map(normalizeScope);
  const labels = [...new Set([...extracted, ...tailLabels])];
  return { text: body, labels };
}
