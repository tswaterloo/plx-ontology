"use client";

import { ChevronRight, EyeOff } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { fieldLabel, formatDate, formatDateTime, formatMoney, plural, readable, SYSTEM_LABEL, withoutFloatNoise } from "@/lib/format";
import type { Conflict, Fact, Freshness, Hidden, NotFoundItem, Source } from "@/lib/types";
import { EvidenceDrawer, SourceChip, SourceChips, sourceName } from "./source-chip";
import { mailField } from "./source-view";
import { DoneRing, OpenRing, StatusGlyph, SystemCode } from "./ui/glyphs";

const MONEY_LABEL = /\b(?:total|amount|cost|price|balance|value|revenue|margin|invoice)\b/i;
const BARE_NUMBER = /^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$/;
const GENERIC_LABEL = /^(?:body text|body|text|content|snippet|passage|body preview|message|quote)$/i;

/** "Label: value" -> [label, value] when the start reads as a field label, else null. */
export function splitLabel(text: string): [string, string] | null {
  const m = /^([A-Za-z][\w ()/.&'-]{0,48}?):\s+([\s\S]+)$/.exec(text.trim());
  if (!m) return null;
  if (/[.!?]\s/.test(m[1])) return null;
  return [m[1].trim(), m[2].trim()];
}

/** A field label as a reader says it: no "(BC)", no "fm", no doubled words. */
export function cleanLabel(label: string): string {
  return label
    .replace(/\s*\((?:BC|Portal|Sage|FileMaker|FM)\)\s*$/i, "")
    .replace(/\bfm\b\s*/gi, "")
    .replace(/^(\w+)\s+\1\b/i, "$1")
    .replace(/^Erp code$/i, "Code")
    .replace(/\s+/g, " ")
    .trim();
}

/** Where a quoted passage comes from, in words ("Email from ross@…", "The record"). */
function said(s: Source | undefined): string {
  if (!s) return "The source";
  if (s.system === "Email") {
    const from = mailField(s.fields, "from");
    return from ? `Email from ${from}` : "The email";
  }
  if (s.system === "Teams") return "The Teams message";
  if (s.system === "File") return "The document";
  return "The record";
}

/**
 * A fact as a reader reads it: "(BC)" and plumbing words out of its label, a bare total with
 * its currency, a passage labelled "Body text" said as what it is ("Email from X: “…”").
 */
export function factText(f: Fact, currency?: string | null): string {
  const text = readable(f.text);
  const pair = splitLabel(text);
  if (!pair) return text;
  const [rawLabel, value] = pair;
  const label = cleanLabel(rawLabel);
  if (GENERIC_LABEL.test(label)) return `${said(f.sources[0])}: “${value.replace(/^“|”$/g, "")}”`;
  if (currency && MONEY_LABEL.test(label) && BARE_NUMBER.test(value)) return `${label}: ${formatMoney(value.replace(/,/g, ""), currency)}`;
  return `${label}: ${value}`;
}

const CURRENCY_FACT = /^Currency(?:\s*\([^)]*\))?:\s*([A-Za-z]{3})\s*$/;
const CURRENCY_FIELD = /(?:^|\.)(?:currency|currencyCode|currency_code)$/i;

/** The one currency these sources' own fields state; null when none or they disagree. */
function sourcesCurrency(sources: Source[]): string | null {
  const codes = new Set<string>();
  for (const s of sources) {
    for (const [k, v] of Object.entries(s.fields ?? {})) {
      if (CURRENCY_FIELD.test(k) && typeof v === "string" && /^[A-Za-z]{3}$/.test(v.trim())) codes.add(v.trim().toUpperCase());
    }
  }
  return codes.size === 1 ? [...codes][0] : null;
}

/**
 * The currency of one fact's amount, from its own record only (WEB-028: a CAD and a USD vendor
 * in one answer): a currency field of the sources it cites, else a "Currency: X" fact citing the
 * same record. Two currencies, or none, is null: the amount is then shown as written, never in
 * another fact's currency.
 */
export function factCurrency(f: Fact, facts: Fact[] | undefined): string | null {
  if (f.sources.length === 0) return null;
  const own = sourcesCurrency(f.sources);
  if (own) return own;
  if (f.sources.some((s) => Object.keys(s.fields ?? {}).some((k) => CURRENCY_FIELD.test(k)))) return null;
  const handles = new Set(f.sources.map((s) => s.handle).filter(Boolean));
  const codes = new Set<string>();
  for (const other of facts ?? []) {
    const m = CURRENCY_FACT.exec(other.text.trim());
    if (m && other.sources.length > 0 && other.sources.every((s) => !!s.handle && handles.has(s.handle))) codes.add(m[1].toUpperCase());
  }
  return codes.size === 1 ? [...codes][0] : null;
}

const UNCONFIRMED = new Set(["lead", "text_match", "not_established", "unchecked"]);

/** Whether a fact is not confirmed against its record (it says so in words). */
export function unconfirmed(f: Fact): boolean {
  return !f.check || UNCONFIRMED.has(f.check);
}

/** One source line for a block: "From Business Central · PO 106093"; it opens the drawer. */
export function SourceLine({ source, record, prefix = "From" }: { source: Source; record?: string | null; prefix?: string }) {
  const [open, setOpen] = useState(false);
  const sys = SYSTEM_LABEL[source.system] ?? source.system;
  const name = sourceName(source, record);
  const tail = name.startsWith(`${source.system} · `) ? name.slice(source.system.length + 3) : name === sys ? "" : name;
  const text = [`${prefix} ${sys}`, tail, source.as_of ? `read ${formatDate(source.as_of, { short: true })}` : null].filter(Boolean).join(", ");
  const opens = !!source.handle || !!source.open_url;
  return (
    // a span (shown as a line): it also stands inside a sentence's paragraph
    <span className="flex items-center gap-2 text-meta text-muted" data-testid="source-line">
      <SystemCode system={source.system} size={16} />
      {opens ? (
        <button type="button" onClick={() => setOpen(true)} className="min-w-0 truncate text-left underline-offset-2 hover:text-fg hover:underline cursor-pointer">
          {text}
        </button>
      ) : (
        text
      )}
      {open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
    </span>
  );
}

/**
 * The facts under an answer or on a record: 2-6 lines, each with at most two chips named by
 * the record. When every fact cites the same one source, the chips go and one source line
 * stands under the list. Unconfirmed facts say so in words; confirmed ones carry no tick.
 */
export function FactList({
  facts,
  record,
  extra = [],
  max,
}: {
  facts: Fact[] | undefined;
  /** The reader's name for a source's record (the page's own record, the answer's one subject). */
  record?: (s: Source) => string | null | undefined;
  /** Extra plain lines (an answer's later paragraphs), shown first. */
  extra?: React.ReactNode[];
  max?: number;
}) {
  const [all, setAll] = useState(false);
  const list = facts ?? [];
  if (list.length === 0 && extra.length === 0) return null;
  const handles = new Set(list.flatMap((f) => f.sources.map((s) => s.handle)));
  const oneSource = list.length > 1 && handles.size === 1 && list.every((f) => f.sources.length > 0) ? list[0].sources[0] : null;
  const shown = max && !all ? list.slice(0, Math.max(0, max - extra.length)) : list;
  return (
    <div className="space-y-3">
      <ul className="space-y-2" aria-label="Supporting facts" data-testid="fact-list">
        {extra.map((x, i) => (
          <li key={`x${i}`} className="flex gap-3 text-[13px] leading-5">
            <span className="mt-[9.5px] h-px w-2.5 shrink-0 bg-[color-mix(in_srgb,var(--fg)_35%,transparent)]" aria-hidden />
            <span className="min-w-0 break-words">{x}</span>
          </li>
        ))}
        {shown.map((f, i) => (
          <li key={i} className="flex gap-3 text-[13px] leading-5">
            <span className="mt-[9.5px] h-px w-2.5 shrink-0 bg-[color-mix(in_srgb,var(--fg)_35%,transparent)]" aria-hidden />
            <span className="min-w-0 break-words">
              {factText(f, factCurrency(f, list))}
              {unconfirmed(f) && <span className="text-muted"> (not confirmed)</span>}{" "}
              {!oneSource && <SourceChips sources={f.sources} record={record} />}
            </span>
          </li>
        ))}
      </ul>
      {max && list.length + extra.length > max && (
        <button type="button" onClick={() => setAll((v) => !v)} className="-mx-2 inline-flex h-7 items-center rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer" aria-expanded={all}>
          {all ? "Show fewer" : `Show all ${list.length + extra.length}`}
        </button>
      )}
      {oneSource && <SourceLine source={oneSource} record={record?.(oneSource)} />}
    </div>
  );
}

/**
 * A missing item as a task, the owner last: "Supplier confirmation: not found yet; mail and
 * SharePoint aren't fully searched. Ask Purchasing." On the record's own page its name is
 * dropped from the item ("… of Purchase order 106001").
 */
/** The server's plumbing words for a gap, said as a reader would. */
const GAP_WORDS: [RegExp, string][] = [
  [/^a value the question asks for$/i, "Part of what you asked"],
  [/^(?:projection|store|index|mirror|readings?)$/i, "Some records"],
];
const WHERE_WORDS: [RegExp, string][] = [
  [/^conflicting sources$/i, "the sources disagree on it"],
  [/^schema drift$/i, "saved in an older format; the Workbench team can refresh them"],
  [/^source stale$/i, "the source is older than the question needs"],
];

export function missingText(n: NotFoundItem, subject?: string | null): string {
  let what = readable(n.what).replace(/\s*[.]$/, "");
  for (const [re, w] of GAP_WORDS) if (re.test(what)) what = w;
  const whereRaw = readable(n.where_it_would_live).replace(/\s*[.]$/, "");
  // a source read too long ago: "Portal data may be out of date", never "the Portal. Stale source."
  if (/^(?:stale source|source stale)$/i.test(whereRaw) && !/^Some records$/.test(what))
    return `${what.replace(/^the\s+/i, "").replace(/^./, (c) => c.toUpperCase())} data is out of date.`;
  if (/^schema drift$/i.test(whereRaw) && what === "Some records") return "Some records are in an older format and may be incomplete. The Workbench team can refresh them.";
  for (const [re, w] of WHERE_WORDS) if (re.test(whereRaw)) return `${what}: ${w}.`;
  if (subject) {
    const esc = subject.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    what = what.replace(new RegExp(`\\s+of\\s+(?:[A-Z][a-z]+(?:\\s[a-z]+)?\\s+)?${esc}$`), "");
  }
  const where = whereRaw;
  if (!where) return `${what}.`;
  const m = /^Ask ([^.]+)\.\s*(?:[^:.;]+:\s*)?(.*)$/.exec(where);
  if (!m) return `${what}. ${where[0].toUpperCase()}${where.slice(1)}.`;
  const rest = m[2]
    .replace(/;?\s*not fully searched: none named/i, "")
    .replace(/not fully searched: (.+)$/i, (_x, list: string) => `${list.replace(/, ([^,]+)$/, " and $1")} aren't fully searched`)
    .replace(/,? as of [^;]+;/i, ";")
    .replace(/searched: (.+)$/i, "searched $1")
    .trim();
  return `${what}${rest ? `: ${rest[0].toLowerCase()}${rest.slice(1)}` : ""}. Ask ${m[1].trim()}.`;
}

const sentenceCase = (t: string) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t);

export function NotFoundSection({ items, title = "Not found", bare = false, subject }: { items: NotFoundItem[] | undefined; title?: string; bare?: boolean; subject?: string | null }) {
  if (!items || items.length === 0) return null;
  return (
    <section data-testid="not-found">
      {!bare && <h3 className="mb-3 text-[12px] leading-4 font-medium text-muted">{title}</h3>}
      <ul className="space-y-2 text-[13px] leading-5">
        {items.map((n, i) => (
          <li key={i} className="flex gap-3">
            <OpenRing size={14} tone="neutral" className="mt-[3px]" />
            <span className="min-w-0">{sentenceCase(missingText(n, subject))}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * Hidden-item notice. Composed here from the count and owner/team names only:
 * no other field (titles, snippets, senders, even the server's own message) is
 * read. Absent hidden -> nothing (never "0 items"). count null -> "some items
 * may be hidden" (no number may be given). The unnamed private bucket (other
 * people's mail, chats and files) is counted but is nobody to ask.
 */
export function HiddenNotice({ hidden }: { hidden: Hidden | null | undefined }) {
  if (!hidden || typeof hidden !== "object") return null;
  const raw = hidden.count;
  const unknown = raw === null;
  const count = typeof raw === "number" && Number.isFinite(raw) ? Math.max(0, Math.trunc(raw)) : 0;
  if (!unknown && count === 0) return null;
  const buckets = (hidden.by_owner ?? []).filter(
    (o) => o && typeof o.owner_or_team === "string" && o.owner_or_team && typeof o.count === "number" && o.count > 0,
  );
  const owners = buckets.filter((o) => o.named !== false).map((o) => ({ owner: o.owner_or_team, count: Math.trunc(o.count) }));
  const privateCount = buckets.filter((o) => o.named === false).reduce((n, o) => n + Math.trunc(o.count), 0);
  const askWho =
    owners.length === 0
      ? null
      : owners.length === 1
        ? owners[0].owner
        : `${owners
            .slice(0, -1)
            .map((o) => o.owner)
            .join(", ")} or ${owners[owners.length - 1].owner}`;
  const them = unknown || count !== 1 ? "them" : "it";
  return (
    <section
      role="note"
      aria-label="Items you don't have access to"
      className="flex gap-3 rounded-xl bg-surface px-4 py-3 text-[13px] leading-5"
      data-testid="hidden-notice"
    >
      <EyeOff className="mt-[3px] size-[14px] shrink-0 text-muted" strokeWidth={2} aria-hidden />
      <div className="min-w-0 space-y-0.5">
        <p className="font-medium text-fg">
          {unknown
            ? "Some items may be hidden from you because you don't have access."
            : `${plural(count, "more item")} match${count === 1 ? "es" : ""}, but you don't have access.`}
        </p>
        <p className="text-[12px] leading-[18px] text-muted">
          {askWho
            ? `Ask ${askWho} if you need ${them}.`
            : privateCount > 0 && privateCount === count
              ? `${count === 1 ? "It is" : "They are"} in other people's private mail, chats or files.`
              : `Ask the owner of that information if you need ${them}.`}
          {askWho && privateCount > 0 && (
            <span className="block pt-1">
              {plural(privateCount, "item")} {privateCount === 1 ? "is" : "are"} in other people's private mail, chats or files.
            </span>
          )}
          {owners.length > 1 && (
            <span className="block pt-1">{owners.map((o) => `${o.owner}: ${o.count}`).join(" · ")}</span>
          )}
        </p>
      </div>
    </section>
  );
}

/** The field a conflict is about, in words ("currency", "PO status"). */
export function conflictField(c: Conflict): string {
  return fieldLabel(c.field)
    .toLowerCase()
    .replace(/\b(po|so|ao|eta|uom|gl|sku|id)\b/g, (w) => w.toUpperCase());
}

/** A system's name for one conflicting value: "Portal customer", "AR record", "Business Central". */
function valueSystem(s: Source): string {
  const name = sourceName(s);
  return name.includes(" · ") ? name.replace(" · ", " ").replace(/^BC /, "Business Central ").replace(/^Portal (AR record)$/, "$1") : SYSTEM_LABEL[s.system] ?? s.system;
}

/** "Currency: Portal customer says CAD; AR record says USD (used)." */
export function conflictSentence(c: Conflict): string {
  const field = conflictField(c);
  const parts = c.values.map((v) => `${valueSystem(v.source)} says ${withoutFloatNoise(v.value)}${v.trusted && c.values.filter((x) => x.trusted).length === 1 ? " (used)" : ""}`);
  return `${field[0].toUpperCase()}${field.slice(1)}: ${parts.join("; ")}.`;
}

/** A bare amount in a money field reads "25,113.60", never "25113.600"; anything else as given. */
export function conflictValue(c: Conflict, value: string): string {
  const v = withoutFloatNoise(value);
  // a date reads as a date, like everywhere else in the answer ("17 Oct 2026", not "2026-10-17")
  if (/^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$/.test(v.trim())) return readable(v.trim());
  if (!/amount|total|price|cost|value/i.test(c.field) || !/^-?\d+(?:\.\d+)?$/.test(v.trim())) return v;
  return Number(v).toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * Systems that disagree, in suspect violet (a data problem, not a business one): one row per
 * system with its value and source, the value we use marked "Used", and plain words when we
 * are not picking one.
 */
export function ConflictCard({ conflict, owner }: { conflict: Conflict; owner?: string | null }) {
  const trusted = conflict.values.filter((v) => v.trusted);
  const field = conflictField(conflict);
  const what = conflict.subject ? `${field} (${conflict.subject})` : field;
  const used = trusted.length === 1 ? trusted[0] : null;
  return (
    <section aria-label={`Conflicting values for ${what}`} className="rounded-xl bg-surface px-4 pt-3 pb-2" data-testid="conflict-card">
      <h3 className="flex items-center gap-2 text-[13px] leading-5 font-medium text-fg">
        <StatusGlyph status="suspect" size={14} />
        Systems disagree on {what}
      </h3>
      <div className="max-w-full overflow-x-auto">
        <table className="mt-1.5 w-full text-[13px] leading-5">
        <caption className="sr-only">Values for {what}, by system</caption>
        <tbody>
          {conflict.values.map((v, i) => (
            <tr key={i} className="border-t border-border align-top first:border-0 max-sm:flex max-sm:flex-wrap">
              {/* on a phone the system takes its own line, and the value with its reason the width below it */}
              <th scope="row" className="py-2 pr-3 text-left font-normal text-muted max-sm:w-full max-sm:pb-0.5">
                <span className="flex items-center gap-2">
                  <SystemCode system={v.source.system} size={16} />
                  {valueSystem(v.source)}
                </span>
              </th>
              <td className="py-2 pr-3 font-medium tabular-nums max-sm:min-w-0 max-sm:flex-1 max-sm:pt-0.5">
                {conflictValue(conflict, v.value)}
                {used === v && (
                  <span className="ml-2.5 inline-flex items-center gap-1 align-[-2px] text-[12px] font-normal text-fg">
                    <DoneRing size={12} />
                    Used
                  </span>
                )}
                {v.why && <span className="block text-meta font-normal text-muted">{whyUsed(v.why)}</span>}
              </td>
              <td className="py-2 text-right max-sm:pt-0.5">
                <SourceChip source={v.source} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
</div>
      {!used && (
        <p className="mt-1 mb-1 text-[12px] leading-[18px] text-muted">
          Neither value is confirmed, so neither is used. Ask {owner || "the record's owner"} which is right.
        </p>
      )}
    </section>
  );
}

/** The server's precedence wording, said plainly. */
function whyUsed(why: string): string {
  const w = readable(why);
  if (/trusted here|declared precedence/i.test(w)) {
    const m = /with the (.+?) record, which is trusted here/i.exec(w);
    return m ? `The ${m[1].replace(/^Portal ar customer$/i, "AR").replace(/^Portal oe order header$/i, "Sage order")} record wins for this field.` : "The other record wins for this field.";
  }
  return w;
}

const WEEK = 7 * 24 * 3600;
const DAY_MS = 24 * 3600 * 1000;

/**
 * The sources read more than a day before the data cut-off (`freshness.as_of`), oldest
 * first, as "Email and Teams to 30 Oct" (one date per group), or null when none is.
 */
export function behindCutOff(freshness: Freshness | null | undefined, now?: Date): string | null {
  const cut = Date.parse(freshness?.as_of ?? "");
  if (Number.isNaN(cut)) return null;
  const behind = (freshness?.sources ?? [])
    .filter((s) => s.as_of && cut - Date.parse(s.as_of) > DAY_MS)
    .sort((a, b) => Date.parse(a.as_of ?? "") - Date.parse(b.as_of ?? ""));
  if (behind.length === 0) return null;
  const byDay = new Map<string, string[]>();
  for (const s of behind) {
    const day = formatDate(s.as_of, { short: true, now });
    byDay.set(day, [...(byDay.get(day) ?? []), SYSTEM_LABEL[s.system] ?? s.system]);
  }
  return [...byDay].map(([day, names]) => `${names.join(" and ")} to ${day}`).join("; ");
}

/**
 * Sources whose searchable text ends over a day before their capture ("Email text
 * searchable to 29 Oct"): records captured after it exist, but search cannot find them yet.
 */
export function searchLag(freshness: Freshness | null | undefined, now?: Date): string | null {
  const lagging = (freshness?.sources ?? [])
    .filter((s) => s.as_of && s.searchable_to && Date.parse(s.as_of) - Date.parse(s.searchable_to) > DAY_MS)
    .sort((a, b) => Date.parse(a.searchable_to ?? "") - Date.parse(b.searchable_to ?? ""));
  if (lagging.length === 0) return null;
  return "searchable text: " + lagging
    .map((s) => `${SYSTEM_LABEL[s.system] ?? s.system} to ${formatDate(s.searchable_to, { short: true, now })}`)
    .join(", ");
}

/**
 * One grey line: "Data as of 1 Nov, 6:40 p.m." (the data cut-off, the same on every
 * screen), then any source read over a day before it ("Email and Teams to 30 Oct").
 * Per-system times are in its tooltip. Amber appears only when a source this view
 * relied on (`used`) is over a week old.
 */
export function FreshnessLabel({
  freshness,
  now,
  used,
}: {
  freshness: Freshness | null | undefined;
  now?: Date;
  used?: string[];
}) {
  if (!freshness || (!freshness.as_of && !freshness.label)) return null;
  const sources = freshness.sources ?? [];
  const detail = sources
    .filter((s) => s.as_of)
    .map((s) => `${SYSTEM_LABEL[s.system] ?? s.system}: ${formatDateTime(s.as_of, now)}`)
    .join(" · ");
  const old = sources.filter((s) => s.stale && (s.age_seconds ?? 0) > WEEK && (used ?? []).includes(s.system));
  const behind = behindCutOff(freshness, now);
  const lag = searchLag(freshness, now);
  return (
    <div className="space-y-1" data-testid="freshness">
      <p className="text-meta text-muted" title={detail || undefined}>
        {freshness.as_of ? <>Data as of {formatDateTime(freshness.as_of, now)}</> : freshness.label}
        {freshness.as_of && behind ? <span data-testid="freshness-behind">; {behind}</span> : null}
      </p>
      {freshness.capture_alarm?.line ? (
        <p className="flex items-center gap-1.5 text-meta text-fg" data-testid="freshness-capture-alarm">
          <OpenRing size={12} />
          {freshness.capture_alarm.line}.
        </p>
      ) : null}
      {lag ? (
        <p className="flex items-center gap-1.5 text-meta text-fg" data-testid="freshness-search-lag">
          <OpenRing size={12} />
          Newer records are not searchable yet ({lag}).
        </p>
      ) : null}
      {old.length > 0 && (
        <p className="flex items-center gap-1.5 text-meta text-fg" data-testid="freshness-old">
          <OpenRing size={12} />
          {old.map((s) => `${SYSTEM_LABEL[s.system] ?? s.system} last read ${formatDate(s.as_of, { short: true, now })}`).join(" · ")}.
        </p>
      )}
    </div>
  );
}

/**
 * "What I checked": each system the answer read or searched, what, and as of when. An old
 * source shows its date here; it turns amber only when over a week old and relied on.
 */
export function CheckedList({ freshness, facts, now }: { freshness: Freshness | null | undefined; facts: Fact[] | undefined; now?: Date }) {
  const bySystem = new Map<string, Set<string>>();
  for (const f of facts ?? [])
    for (const s of f.sources) {
      const set = bySystem.get(s.system) ?? new Set<string>();
      set.add(s.handle);
      bySystem.set(s.system, set);
    }
  const rows = (freshness?.sources ?? []).map((s) => {
    const n = bySystem.get(s.system)?.size ?? 0;
    const used = n > 0;
    const old = used && s.stale && (s.age_seconds ?? 0) > WEEK;
    return {
      code: s.system as string,
      system: SYSTEM_LABEL[s.system] ?? s.system,
      what: used ? `${n} cited` : "None cited",
      asOf: s.as_of ? formatDate(s.as_of, { short: true, now }) : "",
      old,
    };
  });
  for (const [sys, set] of bySystem)
    if (!(freshness?.sources ?? []).some((s) => s.system === sys))
      rows.push({ code: sys, system: SYSTEM_LABEL[sys as keyof typeof SYSTEM_LABEL] ?? sys, what: `${set.size} cited`, asOf: "", old: false });
  if (rows.length === 0) return <p className="text-[13px] text-muted">No source list.</p>;
  return (
    <dl className="space-y-1" data-testid="checked-list">
      {rows.map((r) => (
        <div key={r.system} className="grid h-7 grid-cols-[40px_minmax(0,1fr)_auto] items-center gap-x-2 text-[13px]">
          <SystemCode system={r.code} size={18} />
          <div className="flex min-w-0 items-baseline gap-2">
            <dt className="truncate">{r.system}</dt>
            <dd className={cn("shrink-0 text-[12px]", r.what === "None cited" ? "text-faint" : "text-muted")}>{r.what}</dd>
          </div>
          <dd className="inline-flex items-center gap-1 text-[12px] tabular-nums text-muted">
            {r.old && <OpenRing size={11} />}
            {r.asOf}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** A disclosure in the answer's row: closed by default, a chevron, the count in its label. */
export function Disclosure({
  label,
  count,
  children,
  defaultOpen = false,
  testId,
}: {
  label: string;
  /** Shown after the label in grey. */
  count?: number;
  children: React.ReactNode;
  defaultOpen?: boolean;
  testId?: string;
}) {
  return (
    <details className="group" open={defaultOpen} data-testid={testId}>
      <summary className="-mx-2 flex h-8 cursor-pointer list-none items-center gap-1.5 rounded-lg px-2 text-[13px] text-fg hover:bg-hover [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-3.5 shrink-0 text-muted transition-transform group-open:rotate-90" strokeWidth={2.2} aria-hidden />
        <h3 className="font-medium">{label}</h3>
        {count != null && <span className="num text-muted">{count}</span>}
      </summary>
      <div className="space-y-3 pt-2 pb-4 pl-5">{children}</div>
    </details>
  );
}
