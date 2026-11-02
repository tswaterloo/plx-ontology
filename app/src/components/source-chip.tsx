"use client";

import * as HoverCard from "@radix-ui/react-hover-card";
import Link from "next/link";
import { useState } from "react";
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

const DOT: Record<SourceSystem, string> = {
  BC: "bg-emerald-500",
  Portal: "bg-sky-500",
  Sage: "bg-lime-500",
  FileMaker: "bg-orange-500",
  Email: "bg-amber-500",
  Teams: "bg-violet-500",
  File: "bg-slate-500",
  MC: "bg-rose-500",
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
  return (
    <div className="space-y-2 text-row">
      <div className="flex items-center gap-2 text-meta text-muted">
        <span className="font-medium text-fg">{sys}</span>
        <span aria-hidden>·</span>
        <span className="truncate">{sourceName(source)}</span>
      </div>
      {source.quote ? (
        <blockquote className="border-l-2 border-accent pl-3 text-fg">“{source.quote}”</blockquote>
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
  const chipClass = cn(
    "inline-flex h-6 max-w-[16rem] items-center gap-1.5 rounded-md border border-border bg-surface-2 px-1.5 align-middle text-label text-fg transition-colors hover:border-border-strong hover:bg-border",
    opens && "cursor-pointer",
    className,
  );
  const inner = (
    <>
      <span className={cn("size-1.5 shrink-0 rounded-full", system ? DOT[system] : "bg-slate-400")} aria-hidden />
      <Icon className="size-3 shrink-0 text-muted" aria-hidden />
      <span className="truncate">{name}</span>
    </>
  );
  const aria = `Source: ${system ? SYSTEM_LABEL[system] : "source"}, ${name}${opens ? " (opens the evidence)" : ""}`;
  return (
    <>
      <HoverCard.Root openDelay={200} closeDelay={80} open={hover && !open} onOpenChange={setHover}>
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
            className="z-40 w-[min(22rem,calc(100vw-24px))] rounded-xl border border-border bg-surface p-3 shadow-pop animate-fade-in"
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
  const list = sources ?? [];
  if (list.length === 0) return null;
  const shown = list.slice(0, max);
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
        className="inline-flex h-6 items-center rounded-md border border-border px-1.5 text-label text-muted hover:bg-surface-2 cursor-pointer"
        aria-label={`${sources.length} more sources`}
      >
        +{sources.length}
      </button>
      {open && (
        <Sheet open={open} onOpenChange={setOpen} title={`${sources.length} more sources`}>
          <ul className="space-y-3">
            {sources.map((s, i) => (
              <li key={i} className="rounded-lg border border-border p-3">
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
