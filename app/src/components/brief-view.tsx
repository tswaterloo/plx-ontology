"use client";

import { MessageCircle } from "lucide-react";
import Link from "next/link";
import type { Brief, BriefFigure, BriefRow, BriefSection } from "@/lib/types";
import { formatDate, formatMoney, objectHref, readable, safeHref } from "@/lib/format";
import { SourceChips } from "./source-chip";
import { PageHeader } from "./ui/page-header";
import { Section } from "./ui/section";

/** The Monday `weeks` weeks before the week of `day`, as YYYY-MM-DD. */
export function mondayBefore(day: Date, weeks: number): string {
  const d = new Date(Date.UTC(day.getFullYear(), day.getMonth(), day.getDate()));
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7) - 7 * weeks);
  return d.toISOString().slice(0, 10);
}

/** A figure's value in its own unit: money with its currency, a percent, a count. */
export function figureText(f: Pick<BriefFigure, "value" | "unit" | "currency">): string {
  if (f.value == null) return "Not read";
  if (f.unit === "money") return f.currency ? formatMoney(f.value, f.currency) : f.value;
  if (f.unit === "percent") return `${f.value}%`;
  return f.value;
}

/** A week-over-week change as words ("+3 vs 7", "-2.5 pts vs 90%"). */
export function deltaText(f: Pick<BriefFigure, "delta" | "previous" | "unit" | "currency">): string | null {
  if (f.delta == null || f.previous == null) return null;
  const before = figureText({ value: f.previous, unit: f.unit, currency: f.currency });
  return `${f.delta}${f.unit === "percent" ? " pts" : ""} vs ${before} the week before`;
}

