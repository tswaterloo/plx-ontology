"use client";

// The record at a glance for a type with no page of its own (an item, a lot, a person, a
// document); the header above says what it is and its state. Then: who is on it, what it links to, what last changed
// and what to do next. A question with nothing on record says so in words; the line is
// never left out, so a thin record reads as thin rather than as finished.

import Link from "next/link";
import { formatDate, objectHref } from "@/lib/format";
import type { ObjectPage } from "@/lib/types";

/** The types whose pages carry their own design (figures, steps, orders, margin). */
export const DESIGNED_TYPES = new Set(["PurchaseOrder", "SalesOrder", "AssemblyOrder", "ManufacturingOrder", "Customer", "Supplier", "Vendor"]);

export interface Answer {
  q: string;
  a: string;
  href?: string;
  empty: boolean;
}

function latest(obj: ObjectPage) {
  const dated = [...(obj.timeline ?? [])].filter((e) => e.at && !Number.isNaN(Date.parse(e.at)));
  dated.sort((a, b) => Date.parse(b.at!) - Date.parse(a.at!));
  return dated[0] ?? null;
}

export function answersOf(obj: ObjectPage): Answer[] {
  const people = obj.people ?? [];
  const related = obj.related ?? [];
  const linked = obj.linked ?? [];
  const last = latest(obj);
  const proposal = (obj.proposals ?? [])[0];
  const relatedWords = [...related.slice(0, 3).map((r) => r.label), ...linked.slice(0, Math.max(0, 3 - related.length)).map((l) => l.label)];
  const linkCount = related.length + linked.length;
  const ask = `/ask?${new URLSearchParams({ q: `What should I know about ${obj.label}?` }).toString()}`;
  return [
    {
      q: "Who is on it",
      a: people.length ? people.slice(0, 3).map((p) => `${p.name} (${p.role}${p.organisation ? `, ${p.organisation}` : ""})`).join("; ") + (people.length > 3 ? `; ${people.length - 3} more` : "") : "No person named on record",
      empty: people.length === 0,
    },
    {
      q: "Linked to",
      a: linkCount ? `${relatedWords.join(", ")}${linkCount > relatedWords.length ? ` and ${linkCount - relatedWords.length} more` : ""}` : "Nothing linked yet; explore the links below",
      href: related.length === 1 ? objectHref(related[0].type, related[0].object_id) : undefined,
      empty: linkCount === 0,
    },
    { q: "Last change", a: last ? `${last.title}, ${formatDate(last.at)}` : "No change recorded", empty: !last },
    proposal ? { q: "Next step", a: proposal.title, empty: false } : { q: "Next step", a: "Ask what matters about it", href: ask, empty: false },
  ];
}

export function OnRecord({ obj }: { obj: ObjectPage }) {
  if (DESIGNED_TYPES.has(obj.type)) return null;
  const rows = answersOf(obj);
  return (
    <section aria-label="On record" data-testid="on-record">
      <dl className="grid grid-cols-[132px_minmax(0,1fr)] border-t border-border text-[13px] leading-5">
        {rows.map((r) => (
          <div key={r.q} className="contents">
            <dt className="border-b border-border py-1.5 text-muted">{r.q}</dt>
            <dd className={`border-b border-border py-1.5 ${r.empty ? "text-faint" : ""}`}>
              {r.href ? (
                <Link href={r.href} className="text-accent hover:underline">
                  {r.a}
                </Link>
              ) : (
                r.a
              )}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
