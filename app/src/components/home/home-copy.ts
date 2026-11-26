import { noteParts } from "@/components/autopilot/lines";
import { formatDate, readable } from "@/lib/format";
import type { HomeNeedItem } from "@/lib/types";

export const TZ = "America/Toronto";

export const KIND_ORDER = ["reply", "approve", "question", "late_po", "alert", "task", "waiting", "routine", "gap", "feedback"];
export const SOURCE_ORDER = ["mail", "teams", "pos", "alerts", "planner", "mc", "gaps", "feedback", "system"];
export const DUE_ORDER = ["overdue", "today", "next7", "later", "undated"];

// guard-ok: display labels for typed kind values; no meaning is guessed from these words.
export const KIND_WORDS: Record<string, string> = {
  reply: "Reply",
  approve: "Approve",
  question: "Question",
  task: "Task",
  waiting: "Waiting",
  alert: "Alert",
  late_po: "Late PO",
  routine: "Routine",
  feedback: "Feedback",
  gap: "Gap",
};

// guard-ok: display labels for typed source values; no meaning is guessed from these words.
export const SOURCE_WORDS: Record<string, string> = {
  mail: "Mail",
  teams: "Teams",
  mc: "Mission Control",
  planner: "Planner",
  alerts: "Alerts",
  pos: "POs",
  feedback: "Feedback",
  gaps: "Gaps",
  system: "System",
};

// guard-ok: display labels for typed due-date buckets; no meaning is guessed from these words.
export const DUE_WORDS: Record<string, string> = {
  overdue: "Overdue",
  today: "Due today",
  next7: "Next 7 days",
  later: "Later",
  undated: "No date",
};

export const FINANCIALS_EVENTS = new Set([
  "price_below_cost",
  "customer_price_drop",
  "customer_demand_drop",
  "price_below_typical",
]);

export const HOLD_EVENTS = new Set([
  "lot_on_hold",
  "quality_hold_aging",
  "qc_hold",
  "batch_hold",
  "order_hold",
]);

// guard-ok: display badges for typed hold event values; no meaning is guessed from these words.
export const HOLD_BADGES: Record<string, string> = {
  qc_hold: "QC hold",
  lot_on_hold: "Batch hold",
  batch_hold: "Batch hold",
  order_hold: "Order hold",
  quality_hold_aging: "Quality hold",
};

export const STILL_OPEN_LEAD = /^Still open\?\s*Nothing has happened on it since it was due\s+([^.\n]+?)\.?\s*/i;
export const ONLY_YOU_RE = /^Only you can see this\.?$/i;

