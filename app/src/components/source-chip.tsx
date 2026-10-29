"use client";

import * as HoverCard from "@radix-ui/react-hover-card";
import {
  Calculator,
  Database,
  ExternalLink,
  FileText,
  Globe,
  ListChecks,
  Mail,
  MessagesSquare,
  Table2,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { formatDateTime, safeHref, SYSTEM_LABEL } from "@/lib/format";
import type { Source, SourceSystem } from "@/lib/types";

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

/** Record fields as label/value text. Only scalar values are shown; nested data is not dumped. */
export function fieldEntries(fields: Source["fields"]): [string, string][] {
  if (!fields || typeof fields !== "object" || Array.isArray(fields)) return [];
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(fields)) {
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") out.push([k, String(v)]);
  }
  return out.slice(0, 12);
}

export function SourcePreview({ source }: { source: Source }) {
  const sys = known(source.system) ? SYSTEM_LABEL[source.system] : "Source";
  const fields = fieldEntries(source.fields);
  return (
    <div className="space-y-2 text-sm">
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="font-medium text-fg">{sys}</span>
        <span aria-hidden>·</span>
        <span className="truncate">{source.label}</span>
      </div>
      {source.quote ? (
        <blockquote className="border-l-2 border-accent pl-3 leading-relaxed text-fg">“{source.quote}”</blockquote>
      ) : fields.length > 0 ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          {fields.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted">{k}</dt>
              <dd className="font-medium tabular-nums text-fg">{v}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-muted">No passage was quoted for this source.</p>
      )}
      {source.as_of && <p className="text-xs text-muted">As of {formatDateTime(source.as_of)}</p>}
      {safeHref(source.open_url) && (
        <p className="flex items-center gap-1 text-xs text-accent">
          <ExternalLink className="size-3" aria-hidden /> Click the chip to open it
        </p>
      )}
    </div>
  );
}

/** Inline citation: hover or focus shows the quote/fields; click opens the source. */
export function SourceChip({ source, className }: { source: Source; className?: string }) {
  const system = known(source.system) ? source.system : null;
  const Icon = system ? ICON[system] : FileText;
  const href = safeHref(source.open_url);
  const name = system ? (system === "File" ? "File" : system) : "Source";
  const chipClass = cn(
    "inline-flex max-w-[16rem] items-center gap-1.5 rounded-md border border-border bg-surface-2 px-1.5 py-0.5 align-middle text-xs font-medium text-fg transition-colors hover:border-border-strong hover:bg-border",
    className,
  );
  const inner = (
    <>
      <span className={cn("size-1.5 shrink-0 rounded-full", system ? DOT[system] : "bg-slate-400")} aria-hidden />
      <Icon className="size-3 shrink-0 text-muted" aria-hidden />
      <span>{name}</span>
      <span className="truncate font-normal text-muted">{source.label}</span>
    </>
  );
  const aria = `Source: ${system ? SYSTEM_LABEL[system] : "source"}, ${source.label}${href ? " (opens in a new tab)" : ""}`;
  return (
    <HoverCard.Root openDelay={120} closeDelay={80}>
      <HoverCard.Trigger asChild>
        {href ? (
          <a href={href} target="_blank" rel="noopener noreferrer" className={chipClass} aria-label={aria} data-testid="source-chip">
            {inner}
          </a>
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
          className="z-50 w-[min(22rem,calc(100vw-24px))] rounded-xl border border-border bg-surface p-3 shadow-card animate-fade-in"
          data-testid="source-preview"
        >
          <SourcePreview source={source} />
        </HoverCard.Content>
      </HoverCard.Portal>
    </HoverCard.Root>
  );
}
