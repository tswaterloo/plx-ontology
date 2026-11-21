"use client";

// The record at a glance for a type with no page of its own (an item, a lot, a person, a
// document); the header above says what it is and its state. Then: who is on it, what it links to, what last changed
// and what to do next. A question with nothing on record says so in words; the line is
// never left out, so a thin record reads as thin rather than as finished.

import Link from "next/link";
import { formatDate, namesOrCount, objectHref, plural, recordLabel } from "@/lib/format";
import { linkSections } from "@/lib/record";
import { SITUATION_TYPES } from "./situation";
import type { ObjectPage } from "@/lib/types";

/** The types whose pages carry their own design (figures, steps, orders, margin). */
export const DESIGNED_TYPES = new Set(["PurchaseOrder", "SalesOrder", "AssemblyOrder", "ManufacturingOrder", "Customer", "Supplier", "Vendor"]);

export interface Answer {
  q: string;
  a: string;
  href?: string;
  empty: boolean;
}

/** The record's own latest event: an event of a linked record shown beside it (a customer's
 * other orders on a shipment) is never the record's last change. */
export function latest(obj: Pick<ObjectPage, "timeline">) {
  const dated = [...(obj.timeline ?? [])].filter((e) => e.own !== false && e.at && !Number.isNaN(Date.parse(e.at)));
  dated.sort((a, b) => Date.parse(b.at!) - Date.parse(a.at!));
  return dated[0] ?? null;
}

export function answersOf(obj: ObjectPage): Answer[] {
  const people = obj.people ?? [];
  const related = obj.related ?? [];
  const linked = obj.linked ?? [];
  const last = latest(obj);
  // the records the Linked list beside the page shows, counted as it lists them
  const listed = (obj.links ?? []).length > 0
    ? Object.values(linkSections(obj)).flat().flatMap((s) => s.records)
    : [...new Map(related.map((r) => [r.object_id, r])).values()];
  const proposal = (obj.proposals ?? [])[0];
  // each name once, never the record's own (a Sage invoice's rows all carry its number)
  const own = obj.label.trim().toLowerCase();
  const names = [...new Set([...listed.map((r) => recordLabel(r.type, r.label).trim()), ...linked.map((l) => l.label.trim())])].filter((n) => n && n.toLowerCase() !== own);
  const linkCount = listed.length + linked.length;
  const ask = `/ask?${new URLSearchParams({ q: `What should I know about ${obj.label}?` }).toString()}`;
  return [
    {
      q: "Who is on it",
      // up to three by name and role; more are counted, each listed under People
      a: people.length === 0 ? "No person named on record" : people.length > 3 ? `${plural(people.length, "person", "people")} named on record` : people.map((p) => `${p.name} (${p.role}${p.organisation ? `, ${p.organisation}` : ""})`).join("; "),
      empty: people.length === 0,
    },
    {
      q: "Linked to",
      a: linkCount === 0 ? "Nothing linked yet; explore the links below" : names.length === 0 ? plural(linkCount, "linked record") : namesOrCount(names, "linked record"),
      href: related.length === 1 ? objectHref(related[0].type, related[0].object_id) : undefined,
      empty: linkCount === 0,
    },
    { q: "Last change", a: last ? `${last.title}, ${formatDate(last.at)}` : "No change recorded", empty: !last },
    proposal ? { q: "Next step", a: proposal.title, empty: false } : { q: "Next step", a: "Ask about it", href: ask, empty: false },
  ];
}

export function OnRecord({ obj }: { obj: ObjectPage }) {
  if (DESIGNED_TYPES.has(obj.type)) return null;
  // a row shows only when its fact is on record ("Next step" always has one); the last change
  // is said once, in the situation strip above, for a type that has one
  const rows = answersOf(obj).filter((r) => !r.empty && !(r.q === "Last change" && SITUATION_TYPES.has(obj.type)));
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