const NO_REPLY_HISTORY_PAREN = /\s*\((?:no typical reply time on record yet|nothing in your mail says how long[^)]*)\)/gi;
const NO_REPLY_HISTORY_STANDALONE = /^(?:no typical reply time on record yet|nothing in your mail says how long[^.\n]*)\.?$/i;
const BARE_DATE_HOLD_STUB_RE = /^(?:\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{4})?\.\s*)?(?:qc hold or fail|quality hold|qc hold|batch hold|order hold)\.?\s*$/i;
const MC_TASK_STUB_RE = /^Mission Control task\s+[A-Z0-9_-]+\.?\s*$/i;
const WAIT_TITLE_RE = /^No answer yet from\s+(.+)$/i;
const WAITING_ON_TITLE_RE = /^Waiting on\s+([^:]+):\s*(.+)$/i;
const COMMITMENT_PREFIX_RE = /^You said\s*:\s*/i;
const BEFORE_DATE_TAIL_RE = /\s+(?:before|by)\s+(?:[A-Z][a-z]{2,8}\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}\s+[A-Z][a-z]{2,8}(?:\s+\d{4})?|\d{4}-\d{2}-\d{2})\.?$/i;
const HOLD_PREFIX_RE = /^(?:QC hold or fail|Quality hold|QC hold|Batch hold|Order hold)\s*:\s*/i;
const HOLD_LOT_STATUS_RE = /^(Lot\s+[A-Za-z0-9-]+)(?:\s*\(([^)]+)\))?\s+is\s+(rejected|failed|on[- ]hold)(?:\s*;\s*(?:it\s+holds\s+up\s+)?(\d+)\s+open\s+orders?(?:\(s\))?(?:\s+affected)?)?\.?$/i;
const HOLD_ON_HOLD_ORDERS_RE = /\s+is\s+(rejected|failed|on[- ]hold)\s*;\s*(?:it\s+holds\s+up\s+)?(\d+)\s+open\s+orders?(?:\(s\))?(?:\s+affected)?\.?$/i;
const HOLD_ON_HOLD_END_RE = /\s+is\s+(rejected|failed|on[- ]hold)\.?$/i;
const HOLD_LEAD_SENTENCE_RE = /^Lot\s+\S+(?:\s*\([^)]+\))?\s+(?:is\s+)?(?:rejected|failed|on[- ]hold)(?:\s*[;·]\s*(?:it\s+holds\s+up\s+)?\d+\s+open\s+orders?(?:\(s\))?(?:\s+affected)?)?\.?\s*/i;
const TITLE_LEAD_STRIP_RE = /^(?:(?:re|fw|fwd)\s*:\s*)+|^(?:you said|waiting on [^:]+|no answer yet from [^:]+|qc hold or fail|quality hold|qc hold|batch hold|order hold)\s*:\s*/i;
const COLLAPSED_FACT_RE = /^\+(\d+)\s+updates?\s+collapsed$/i;
const CODE_TOKEN_RE = /\b[a-z]*\d+[a-z0-9-]*\b/g;

// guard-ok: English grammatical stop words used only for Jaccard similarity deduplication, never for semantic routing.
const DEDUPE_STOPWORDS = new Set([
  "the", "a", "an", "to", "for", "of", "on", "in", "is", "are", "was", "were",
  "and", "or", "with", "from", "by", "at", "it", "this", "that", "be", "has", "have",
]);

// guard-ok: English pronouns and reporting verbs stripped only when collapsing same-second promise/conversation duplicates.
const EVENT_DEDUPE_STOPWORDS = new Set([
  ...DEDUPE_STOPWORDS,
  "he", "she", "they", "him", "her", "his", "their", "them", "you", "your", "we", "our",
  "will", "would", "can", "could", "should", "may", "might", "had", "been", "being",
  "says", "said", "trying", "try", "going", "about", "into", "still", "also", "just",
]);

const SPEAKER_LEAD_RE = /^(?:you\s+said\s*:\s*|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:says|said|has|will|committed\s+to|promised\s+to|is\s+trying\s+to)\s+(?:(?:he|she|they)\s+(?:is|was|are|will)\s+)?(?:trying\s+to\s+)?)/i;
const SAME_EVENT_WINDOW_MS = 120_000;

export function sourceLabelOf(item: Pick<HomeNeedItem, "source" | "source_label">): string {
  if (item.source === "mc" || item.source_label === "MC") return "Mission Control";
  return item.source_label || SOURCE_WORDS[item.source] || item.source;
}

export function isOpsSystemNotice(item: HomeNeedItem): boolean {
  const title = (item.title ?? "").trim();
  const combined = `${title} ${item.line ?? ""}`;
  if (/^(?:Workbench API health|API health|System health|Staging health|Health check|Daily (?:Workbench API )?health (?:check|alert)|Automated(?:\s+\w+)*\s+alert:)\b/i.test(title)) {
    return true;
  }
  if (/\b(?:email-relay job failed|(?:UAT (?:BC|Business Central)|Portal-to-BC) (?:scheduled )?sync has not (?:run|refreshed)|(?:unavailable )?Workbench API(?: staging)? relations|PR merge[- ]class|merge[- ]class gate)\b/i.test(combined)) {
    return true;
  }
  if (item.source === "mail" && /:\s*ALERT\s*$/i.test(title) && /health|runbook|relations/i.test(combined)) {
    return true;
  }
  return false;
}

