"use client";

// An alert opened from My work: what fired and why, the figures it rests on (the order
// total beside its lines' sum when they disagree), how long it has been open, whose it is
// and the customer orders it touches, each figure next to the record it came from.

import { formatDate, formatMoney, plural, readable } from "@/lib/format";
import { briefingKind } from "@/lib/status";
import type { BriefingItem } from "@/lib/types";
import { SourceChip } from "./source-chip";

const DAY = 86_400_000;

function Row({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="grid grid-cols-[112px_minmax(0,1fr)] gap-x-4 py-1.5 text-[13px] leading-5" data-testid={testId}>
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

export function AlertFired({ item, now = new Date() }: { item: BriefingItem; now?: Date }) {
  const kind = briefingKind(item.event_type, { amountSuspect: !!item.amount_suspect, fallbackLabel: item.label });
  const first = item.first_seen ? Date.parse(item.first_seen) : NaN;
  const openDays = Number.isNaN(first) ? null : Math.max(0, Math.floor((now.getTime() - first) / DAY));
  const src = (item.sources ?? [])[0];
  const amount = item.amount ? formatMoney(item.amount.amount, item.amount.currency) : null;
  const lines = item.lines_sum ? formatMoney(item.lines_sum.amount, item.lines_sum.currency) : null;
  return (
    <section data-testid="alert-fired">
      <h3 className="mb-1 text-[12px] font-medium leading-4 text-muted">What fired</h3>
      <dl className="divide-y divide-border border-y border-border">
        <Row label="Alert">
          <span className="font-medium">{kind.label}</span>
          {item.new_since_visit && <span className="text-muted"> · new since your last visit</span>}
        </Row>
        <Row label="What it found" testId="alert-what">
          {readable(item.summary)}
          {src && (
            <span className="ml-2 inline-block align-baseline text-[12px]">
              <SourceChip source={src} />
            </span>
          )}
        </Row>
        {item.why && <Row label="Reason">{readable(item.why)}</Row>}
        {(amount || lines) && (
          <Row label="Figures" testId="alert-figures">
            {amount && (
              <span className="num">
                {amount}
                {item.amount_basis ? <span className="text-muted"> ({readable(item.amount_basis)})</span> : null}
              </span>
            )}
            {item.amount_disagree && lines && (
              <span className="block text-[color:var(--risk)]">
                The order total and its lines disagree: lines sum to <span className="num">{lines}</span>
              </span>
            )}
            {item.amount_suspect && <span className="block text-[color:var(--risk)]">Check the total: {readable(item.amount_suspect)}</span>}
          </Row>
        )}
        <Row label="Open for" testId="alert-open">
          {openDays === null ? <span className="text-faint">First seen not on record</span> : <>{openDays === 0 ? "Since today" : plural(openDays, "day")}<span className="text-muted"> · first seen {formatDate(item.first_seen, { short: true })}</span></>}
          {typeof item.days === "number" && item.days > 0 && <span className="text-muted"> · {plural(item.days, "day")} past its date</span>}
        </Row>
        <Row label="Whose" testId="alert-owner">
          {item.yours === true ? "Yours" : item.yours === false ? "Not routed to you" : <span className="text-faint">Owner not on record</span>}
          {(item.why_yours ?? [])[0] && <span className="text-muted">: {readable((item.why_yours ?? [])[0])}</span>}
        </Row>
        {(item.counterpart || typeof item.orders_affected === "number") && (
          <Row label="Touches">
            {[item.counterpart, typeof item.orders_affected === "number" ? plural(item.orders_affected, "customer order") : null].filter(Boolean).join(" · ")}
          </Row>
        )}
      </dl>
    </section>
  );
}
