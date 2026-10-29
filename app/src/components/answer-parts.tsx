"use client";

import { AlertTriangle, Check, Clock, Dot, EyeOff, HelpCircle, Scale, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/cn";
import { ago, formatDateTime, plural, SYSTEM_LABEL } from "@/lib/format";
import type { Conflict, Fact, Freshness, Hidden, NotFoundItem } from "@/lib/types";
import { SourceChip } from "./source-chip";

export function FactList({ facts }: { facts: Fact[] | undefined }) {
  if (!facts || facts.length === 0) return null;
  return (
    <ul className="space-y-2.5" aria-label="Supporting facts">
      {facts.map((f, i) => (
        <li key={i} className="flex gap-2.5 text-[15px] leading-relaxed">
          {f.check ? (
            <Check className="mt-1 size-4 shrink-0 text-good" aria-label="Checked against its source" />
          ) : (
            <Dot className="mt-0.5 size-5 shrink-0 text-muted" aria-hidden />
          )}
          <span>
            {f.text}{" "}
            <span className="inline-flex flex-wrap gap-1 align-middle">
              {f.sources.map((s, j) => (
                <SourceChip key={j} source={s} />
              ))}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}

export function NotFoundSection({ items }: { items: NotFoundItem[] | undefined }) {
  if (!items || items.length === 0) return null;
  return (
    <section className="rounded-xl border border-border bg-bg-subtle p-4" data-testid="not-found">
      <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
        <HelpCircle className="size-4 text-warn" aria-hidden /> Not found or not confirmed
      </h3>
      <ul className="space-y-1.5 text-sm">
        {items.map((n, i) => (
          <li key={i}>
            <span className="text-fg">{n.what}</span>
            {n.where_it_would_live && <span className="text-muted"> — would normally be in: {n.where_it_would_live}</span>}
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
      className="flex gap-3 rounded-xl border border-info/30 bg-info-soft p-4 text-sm"
      data-testid="hidden-notice"
    >
      <EyeOff className="mt-0.5 size-4 shrink-0 text-info" aria-hidden />
      <div className="space-y-1">
        <p className="font-medium text-fg">
          {unknown
            ? "Some items may be hidden from you because you don't have access."
            : `${plural(count, "more item")} match${count === 1 ? "es" : ""}, but you don't have access.`}
        </p>
        <p className="text-muted">
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

export function ConflictCard({ conflict }: { conflict: Conflict }) {
  const trusted = conflict.values.filter((v) => v.trusted);
  const what = conflict.subject ? `${conflict.field} (${conflict.subject})` : conflict.field;
  return (
    <section aria-label={`Conflicting values for ${what}`} className="rounded-xl border border-warn/40 bg-warn-soft/60 p-4" data-testid="conflict-card">
      <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <Scale className="size-4 shrink-0 text-warn" aria-hidden />
        The sources disagree on {what}
      </h3>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {conflict.values.map((v, i) => (
          <div
            key={i}
            className={cn("min-w-0 rounded-lg border bg-surface p-3", v.trusted && trusted.length === 1 ? "border-good/50 ring-1 ring-good/30" : "border-border")}
          >
            <div className="flex items-start justify-between gap-2">
              <span className="text-lg font-semibold tabular-nums">{v.value}</span>
              {v.trusted && trusted.length === 1 && (
                <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-good-soft px-2 py-0.5 text-xs font-medium text-good">
                  <ShieldCheck className="size-3" aria-hidden /> Trusted
                </span>
              )}
            </div>
            <div className="mt-2">
              <SourceChip source={v.source} />
            </div>
            {v.why && <p className="mt-2 text-xs leading-relaxed text-muted">{v.why}</p>}
          </div>
        ))}
      </div>
      {trusted.length !== 1 && (
        <p className="mt-3 text-sm text-muted">Neither value is confirmed; check with the record owner before acting.</p>
      )}
    </section>
  );
}

export function FreshnessLabel({ freshness, now }: { freshness: Freshness | null | undefined; now?: Date }) {
  if (!freshness || (!freshness.as_of && !freshness.label)) return null;
  const sources = freshness.sources ?? [];
  const stale = sources.filter((s) => s.stale);
  const detail = sources
    .filter((s) => s.as_of)
    .map((s) => `${SYSTEM_LABEL[s.system] ?? s.system}: ${formatDateTime(s.as_of)}`)
    .join(" · ");
  return (
    <div className="space-y-1" data-testid="freshness">
      <p className="inline-flex items-center gap-1.5 text-xs text-muted" title={detail || undefined}>
        <Clock className="size-3.5" aria-hidden />
        {freshness.as_of ? (
          <>
            Data as of {formatDateTime(freshness.as_of)}
            <span aria-hidden>·</span>
            {ago(freshness.as_of, now)}
          </>
        ) : (
          freshness.label
        )}
      </p>
      {stale.length > 0 && (
        <p className="inline-flex items-center gap-1.5 text-xs text-warn">
          <AlertTriangle className="size-3.5" aria-hidden />
          Out of date: {stale.map((s) => SYSTEM_LABEL[s.system] ?? s.system).join(", ")}
        </p>
      )}
    </div>
  );
}