export function isFinancialsItem(item: HomeNeedItem): boolean {
  return item.section === "financials" || FINANCIALS_EVENTS.has(item.event_type ?? "");
}

export function isHoldItem(item: HomeNeedItem): boolean {
  return (
    item.section === "holds" ||
    item.alert_kind === "quality_hold" ||
    HOLD_EVENTS.has(item.event_type ?? "") ||
    HOLD_EVENTS.has(item.alert_kind ?? "")
  );
}

export function isCommitmentItem(item: HomeNeedItem): boolean {
  const title = (item.title ?? "").trim();
  const act = (item.action_label ?? "").toLowerCase();
  return COMMITMENT_PREFIX_RE.test(title) || act === "commitment" || act === "your commitment";
}

export function itemBadgeOf(item: HomeNeedItem): string | null {
  if (item.badge) return item.badge;
  if (item.event_type && HOLD_BADGES[item.event_type]) return HOLD_BADGES[item.event_type];
  if (item.event_type === "customer_price_drop") return "Price drop";
  if (item.event_type === "customer_demand_drop") return "Demand drop";
  if (item.event_type === "price_below_cost") return "Below cost";
  if (item.event_type === "price_below_typical") return "Below typical price";
  if (isHoldItem(item)) return "Quality hold";
  return null;
}

export function financialsSummaryLabel(items: HomeNeedItem[]): string {
  let priceDrops = 0;
  let demandDrops = 0;
  let belowCost = 0;
  let belowTypical = 0;
  let other = 0;
  for (const it of items) {
    if (it.event_type === "customer_price_drop") priceDrops += 1;
    else if (it.event_type === "customer_demand_drop") demandDrops += 1;
    else if (it.event_type === "price_below_cost") belowCost += 1;
    else if (it.event_type === "price_below_typical") belowTypical += 1;
    else other += 1;
  }
  const parts: string[] = [];
  if (priceDrops > 0) parts.push(`${priceDrops} ${priceDrops === 1 ? "price drop" : "price drops"}`);
  if (demandDrops > 0) parts.push(`${demandDrops} ${demandDrops === 1 ? "demand drop" : "demand drops"}`);
  if (belowCost > 0) parts.push(`${belowCost} below cost`);
  if (belowTypical > 0) parts.push(`${belowTypical} below typical`);
  if (other > 0) parts.push(`${other} other`);
  return parts.length > 0 ? `Financials · ${parts.join(", ")}` : `Financials (${items.length})`;
}

export function dayOf(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleDateString("en-CA", { timeZone: TZ });
}

export function dueValue(item: HomeNeedItem): string {
  const dueDay = dayOf(item.due_at);
  if (!dueDay) return "undated";
  const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
  const days = Math.round(
    (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${dueDay}T12:00:00Z`).getTime()) / 86_400_000,
  );
  if (days > 0) return "overdue";
  if (days === 0) return "today";
  if (days >= -7) return "next7";
  return "later";
}

export function timingMeta(
  item: HomeNeedItem,
  extractedDue?: string | null,
): { badge: string; overdue: boolean; dueShort: string | null; ageShort: string | null } {
  const today = new Date().toLocaleDateString("en-CA", { timeZone: TZ });
  const dueDay = dayOf(item.due_at);
  if (dueDay) {
    const days = Math.round(
      (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${dueDay}T12:00:00Z`).getTime()) / 86_400_000,
    );
    const dueShort = formatDate(item.due_at, { short: true });
    if (days > 0) return { badge: `${days}d overdue`, overdue: true, dueShort, ageShort: null };
    if (days === 0) return { badge: "Due today", overdue: false, dueShort, ageShort: null };
    return { badge: `Due ${dueShort}`, overdue: false, dueShort, ageShort: null };
  }
  if (item.days_late != null && item.days_late > 0) {
    return { badge: `${item.days_late}d overdue`, overdue: true, dueShort: extractedDue ?? null, ageShort: null };
  }
  const basis = dayOf(item.basis_at);
  if (!basis) return { badge: "", overdue: false, dueShort: extractedDue ?? null, ageShort: null };
  const days = Math.max(
    0,
    Math.round(
      (new Date(`${today}T12:00:00Z`).getTime() - new Date(`${basis}T12:00:00Z`).getTime()) / 86_400_000,
    ),
  );
  if (days === 0) return { badge: "Today", overdue: false, dueShort: extractedDue ?? null, ageShort: "Today" };
  if (days === 1) return { badge: "Yesterday", overdue: false, dueShort: extractedDue ?? null, ageShort: "1d" };
  return { badge: `${days}d old`, overdue: false, dueShort: extractedDue ?? null, ageShort: `${days}d` };
}

