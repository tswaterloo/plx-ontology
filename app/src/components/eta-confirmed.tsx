"use client";

import Link from "next/link";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { etaShown, lineShown, MATCHED_BY, promiseLate, type EtaShown } from "@/lib/eta";
import { formatDate, objectHref, plural, safeHref } from "@/lib/format";
import { STATUS_CLASS } from "@/lib/status";
import type { EtaCheck, ObjectRef, PromiseLine, Source } from "@/lib/types";
import { EvidenceDrawer } from "./source-chip";
import { Card, CardBody, CardHeader, CardTitle } from "./ui/card";

/** A status that may run longer than three words (it wraps on a narrow screen): a small square in its colour and the words. */
export function EtaPill({ shown, className, testId, quiet = false }: { shown: EtaShown | null; className?: string; testId?: string; quiet?: boolean }) {
  if (!shown) return null;
  const c = STATUS_CLASS[shown.status];
  // quiet: the same status down a whole column is said once above it, then in plain muted words
  return (
    <span data-status={shown.status} data-testid={testId} className={cn("inline-flex items-baseline gap-1.5 text-label", quiet ? "text-muted" : cn("font-medium", shown.status === "neutral" ? "text-muted" : c.fg), className)}>
      {!quiet && <span className={cn("size-1.5 shrink-0 translate-y-[-1px] self-center", c.dot)} aria-hidden />}
      {shown.label}
    </span>
  );
}

/** "Open the confirmation": the source in the evidence drawer (the page stays put). */
function Evidence({ source, label }: { source: Source | null | undefined; label: string }) {
  const [open, setOpen] = useState(false);
  if (!source || !(source.handle || safeHref(source.open_url))) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} className="whitespace-nowrap text-meta font-medium text-accent hover:underline cursor-pointer">
        {label}
      </button>
      {open && <EvidenceDrawer source={source} open={open} onOpenChange={setOpen} />}
    </>
  );
}

const refHref = (r: ObjectRef) => safeHref(r.open_url) ?? objectHref(r.type, r.object_id);

function LineRow({ l, link }: { l: PromiseLine; link: "confirmed" | "unconfirmed_link" }) {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2" data-testid="eta-line">
      <Link href={refHref(l.line)} className="min-w-0 flex-1 basis-48 truncate font-medium hover:underline">
        {l.line.label}
      </Link>
      <span className="tabular-nums text-muted">{l.promised_date ? `${link === "confirmed" ? "arrives" : "says"} ${formatDate(l.promised_date, { short: true })}` : "dates disagree"}</span>
      <EtaPill shown={lineShown(l, link)} />
      {l.matched_by && <span className="text-meta text-muted">{MATCHED_BY[l.matched_by] ?? l.matched_by}</span>}
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
    <Card data-testid="eta-card">
      <CardHeader>
        <CardTitle>ETA confirmed?</CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <EtaPill shown={shown} testId="eta-badge" />
          <EtaPill shown={late} testId="eta-late" />
          <Evidence source={source} label={p?.link === "unconfirmed_link" || eta.state === "lead_only" ? "Open the possible confirmation" : "Open the confirmation"} />
        </div>
        {p?.link === "unconfirmed_link" && <p className="text-meta text-muted">The document names this PO, but its link to it isn&apos;t confirmed, so its date isn&apos;t counted.</p>}
        {!p && eta.text && <p className="text-meta text-muted">{eta.text}</p>}
        {lines.length > 0 && (
          <ul className="divide-y divide-border border-t border-border text-row" aria-label="Promised date per line">
            {lines.map((l) => (
              <LineRow key={l.line.object_id} l={l} link={p!.link} />
            ))}
          </ul>
        )}
        {((p?.lines_hidden ?? 0) > 0 || (p?.lines_unmatched ?? 0) > 0 || p?.complete === false) && (
          <p className="text-meta text-muted" data-testid="eta-notes">
            {[
              p?.lines_hidden ? `${plural(p.lines_hidden, "more line")} promised in a confirmation you can't open.` : null,
              p?.lines_unmatched ? `${plural(p.lines_unmatched, "open line")} not matched to a promised date.` : null,
              p?.complete === false && !p?.lines_hidden ? "A later promise you can't see may exist." : null,
            ]
              .filter(Boolean)
              .join(" ")}
          </p>
        )}
      </CardBody>
    </Card>
  );
}
