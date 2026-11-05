"use client";

import * as HoverCard from "@radix-ui/react-hover-card";
import Link from "next/link";
import { createContext, useContext, useState } from "react";
import {
  Calculator,
  Database,
  ExternalLink,
  Maximize2,
  FileText,
  Globe,
  ListChecks,
  Mail,
  MessagesSquare,
  Table2,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { currencyCode, fieldKey, fieldLabel, fieldValue, formatDate, formatDateTime, formatMoney, isInternalField, isUnsetDate, recordKind, safeHref, sourceHref, SYSTEM_LABEL } from "@/lib/format";
import type { Source, SourceSystem } from "@/lib/types";
import { mailField, SourceLoader } from "./source-view";
import { Sheet } from "./ui/sheet";

const ICON: Record<SourceSystem, LucideIcon> = {
  BC: Database,
  Portal: Globe,
  Sage: Calculator,
  FileMaker: Table2,
  Email: Mail,
  Teams: MessagesSquare,
  File: FileText,
  MC: ListChecks,
};

function known(system: string): system is SourceSystem {
  return system in SYSTEM_LABEL;
}

const norm = (k: string) => (fieldKey(k).split(".").pop() ?? k).toLowerCase().replace(/[^a-z0-9]/g, "");

/** The curated order of a business record's fields: what a person asks of it first. */
const ORDER: RegExp[] = [
  /^(?:number|no|ponumber|documentnumber|fmponumber|ordernumber|sonumber|invoicenumber|code|reference)$/,
  /status$/,
  /^(?:vendorname|buyfromvendorname|vendor|suppliername|supplier|customername|selltocustomername|customer|displayname|name)$/,
  /^(?:total|totalamount|totalamountincludingtax|totalamountexcludingtax|totalcost|amount|amountincludingtax|grandtotal|subtotal|balance|price|unitprice|unitcost|directunitcost)$/,
  /^(?:currency|currencycode)$/,
  /^(?:orderdate|documentdate|postingdate|duedate|requestedreceiptdate|expectedreceiptdate|promisedreceiptdate|datereceived|receiveddate|shipmentdate|invoicedate|dategenerated|createdat)$/,
  /^(?:description|itemdescription|quantity|qty|unitofmeasurecode|unitofmeasure|uom)$/,
];
const MONEY = /(?:total|amount|cost|price|balance)/;
const NOT_MONEY = /(?:qty|quantit|count|percent|discount(?:applied)|line)/;

export interface CuratedFields {
  /** The cited field(s) first, then number, status, party, totals, dates. */
  lead: [string, string][];
  /** Every other business field (keys and plumbing never). */
  more: [string, string][];
}

/**
 * A captured record's fields as a person reads them: internal keys (row.id, hashes, change
 * keys, mail headers) hidden, labels in words, the field the answer cited first, then a
 * curated set (number, status, vendor or customer, totals with their currency, dates),
 * never the first fields alphabetically. Only scalar values; nested data is not dumped.
 */
export function curatedFields(fields: Source["fields"], cited: string[] = []): CuratedFields {
  if (!fields || typeof fields !== "object" || Array.isArray(fields)) return { lead: [], more: [] };
  const entries: [string, string][] = [];
  const wanted = cited.map(norm).filter(Boolean);
  for (const [k, v] of Object.entries(fields)) {
    if (v === null || v === undefined || v === "") continue;
    if (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean") continue;
    // a field the answer cites always shows (with a plain label), whatever its name
    if (isInternalField(k) && !wanted.includes(norm(k))) continue;
    if (isUnsetDate(String(v))) continue; // BC's empty date: no date, never "1 Jan 1"
    const shown = fieldValue(k, String(v));
    if (!shown) continue;
    entries.push([k, shown]);
  }
  const currencyEntry = entries.find(([k]) => /^(?:currency|currencycode)$/.test(norm(k)));
  const currency = currencyEntry ? currencyCode(currencyEntry[1]) : "";
  const rank = (k: string): number => {
    const n = norm(k);
    if (wanted.includes(n)) return -1;
    const i = ORDER.findIndex((r) => r.test(n));
    return i === -1 ? ORDER.length : i;
  };
  const sorted = entries
    .map((e, i) => ({ e, i, r: rank(e[0]) }))
    .sort((a, b) => a.r - b.r || a.i - b.i)
    .map(({ e: [k, v], r }) => {
      const n = norm(k);
      const money = currency && MONEY.test(n) && !NOT_MONEY.test(n) && /^-?\d+(?:\.\d+)?$/.test(v);
      return { r, pair: [fieldLabel(k), money ? formatMoney(v, currency) : v] as [string, string] };
    });
  return {
    lead: sorted.filter((x) => x.r < ORDER.length).slice(0, 12).map((x) => x.pair),
    more: [...sorted.filter((x) => x.r < ORDER.length).slice(12), ...sorted.filter((x) => x.r >= ORDER.length)].map((x) => x.pair),
  };
}

/** Record fields as label/value text for a preview: the curated lead, else the first others. */
export function fieldEntries(fields: Source["fields"], cited: string[] = []): [string, string][] {
  const { lead, more } = curatedFields(fields, cited);
  return [...lead, ...more].slice(0, 12);
}

/**
 * Citation numbers for the sources of one answer (handle -> 1, 2, …). Inside it a chip whose
 * source is numbered is set as a small numbered marker on the claim; the numbered list under
 * the answer names each one. Outside it (record pages) chips name their source.
 */
export const CiteNumbers = createContext<ReadonlyMap<string, number> | null>(null);

/** Short system names for chips; the full name is in the aria label and the drawer. */
const SHORT: Record<SourceSystem, string> = {
  BC: "BC",
  Portal: "Portal",
  Sage: "Sage",
  FileMaker: "FileMaker",
  Email: "Email",
  Teams: "Teams",
  File: "File",
  MC: "Mission Control",
};

/**
 * What a chip calls its source: the record ("PO 106093"), or "Email · Ross, 2 Sep", never the
 * environment ("BC production"). `record` is the caller's name for the record when it knows it
 * (the page's own record, the answer's one subject).
 */
export function sourceName(source: Source, record?: string | null): string {
  const system = known(source.system) ? source.system : null;
  if (system === "Email" || system === "Teams") {
    const from = mailField(source.fields, "from").replace(/@.*$/, "");
    const who = from ? from[0].toUpperCase() + from.slice(1) : "";
    const at = source.as_of ? formatDate(source.as_of, { short: true }).replace(/ \d{4}$/, "") : "";
    // no sender captured: the label names it when it is more than "Email message"
    const named = !who && source.label && !/^(?:email|teams)? ?message$/i.test(source.label.trim()) ? source.label.replace(/^(?:email|message) from /i, "") : "";
    const tail = [who || named, at].filter(Boolean).join(", ");
    return tail ? `${SHORT[system]} · ${tail}` : SHORT[system];
  }
  if (record) return record;
  const kind = recordKind(source.handle, source.label);
  const sys = system ? SHORT[system] : "Source";
  return kind ? `${sys} · ${kind}` : system ? SYSTEM_LABEL[system] : source.label;
}

export function SourcePreview({ source }: { source: Source }) {
  const sys = known(source.system) ? SYSTEM_LABEL[source.system] : "Source";
  const fields = fieldEntries(source.fields, Object.keys(source.fields ?? {}));
  // the system is said once: "Email · Maple Leaf Soapworks, 21 Oct", not "Email · Email · …"
  const name = sourceName(source);
  const short = known(source.system) ? SHORT[source.system] : "";
  const shownName = short && name.startsWith(`${short} · `) ? name.slice(short.length + 3) : name === short || name === sys ? "" : name;
  return (
    <div className="space-y-2 text-row">
      <div className="flex items-center gap-2 text-meta text-muted">
        <span className="font-medium text-fg">{sys}</span>
        {shownName && (
          <>
            <span aria-hidden>·</span>
            <span className="truncate">{shownName}</span>
          </>
        )}
      </div>
      {source.quote ? (
        <blockquote className="border-l-2 border-accent pl-3 font-serif text-[15px] leading-[22px] italic text-fg">“{source.quote}”</blockquote>
      ) : fields.length > 0 ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {fields.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted">{k}</dt>
              <dd className="min-w-0 break-words font-medium tabular-nums text-fg">{v}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-muted">No passage was quoted for this source.</p>
      )}
      {source.as_of && <p className="text-meta text-muted">Read {formatDateTime(source.as_of)}</p>}
      <p className="flex items-center gap-1 text-meta text-accent">
        <ExternalLink className="size-3" aria-hidden /> Click to open it
      </p>
    </div>
  );
}

/** The evidence drawer for one source: the cited passage first, then the record or message. */
export function EvidenceDrawer({
  source,
  open,
  onOpenChange,
}: {
  source: Source;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const sys = known(source.system) ? SYSTEM_LABEL[source.system] : "Source";
  const cited = Object.keys(source.fields ?? {});
  const full = source.handle ? `${sourceHref(source.handle)}${cited.map((f) => `&field=${encodeURIComponent(f)}`).join("")}` : safeHref(source.open_url);
  return (
    <Sheet open={open} onOpenChange={onOpenChange} title={`Evidence · ${sys}`}>
      {open && source.handle ? (
        <SourceLoader
          handle={source.handle}
          cited={cited}
          quote={source.quote}
          headingLevel={2}
          actions={() =>
            full ? (
              <Link href={full} className="inline-flex items-center gap-1 pt-1 text-meta font-medium text-accent hover:underline">
                <Maximize2 className="size-3.5" aria-hidden /> Open full page
              </Link>
            ) : null
          }
        />
      ) : (
        <SourcePreview source={source} />
      )}
    </Sheet>
  );
}

/**
 * Inline citation, labelled by the record. Click or tap opens the evidence drawer (the page
 * stays put); hover or focus shows a short preview on desktop. With no safe link and no
 * handle it is a plain label with the preview.
 */
export function SourceChip({ source, className, record }: { source: Source; className?: string; record?: string | null }) {
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState(false);
  const system = known(source.system) ? source.system : null;
  const Icon = system ? ICON[system] : FileText;
  const href = safeHref(source.open_url);
  const opens = !!href || !!source.handle;
  const name = sourceName(source, record);
  const cites = useContext(CiteNumbers);
  const n = source.handle ? cites?.get(source.handle) : undefined;
  const chipClass = n !== undefined
    ? cn(
        "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-sm bg-accent-soft px-1 align-[1px] font-sans text-[11px] font-semibold leading-none tabular-nums text-accent transition-colors hover:bg-accent hover:text-accent-fg",
        opens && "cursor-pointer",
        className,
      )
    : cn(
    "inline-flex h-5 max-w-[16rem] items-center gap-1.5 rounded-sm border border-border bg-surface px-1.5 align-middle text-label font-normal text-muted transition-colors hover:border-accent hover:text-accent",
    opens && "cursor-pointer",
    className,
  );
  // "BC · PO line": the system as a small-caps tag, then what it is
  const [lead, ...tail] = name.split(" · ");
  const tagged = !!system && lead === SHORT[system];
  const inner = n !== undefined ? (
    <>{n}</>
  ) : (
    <>
      {tagged ? (
        <span className="shrink-0 text-[10px] font-semibold uppercase leading-none tracking-[0.06em] text-faint">{lead}</span>
      ) : (
        <Icon className="size-3 shrink-0 text-faint" strokeWidth={1.5} aria-hidden />
      )}
      {(!tagged || tail.length > 0) && (
        <span className="truncate">{tagged ? tail.join(" · ").replace(new RegExp(`^${lead}\\s+(?=\\S)`, "i"), "") : name}</span>
      )}
    </>
  );
  const aria = `Source: ${system ? SYSTEM_LABEL[system] : "source"}, ${name}${opens ? " (opens the evidence)" : ""}`;
  return (
    <>
      <HoverCard.Root openDelay={120} closeDelay={150} open={hover && !open} onOpenChange={setHover}>
        <HoverCard.Trigger asChild>
          {opens ? (
            <button
              type="button"
              onClick={() => {
                setHover(false);
                setOpen(true);
              }}
              className={chipClass}
              aria-label={aria}
              data-testid="source-chip"
            >
              {inner}
            </button>
          ) : (
            <span tabIndex={0} className={chipClass} aria-label={aria} data-testid="source-chip">
              {inner}
            </span>
          )}
        </HoverCard.Trigger>
        <HoverCard.Portal>
          <HoverCard.Content
            side="top"
            align="start"
            sideOffset={6}
            collisionPadding={12}
            className="z-40 w-[min(22rem,calc(100vw-24px))] rounded-xl border border-border bg-raised p-3 shadow-pop animate-fade-in"
            data-testid="source-preview"
          >
            <SourcePreview source={source} />
          </HoverCard.Content>
        </HoverCard.Portal>
      </HoverCard.Root>
      {opens && open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
    </>
  );
}

/** At most `max` chips; the rest collapse into "+N" (which opens the first hidden one). */
export function SourceChips({ sources, max = 2, record }: { sources: Source[] | null | undefined; max?: number; record?: (s: Source) => string | null | undefined }) {
  const cites = useContext(CiteNumbers);
  const all = sources ?? [];
  // numbered markers are small: every one shows, each number once
  const list = cites ? all.filter((s, i) => !s.handle || all.findIndex((x) => x.handle === s.handle) === i) : all;
  if (list.length === 0) return null;
  const shown = cites ? list : list.slice(0, max);
  const rest = list.length - shown.length;
  return (
    <span className="inline-flex flex-wrap items-center gap-1 align-middle">
      {shown.map((s, i) => (
        <SourceChip key={i} source={s} record={record?.(s)} />
      ))}
      {rest > 0 && <MoreChip sources={list.slice(max)} />}
    </span>
  );
}

function MoreChip({ sources }: { sources: Source[] }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex h-5 items-center rounded-sm border border-border px-1.5 text-label text-muted hover:border-accent hover:text-accent cursor-pointer"
        aria-label={`${sources.length} more sources`}
      >
        +{sources.length}
      </button>
      {open && (
        <Sheet open={open} onOpenChange={setOpen} title={`${sources.length} more sources`}>
          <ul className="space-y-3">
            {sources.map((s, i) => (
              <li key={i} className="border-t border-border pt-3">
                <SourcePreview source={s} />
                <div className="mt-2">
                  <SourceChip source={s} />
                </div>
              </li>
            ))}
          </ul>
        </Sheet>
      )}
    </>
  );
}

/** One numbered row of an answer's sources: its number, system, what it is and when it was read; it opens the evidence. */
export function SourceListRow({ source, n }: { source: Source; n: number }) {
  const [open, setOpen] = useState(false);
  const system = known(source.system) ? source.system : null;
  const name = sourceName(source);
  const [lead, ...tail] = name.split(" · ");
  const tagged = !!system && lead === SHORT[system];
  const message = system === "Email" || system === "Teams";
  // a message is named by its own label ("from Maple Leaf Soapworks, 21 Oct"), one date only
  const what = message && source.label && !/^(?:email|teams)? ?message$/i.test(source.label.trim())
    ? source.label.replace(/^(?:email|teams|message)\s+/i, "").replace(/^from\s+/i, "")
    : tagged
      ? tail.join(" · ").replace(new RegExp(`^${lead}\\s+(?=\\S)`, "i"), "") || SYSTEM_LABEL[system!]
      : name;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group grid w-full grid-cols-[22px_minmax(0,1fr)] items-baseline gap-x-2 rounded py-1 text-left text-meta hover:bg-surface cursor-pointer"
        aria-label={`Source ${n}: ${system ? SYSTEM_LABEL[system] : "source"}, ${name}`}
        data-testid="answer-source"
      >
        <span className="inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-sm bg-accent-soft px-1 text-[11px] font-semibold tabular-nums text-accent group-hover:bg-accent group-hover:text-accent-fg">
          {n}
        </span>
        <span className="min-w-0 truncate">
          <span className="mr-1.5 text-[10px] font-semibold uppercase tracking-[0.06em] text-faint">{system ? SHORT[system] : "Source"}</span>
          <span className="text-fg">{what}</span>
          {source.as_of && !message && <span className="text-faint"> · read {formatDate(source.as_of, { short: true })}</span>}
        </span>
      </button>
      {open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
    </>
  );
}