function normWords(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

function sentenceCase(text: string): string {
  const t = text.trim();
  if (!t || !/^[a-z]/.test(t)) return t;
  const first = t.split(" ", 1)[0];
  if (first.includes("@") || first.includes(".")) return t;
  return t[0].toUpperCase() + t.slice(1);
}

function stripNoHistory(text: string): string {
  const cleaned = text.replace(NO_REPLY_HISTORY_PAREN, "").replace(/\s{2,}/g, " ").trim().replace(/[;,\s]+$/, "");
  if (!cleaned || NO_REPLY_HISTORY_STANDALONE.test(cleaned) || BARE_DATE_HOLD_STUB_RE.test(cleaned)) return "";
  return cleaned;
}

function stripPersonEcho(text: string, person: string | null | undefined): string {
  if (!text || !person) return text;
  const p = person.trim();
  if (!p) return text;
  const first = p.split(/\s+/)[0];
  const candidates = first && first !== p ? [p, first] : [p];
  for (const name of candidates) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const rx = new RegExp(`^${escaped}\\s*:\\s*`, "i");
    if (rx.test(text)) {
      return text.replace(rx, "").trim();
    }
  }
  return text;
}

function formatHoldTitleAndImpact(raw: string): { title: string; impact: string | null } {
  let t = raw.replace(HOLD_PREFIX_RE, "").trim();
  const lotMatch = HOLD_LOT_STATUS_RE.exec(t);
  if (lotMatch) {
    const lotPart = lotMatch[1].trim();
    const productPart = (lotMatch[2] ?? "").trim();
    const rawStatus = lotMatch[3].toLowerCase().replace("-", " ");
    const statusWord = rawStatus === "rejected" || rawStatus === "failed" ? "rejected" : "on hold";
    let impact: string | null = null;
    if (lotMatch[4]) {
      const count = Number(lotMatch[4]);
      impact = count === 1 ? "1 open order affected" : `${count} open orders affected`;
    }
    const headline = productPart ? `${lotPart} ${statusWord} (${productPart})` : `${lotPart} ${statusWord}`;
    return { title: headline.replace(/\u2014/g, " - ").replace(/\s{2,}/g, " ").trim(), impact };
  }
  let impact: string | null = null;
  const m = HOLD_ON_HOLD_ORDERS_RE.exec(t);
  if (m) {
    const rawStatus = m[1].toLowerCase().replace("-", " ");
    const statusWord = rawStatus === "rejected" || rawStatus === "failed" ? "rejected" : "on hold";
    const count = Number(m[2]);
    impact = count === 1 ? "1 open order affected" : `${count} open orders affected`;
    t = `${t.slice(0, m.index).trim()} ${statusWord}`;
  } else {
    const endMatch = HOLD_ON_HOLD_END_RE.exec(t);
    if (endMatch) {
      const rawStatus = endMatch[1].toLowerCase().replace("-", " ");
      const statusWord = rawStatus === "rejected" || rawStatus === "failed" ? "rejected" : "on hold";
      t = `${t.slice(0, endMatch.index).trim()} ${statusWord}`;
    }
  }
  return { title: t.replace(/\u2014/g, " - ").replace(/\s{2,}/g, " ").trim(), impact };
}

