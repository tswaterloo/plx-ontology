"use client";

import Link from "next/link";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { etaShown, lineShown, MATCHED_BY, promiseLate, type EtaShown } from "@/lib/eta";
import { formatDate, objectHref, plural, safeHref } from "@/lib/format";
import type { EtaCheck, ObjectRef, PromiseLine, Source } from "@/lib/types";
import { EvidenceDrawer } from "./source-chip";
import { StatusGlyph } from "./ui/glyphs";

/** A status that may run longer than three words (it wraps on a narrow screen): its shape, then the words in ink. */
export function EtaPill({ shown, className, testId, quiet = false }: { shown: EtaShown | null; className?: string; testId?: string; quiet?: boolean }) {
  if (!shown) return null;
  // quiet: the same status down a whole column is said once above it, then in plain muted words
  const plain = quiet || shown.status === "neutral";
  return (
    <span data-status={shown.status} className={cn("inline-flex items-baseline gap-1.5 text-[13px] leading-5", plain ? "text-muted" : "font-medium text-fg", className)}>
      {!plain && <StatusGlyph status={shown.status} size={13} className="relative top-[2px]" />}
      <span data-testid={testId}>{shown.label}</span>
    </span>
  );
}

/** "Open the confirmation": the source in the evidence drawer (the page stays put). */
function Evidence({ source, label }: { source: Source | null | undefined; label: string }) {
  const [open, setOpen] = useState(false);
  if (!source || !(source.handle || safeHref(source.open_url))) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="whitespace-nowrap text-[12px] font-medium text-muted underline decoration-border-strong underline-offset-2 hover:text-fg cursor-pointer">
        {label}
      </button>
      {open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
    </>
  );
}

const refHref = (r: ObjectRef) => safeHref(r.open_url) ?? objectHref(r.type, r.object_id);

function LineRow({ l, link }: { l: PromiseLine; link: "confirmed" | "unconfirmed_link" }) {
  return (
    <li className="flex min-h-9 flex-wrap items-center gap-x-4 gap-y-1 py-2" data-testid="eta-line">
      <Link href={refHref(l.line)} className="min-w-0 flex-1 basis-48 truncate font-medium hover:underline">
        {l.line.label}
      </Link>
      <span className="tabular-nums text-muted">{l.promised_date ? `${link === "confirmed" ? "arrives" : "says"} ${formatDate(l.promised_date, { short: true })}` : "dates disagree"}</span>
      <EtaPill shown={lineShown(l, link)} />
      {l.matched_by && <span className="text-[12px] text-muted">{MATCHED_BY[l.matched_by] ?? l.matched_by}</span>}
      <Evidence source={l.source} label="Confirmation" />
    </li>
  );
}

/**
 * "ETA confirmed?" on a purchase order: whether a supplier confirmation backs the date, the
 * order (or each promised line) against the supplier's own date, and the confirmation one
 * click away. Beside our requested-by verdict, never in its place.
 */
export function EtaCard({ eta }: { eta: EtaCheck | null | undefined }) {
  const shown = etaShown(eta);
  if (!eta || !shown) return null;
  const p = eta.promise ?? null;
  const lines = p?.lines ?? [];
  const late = p ? promiseLate(p, p.link) : null;
  const source = p?.source ?? eta.source;
  return (
    <section className="space-y-3" data-testid="eta-card">
      <h2 className="text-[12px] font-medium leading-4 text-muted">Supplier confirmation</h2>
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
        <EtaPill shown={shown} testId="eta-badge" />
        <EtaPill shown={late} testId="eta-late" />
        <Evidence source={source} label={p?.link === "unconfirmed_link" || eta.state === "lead_only" ? "Open possible confirmation" : "Open confirmation"} />
      </div>
      {p?.link === "unconfirmed_link" && <p className="text-[12px] leading-4 text-muted">The document mentions this PO but isn&apos;t linked to it, so its date isn&apos;t used.</p>}
      {!p && eta.text && <p className="text-[13px] leading-5 text-muted">{eta.text}</p>}
      {lines.length > 0 && (
        <ul className="divide-y divide-border border-y border-border text-[13px]" aria-label="Promised date per line">
          {lines.map((l) => (
            <LineRow key={l.line.object_id} l={l} link={p!.link} />
          ))}
        </ul>
      )}
      {((p?.lines_hidden ?? 0) > 0 || (p?.lines_unmatched ?? 0) > 0 || p?.complete === false) && (
        <p className="text-[12px] leading-4 text-muted" data-testid="eta-notes">
          {[
            p?.lines_hidden ? `${plural(p.lines_hidden, "more line")} promised in a confirmation you can't open.` : null,
            p?.lines_unmatched ? `${plural(p.lines_unmatched, "open line")} not matched to a promised date.` : null,
            p?.complete === false && !p?.lines_hidden ? "A later promise you can't see may exist." : null,
          ]
            .filter(Boolean)
            .join(" ")}
        </p>
      )}
    </section>
  );
}