function Row({ row }: { row: BriefRow }) {
  const href = row.object ? safeHref(row.object.open_url) ?? objectHref(row.object.type, row.object.object_id) : null;
  return (
    <li className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-2 text-[13px]" data-testid="brief-row">
      <div className="min-w-0 space-y-0.5">
        {href ? (
          <Link href={href} className="font-medium hover:underline">
            {row.label}
          </Link>
        ) : (
          <span className="font-medium">{row.label}</span>
        )}
        {(row.who || row.detail) && (
          <p className="text-muted">
            {row.who && <span data-testid="brief-row-who">{row.who}</span>}
            {row.who && row.detail ? " · " : null}
            {row.detail}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-3 text-muted">
        {row.amount && <span className="num text-fg">{formatMoney(row.amount.amount, row.amount.currency)}</span>}
        {row.date && <span>{formatDate(row.date, { short: true })}</span>}
        <SourceChips sources={row.sources} max={2} />
      </div>
    </li>
  );
}

function Figure({ f }: { f: BriefFigure }) {
  const delta = deltaText(f);
  const missing = f.value == null;
  // "Not read" already says what a "could not be read" note would
  const note = f.note && !(missing && /^could not be read\.?$/i.test(f.note.trim())) ? f.note : null;
  return (
    <div className="rounded-lg border border-border p-3" data-testid="brief-figure">
      <p className="text-meta text-muted">{f.label}</p>
      <p className={missing ? "py-1 text-[15px] leading-5 font-medium text-muted" : "text-[20px] font-semibold tabular-nums"}>{figureText(f)}</p>
      {delta && <p className="text-[12px] text-muted">{delta}</p>}
      {note && <p className="text-[12px] text-muted">{note}</p>}
      <div className="mt-1">
        <SourceChips sources={f.sources} max={2} />
      </div>
    </div>
  );
}

const EMPTY: Record<string, string> = {
  empty: "Nothing here you may read.",
  finance_marked: "Finance-marked: shown only to readers who hold finance.",
  unread: "Could not be read; this section may still have items.",
  blocked: "Not shown: the people exclusion is not loaded; this section may still have items.",
};

/** The section's own note already says what the empty line would (unread may still have items; finance only): said once. */
function saidInNote(section: BriefSection): boolean {
  const note = (section.note ?? "").toLowerCase();
  if (!note) return false;
  if (section.state === "finance_marked") return note.includes("finance");
  if (section.state === "unread" || section.state === "blocked") return note.includes("may still have");
  return false;
}

/** `repeat`: the section above said this same note; it is said once, then pointed at. */
export function BriefSectionView({ section, repeat = false }: { section: BriefSection; repeat?: boolean }) {
  const figures = section.figures ?? [];
  const rows = section.rows ?? [];
  const more = section.more ?? 0;
  return (
    <Section title={section.title} testId={`brief-section-${section.key}`}>
      {section.note && <p className="mb-2 text-[12px] text-muted">{repeat ? (section.state === "finance_marked" ? "Finance-marked too." : "Not read either, for the same reason.") : readable(section.note)}</p>}
      {section.state === "partial" && <p className="mb-2 text-[12px] text-muted">A read limit was reached: this section may be incomplete.</p>}
      {figures.length > 0 && (
        <div className="mb-2 grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2">
          {figures.map((f, i) => (
            <Figure key={`${f.label}-${i}`} f={f} />
          ))}
        </div>
      )}
      {rows.length > 0 ? (
        <ul className="divide-y divide-border">
          {rows.map((r, i) => (
            <Row key={`${r.label}-${r.object?.object_id ?? i}`} row={r} />
          ))}
        </ul>
      ) : (
        figures.length === 0 && !saidInNote(section) && <p className="text-[13px] text-muted">{EMPTY[section.state] ?? EMPTY.empty}</p>
      )}
      {more > 0 && <p className="mt-2 text-[12px] text-muted">{more} more not listed.</p>}
    </Section>
  );
}

/** A section read in full with nothing in it and no note of its own: said in one line under the
 * brief, not one "nothing here" each. A section with a note keeps its heading, so the note is never lost. */
function quiet(s: BriefSection): boolean {
  return (s.state === "empty" || s.state === "shown") && !(s.rows ?? []).length && !(s.figures ?? []).length && !s.more && !s.note;
}

function listWords(xs: string[]): string {
  return xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} or ${xs[xs.length - 1]}`;
}

/** Two sections in a row held back for one stated reason (unread, blocked, finance only). */
function sameUnreadNote(prev: BriefSection | undefined, s: BriefSection): boolean {
  return !!prev && !!s.note && prev.note === s.note && (s.state === "unread" || s.state === "blocked" || s.state === "finance_marked") && prev.state === s.state;
}

/** One brief (meeting prep or weekly review): its sections, parties and how it was read. */
export function BriefView({ brief, level = 1 }: { brief: Brief; eyebrow?: string; level?: 1 | 2 }) {
  const subject = brief.subject;
  const parties = brief.parties ?? [];
  const possible = brief.possible_parties ?? [];
  const ask = brief.summary_question
    ? `/ask?q=${encodeURIComponent(brief.summary_question)}${subject ? `&object=${encodeURIComponent(subject.object_id)}` : ""}`
    : null;
  return (
    <div className="space-y-4" data-testid="brief">
      <PageHeader
        level={level}
        title={readable(brief.title)}
        sub={
          // the week's date is said once: in the title when it names it, else here
          (brief.when && !readable(brief.title).includes(formatDate(brief.when)) ? `${formatDate(brief.when)} · read ${formatDate(brief.as_of)}` : `Read ${formatDate(brief.as_of)}`) +
          (brief.stored_at ? ` · kept ${formatDate(brief.stored_at)}` : "")
        }
        actions={
          ask && (
            <Link href={ask} className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium" title="Ask for a checked summary">
              <MessageCircle className="size-[15px]" strokeWidth={2} aria-hidden />
              Summarise
            </Link>
          )
        }
      />
      {(parties.length > 0 || possible.length > 0) && (
        <p className="text-[13px]">
          {parties.length > 0 && (
            <>
              With{" "}
              {parties.map((p, i) => (
                <span key={p.object_id}>
                  {i > 0 && ", "}
                  <Link className="font-medium hover:underline" href={safeHref(p.open_url) ?? objectHref(p.type, p.object_id)}>
                    {p.label}
                  </Link>
                </span>
              ))}
              .{" "}
            </>
          )}
          {possible.length > 0 && (
            <span className="text-muted">
              Possibly also (matched by email domain only, not read):{" "}
              {possible.map((p, i) => (
                <span key={p.object_id}>
                  {i > 0 && ", "}
                  <Link className="hover:underline" href={`/meetings/brief/${encodeURIComponent(p.type)}/${encodeURIComponent(p.object_id)}`}>
                    {p.label}
                  </Link>
                </span>
              ))}
            </span>
          )}
        </p>
      )}
      <div className="grid gap-4">
        {brief.sections.map((section, i) =>
          quiet(section) ? null : (
            <BriefSectionView key={section.key} section={section} repeat={sameUnreadNote(brief.sections[i - 1], section)} />
          ),
        )}
      </div>
      {brief.sections.some(quiet) && (
        <p className="text-[13px] text-muted" data-testid="brief-quiet">
          Nothing you may read under {listWords(brief.sections.filter(quiet).map((s) => s.title))}.
        </p>
      )}
      <p className="text-[12px] text-muted">{brief.basis}</p>
    </div>
  );
}