function clipTitleTopic(text: string, maxLen = 120): string {
  const clean = text.replace(/\.$/, "").trim();
  if (clean.length <= maxLen) return clean;
  // guard-ok: display title clipping for verbose waiting topics; full text stays in inline detail.
  const cut = clean.slice(0, maxLen).trimEnd();
  const space = cut.lastIndexOf(" ");
  const bounded = space >= Math.floor(maxLen / 2) ? cut.slice(0, space) : cut;
  return `${bounded.replace(/[ ,;:-]+$/, "")}...`;
}

export function cleanBodyText(raw: string): { text: string; extractedDue: string | null } {
  const readableText = readable(raw, { shortDates: true }).replace(/\u2014/g, " - ").trim();
  if (!readableText) return { text: "", extractedDue: null };
  let extractedDue: string | null = null;
  let body = readableText;
  const m = STILL_OPEN_LEAD.exec(body);
  if (m) {
    extractedDue = m[1].trim();
    body = body.slice(m[0].length).trim();
  }
  body = stripNoHistory(body);
  if (!body || MC_TASK_STUB_RE.test(body)) return { text: "", extractedDue };
  body = sentenceCase(body);
  return { text: /[.!?:)]$/.test(body) ? body : `${body}.`, extractedDue };
}

export interface CleanedRowCopy {
  title: string;
  subtitle: string;
  extractedDue: string | null;
  person: string | null;
  isPrivate: boolean;
  otherLabels: string[];
  actionLabel: string | null;
  waitingLabel: string | null;
  holdImpact: string | null;
  backendCollapsedCount: number;
}

/** Produce crystal-clear, scannable, non-repetitive headline and metadata copy for a Home row. */
export function cleanNeedCopy(item: HomeNeedItem): CleanedRowCopy {
  const parts = noteParts(item.line ?? "");
  const cleaned = cleanBodyText(parts.text);
  const isPrivate = parts.labels.some((l) => ONLY_YOU_RE.test(l.trim()));
  const otherLabels = parts.labels.filter((l) => !ONLY_YOU_RE.test(l.trim()));

  let title = readable(item.title, { shortDates: true }).replace(/\u2014/g, " - ").trim();
  let person = item.person ?? null;
  let subtitle = cleaned.text;
  let actionLabel = item.action_label || null;
  let waitingLabel: string | null = null;
  let holdImpact: string | null = null;

  // Check if backend collapsed duplicate updates into facts.
  let backendCollapsedCount = 0;
  for (const fact of item.facts ?? []) {
    const cm = COLLAPSED_FACT_RE.exec(fact.trim());
    if (cm) backendCollapsedCount += Number(cm[1]);
  }

  // 1. Clean QC / batch / order hold titles: "Lot BC-25295D rejected (AA-NA650)" + "1 open order affected".
  if (isHoldItem(item) || HOLD_PREFIX_RE.test(title)) {
    const formatted = formatHoldTitleAndImpact(title);
    title = formatted.title;
    holdImpact = formatted.impact;
    if (BARE_DATE_HOLD_STUB_RE.test(subtitle)) {
      subtitle = "";
    } else if (subtitle) {
      subtitle = subtitle.replace(HOLD_PREFIX_RE, "").replace(HOLD_LEAD_SENTENCE_RE, "").trim();
    }
  }

  // 2. Clean commitment items ("You said: renew the workbench-host-vince Portal access before Dec 14, 2026").
  if (isCommitmentItem(item)) {
    actionLabel = "Commitment";
    if (COMMITMENT_PREFIX_RE.test(title)) {
      let stripped = title.replace(COMMITMENT_PREFIX_RE, "").trim();
      if (item.due_at) {
        stripped = stripped.replace(BEFORE_DATE_TAIL_RE, "").trim();
      }
      stripped = stripped.replace(/^([A-Za-z]+)\s+the\s+(?=[a-z0-9]+-[a-z0-9-]+)/i, "$1 ");
      if (stripped) title = sentenceCase(stripped.replace(/\.$/, ""));
    }
  }

  // 3. Clean waiting items: promote real topic to headline, put "Waiting on <Person>" in metadata/pill.
  const waitMatch = WAIT_TITLE_RE.exec(title);
  const waitingOnMatch = !waitMatch ? WAITING_ON_TITLE_RE.exec(title) : null;
  if (waitMatch) {
    const whoRaw = waitMatch[1].trim().replace(/[.:]+$/, "");
    const whoDisplay = whoRaw.toLowerCase() === "them" ? (person || "") : whoRaw;
    if (!person && whoDisplay) person = whoDisplay;
    waitingLabel = whoDisplay ? `Waiting on ${whoDisplay}` : "Waiting on reply";
    const topic = stripPersonEcho(subtitle, person || whoDisplay).replace(/\.$/, "").trim();
    if (topic) {
      title = sentenceCase(clipTitleTopic(topic, 120));
      subtitle = topic.length <= 120 ? "" : topic;
    } else {
      title = whoDisplay ? `Follow up with ${whoDisplay}` : "Waiting on reply";
      subtitle = "";
    }
  } else if (waitingOnMatch) {
    const whoDisplay = waitingOnMatch[1].trim();
    if (!person && whoDisplay) person = whoDisplay;
    waitingLabel = `Waiting on ${whoDisplay}`;
    title = sentenceCase(waitingOnMatch[2].trim().replace(/\.$/, ""));
  } else if (item.kind === "waiting") {
    waitingLabel =
      actionLabel && /^Waiting on /i.test(actionLabel)
        ? actionLabel
        : person
          ? `Waiting on ${person}`
          : "Waiting on reply";
  }

  // Never repeat "Ricardo:" right after "Ricardo ·".
  subtitle = stripPersonEcho(subtitle, person);

  // Deduplicate subtitle whenever it overlaps or echoes the headline.
  if (subtitle) {
    const cmpSubtitle = isCommitmentItem(item) ? subtitle.replace(BEFORE_DATE_TAIL_RE, "").trim() : subtitle;
    const normTitleFull = normWords(title);
    const normTitleBody = normWords(title.replace(TITLE_LEAD_STRIP_RE, ""));
    const normSub = normWords(cmpSubtitle);
    const tokTitle = topicTokens(normTitleBody || normTitleFull);
    const tokSub = topicTokens(normSub);
    const allSubInTitle = tokSub.size > 0 && tokTitle.size > 0 && [...tokSub].every((w) => tokTitle.has(w));
    if (
      !normSub ||
      MC_TASK_STUB_RE.test(subtitle) ||
      normSub === normTitleFull ||
      normSub === normTitleBody ||
      allSubInTitle ||
      (normTitleBody.length >= 12 && (normTitleFull.includes(normSub) || normSub.includes(normTitleBody)))
    ) {
      subtitle = "";
    }
  }

  if (holdImpact && subtitle && subtitle.toLowerCase().includes(holdImpact.toLowerCase())) {
    holdImpact = null;
  }

  return {
    title,
    subtitle,
    extractedDue: cleaned.extractedDue,
    person,
    isPrivate,
    otherLabels,
    actionLabel,
    waitingLabel,
    holdImpact,
    backendCollapsedCount,
  };
}

function itemSpecificHref(item: HomeNeedItem): string | null {
  const href = (item.href ?? "").trim();
  if (href.startsWith("/assist?item=") || href.startsWith("/feedback#") || href.startsWith("/source?")) {
    return href;
  }
  return null;
}

function coreTopicFingerprint(item: HomeNeedItem): string {
  const copy = cleanNeedCopy(item);
  let t = copy.title.trim();
  while (true) {
    const next = t.replace(TITLE_LEAD_STRIP_RE, "").trim();
    if (next === t) break;
    t = next;
  }
  return normWords(t);
}

function topicTokens(core: string): Set<string> {
  return new Set(core.split(" ").filter((w) => w.length >= 2 && !DEDUPE_STOPWORDS.has(w)));
}

function codeTokens(core: string): Set<string> {
  const found = core.match(CODE_TOKEN_RE);
  return new Set(found ?? []);
}

function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

function stemToken(w: string): string {
  if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss") && !w.endsWith("us") && !w.endsWith("is")) {
    return w.slice(0, -1);
  }
  return w;
}

function eventTokens(text: string): Set<string> {
  let s = text.trim();
  while (true) {
    const next = s.replace(TITLE_LEAD_STRIP_RE, "").trim().replace(SPEAKER_LEAD_RE, "").trim();
    if (next === s) break;
    s = next;
  }
  const out = new Set<string>();
  for (const w of normWords(s).split(" ")) {
    if (w.length >= 2 && !EVENT_DEDUPE_STOPWORDS.has(w)) {
      out.add(stemToken(w));
    }
  }
  return out;
}

function sameEventOverlap(textA: string, textB: string): boolean {
  const codesA = codeTokens(normWords(textA));
  const codesB = codeTokens(normWords(textB));
  if (codesA.size > 0 && codesB.size > 0) {
    let sharesCode = false;
    for (const c of codesA) if (codesB.has(c)) sharesCode = true;
    if (!sharesCode) return false;
  }
  const tokA = eventTokens(textA);
  const tokB = eventTokens(textB);
  if (tokA.size < 2 || tokB.size < 2) return false;
  let inter = 0;
  for (const w of tokA) if (tokB.has(w)) inter += 1;
  if (inter < 2) return false;
  const union = tokA.size + tokB.size - inter;
  const jaccard = union > 0 ? inter / union : 0;
  const containment = inter / Math.min(tokA.size, tokB.size);
  return jaccard >= 0.35 || containment >= 0.7;
}

function isCrossKindSameEvent(a: HomeNeedItem, b: HomeNeedItem): boolean {
  if (a.object_id && b.object_id && a.object_id !== b.object_id) return false;
  const pairKinds =
    (a.kind === "task" && b.kind === "reply") ||
    (a.kind === "reply" && b.kind === "task") ||
    (a.kind === "task" && b.kind === "task" && (isCommitmentItem(a) || isCommitmentItem(b)));
  if (!pairKinds) return false;
  const tsA = a.basis_at ? new Date(a.basis_at).getTime() : Number.NaN;
  const tsB = b.basis_at ? new Date(b.basis_at).getTime() : Number.NaN;
  if (Number.isNaN(tsA) || Number.isNaN(tsB) || Math.abs(tsA - tsB) > SAME_EVENT_WINDOW_MS) return false;
  if (sameEventOverlap(a.title ?? "", b.title ?? "")) return true;
  const textA = `${a.title ?? ""} ${a.line ?? ""}`.trim();
  const textB = `${b.title ?? ""} ${b.line ?? ""}`.trim();
  return sameEventOverlap(textA, textB);
}

function actionabilityScore(item: HomeNeedItem): number {
  const ts = item.basis_at ? new Date(item.basis_at).getTime() || 0 : 0;
  const planned = item.plan_status === "drafted" || item.plan_status === "planned";
  return (
    (item.draft_ready ? 1e16 : 0) +
    (planned ? 1e15 : 0) +
    ((item.facts?.length ?? 0) > 0 ? 1e14 : 0) +
    (item.ask ? 1e13 : 0) +
    (item.object_id ? 1e12 : 0) +
    (!isCommitmentItem(item) ? 1e11 : 0) +
    ts
  );
}

function areDuplicateItems(a: HomeNeedItem, b: HomeNeedItem): boolean {
  if (a.id === b.id) return true;
  if (a.matter_id && b.matter_id && a.matter_id === b.matter_id) return true;
  const hrefA = itemSpecificHref(a);
  const hrefB = itemSpecificHref(b);
  if (hrefA && hrefB && hrefA === hrefB) return true;

  const bothHolds = isHoldItem(a) && isHoldItem(b);
  if (a.object_id && b.object_id && a.object_id === b.object_id) {
    if (bothHolds) return true;
    if (a.kind === b.kind && a.kind === "late_po") return true;
    if (a.kind === b.kind && a.kind === "alert" && (a.event_type ?? "") === (b.event_type ?? "")) return true;
  }

  if (isCrossKindSameEvent(a, b)) return true;

  if (a.kind !== b.kind && !bothHolds) return false;
  if (a.object_id && b.object_id && a.object_id !== b.object_id) return false;

  const cpA = normWords(a.person ?? "");
  const cpB = normWords(b.person ?? "");
  if (cpA || cpB) {
    if (cpA !== cpB) return false;
  } else if (a.source !== b.source) {
    return false;
  }

  const coreA = coreTopicFingerprint(a);
  const coreB = coreTopicFingerprint(b);
  if (!coreA || !coreB) return false;
  if (coreA === coreB) return true;

  const codesA = codeTokens(coreA);
  const codesB = codeTokens(coreB);
  if ((codesA.size > 0 || codesB.size > 0) && !setsEqual(codesA, codesB)) return false;

  // guard-ok: 48-char prefix comparison for duplicate notification fingerprinting, never displayed.
  if (coreA.length >= 12 && coreB.length >= 12 && coreA.slice(0, 48) === coreB.slice(0, 48)) return true;

  const tokA = topicTokens(coreA);
  const tokB = topicTokens(coreB);
  if (tokA.size >= 2 && tokB.size >= 2) {
    let inter = 0;
    for (const w of tokA) if (tokB.has(w)) inter += 1;
    const union = tokA.size + tokB.size - inter;
    if (union > 0 && inter / union >= 0.65) return true;
  }
  return false;
}

export interface DedupedNeed {
  item: HomeNeedItem;
  mergedCount: number;
}

/** Defensive frontend deduplication mirroring backend rules so duplicate notifications never stack. */
export function dedupeNeedItems(items: HomeNeedItem[]): DedupedNeed[] {
  const clusters: { item: HomeNeedItem; extraCount: number }[] = [];
  for (const row of items) {
    const idx = clusters.findIndex((c) => areDuplicateItems(c.item, row));
    if (idx === -1) {
      clusters.push({ item: row, extraCount: 0 });
    } else {
      const cur = clusters[idx];
      if (actionabilityScore(row) > actionabilityScore(cur.item)) {
        cur.item = row;
      }
      cur.extraCount += 1;
    }
  }
  return clusters.map(({ item, extraCount }) => ({
    item,
    mergedCount: extraCount + cleanNeedCopy(item).backendCollapsedCount,
  }));
}

export type BentoBucketId = "replies" | "waiting" | "alerts" | "tasks" | "late_pos";

export function bentoBucketOf(item: HomeNeedItem): BentoBucketId {
  if (item.kind === "late_po") return "late_pos";
  if (isHoldItem(item) || item.kind === "alert") return "alerts";
  if (item.kind === "reply" || item.kind === "question" || item.kind === "approve") return "replies";
  if (item.kind === "waiting") return "waiting";
  return "tasks";
}

export type HomeSectionId = "replies" | "waiting" | "tasks" | "holds_alerts";

export function isLateSupplyItem(item: HomeNeedItem): boolean {
  return item.kind === "late_po";
}

export function isDecisionOrHoldItem(item: HomeNeedItem): boolean {
  return isHoldItem(item) || item.kind === "alert";
}

export function sectionOf(item: HomeNeedItem): HomeSectionId {
  if (isHoldItem(item) || item.kind === "alert" || item.kind === "late_po") return "holds_alerts";
  if (item.kind === "reply" || item.kind === "question" || item.kind === "approve") return "replies";
  if (item.kind === "waiting") return "waiting";
  return "tasks";
}
