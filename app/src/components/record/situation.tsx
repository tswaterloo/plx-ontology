"use client";

// A record's situation in one band under its header: the two records it is read against
// (per type: an order's customer and source, a PO's supplier and what it is needed for, a
// lot's receipt and use, a shipment's or invoice's order and party), who is on it and what
// last changed. A cell shows only when its fact is on record; an empty band is not drawn.

import Link from "next/link";
import { formatDate, objectHref, recordLabel } from "@/lib/format";
import type { ObjectPage, ObjectRef } from "@/lib/types";
import { ShowAll } from "@/components/show-all";

/** Each record type's first two cells: the records it is read against, by link type. */
const HEADS: Record<string, [string, string[]][]> = {
  SalesOrder: [["Customer", ["Customer"]], ["Made or bought on", ["AssemblyOrder", "ManufacturingOrder", "PurchaseOrder"]]],
  AssemblyOrder: [["For", ["SalesOrder", "Customer"]], ["Materials from", ["PurchaseOrder"]]],
  ManufacturingOrder: [["For", ["SalesOrder", "Customer"]], ["Materials from", ["PurchaseOrder"]]],
  PurchaseOrder: [["Supplier", ["Supplier"]], ["Needed for", ["SalesOrder", "AssemblyOrder", "ManufacturingOrder"]]],
  Lot: [["Received on", ["PurchaseOrder", "Supplier"]], ["Used in", ["AssemblyOrder", "ManufacturingOrder", "SalesOrder", "Shipment"]]],
  Shipment: [["Order", ["SalesOrder"]], ["Customer", ["Customer"]]],
  SalesInvoice: [["Order", ["SalesOrder", "Shipment"]], ["Customer", ["Customer"]]],
  PurchaseInvoice: [["Purchase order", ["PurchaseOrder"]], ["Supplier", ["Supplier"]]],
};

/** The record types this band is for. */
export const SITUATION_TYPES = new Set(Object.keys(HEADS));

export interface Cell {
  label: string;
  refs: ObjectRef[];
  text?: string;
}

const of = (obj: ObjectPage, ...types: string[]) => (obj.related ?? []).filter((r) => types.includes(r.type));

/** The party cells a counterpart can fill when the related list does not carry it. */
const PARTY = new Set(["Supplier", "Customer"]);

export function cellsOf(obj: ObjectPage, party?: ObjectRef | null): Cell[] {
  const people = obj.people ?? [];
  // the record's own events only: a linked customer's orders are not this record's change
  const dated = (obj.timeline ?? []).filter((e) => e.own !== false && e.at && !Number.isNaN(Date.parse(e.at)));
  dated.sort((a, b) => Date.parse(b.at!) - Date.parse(a.at!));
  const last = dated[0];
  const head: Cell[] = (HEADS[obj.type] ?? []).map(([label, types]) => {
    const refs = of(obj, ...types);
    // the header's counterpart (a PO's supplier, an invoice's customer) when no link names it
    const fill = refs.length === 0 && PARTY.has(label) && party?.object_id && party.type === label ? [party] : refs;
    return { label, refs: fill };
  });
  return [
    ...head,
    {
      label: "Who",
      refs: [],
      text: people.length ? people.slice(0, 2).map((p) => `${p.name} (${p.role})`).join(", ") + (people.length > 2 ? ` +${people.length - 2}` : "") : undefined,
    },
    { label: "Last change", refs: [], text: last ? `${last.title}, ${formatDate(last.at)}` : undefined },
  ];
}

/** `named`: the party the record's title already names (its supplier or customer); a cell
 * holding only that party would say the title again, so it is left out. */
export function Situation({ obj, party, named }: { obj: ObjectPage; party?: ObjectRef | null; named?: string | null }) {
  if (!SITUATION_TYPES.has(obj.type)) return null;
  const same = (r: ObjectRef) => (!!party?.object_id && r.object_id === party.object_id) || (!!named && r.label.trim().toLowerCase() === named.trim().toLowerCase());
  const cells = cellsOf(obj, party).filter((c) => (c.refs.length > 0 || !!c.text) && !(PARTY.has(c.label) && c.refs.length > 0 && c.refs.every(same)));
  if (cells.length === 0) return null;
  return (
    <section aria-label="Situation" data-testid="situation">
      <dl className="grid grid-cols-2 border-y border-border lg:grid-cols-4">
        {cells.map((c) => {
          return (
            <div key={c.label} className="min-w-0 border-border px-3 py-2 max-lg:odd:pl-0 lg:border-l lg:first:border-l-0 lg:first:pl-0">
              <dt className="text-[12px] leading-4 text-muted">{c.label}</dt>
              <dd className="mt-0.5 text-[13px] leading-5">
                {c.refs.length > 0 ? (
                  <span className="flex flex-wrap gap-x-2">
                    <ShowAll items={c.refs} first={3}>{(r) => (
                      <Link key={r.object_id} href={objectHref(r.type, r.object_id)} className="truncate font-medium hover:underline">
                        {recordLabel(r.type, r.label)}
                      </Link>
                    )}</ShowAll>
                    {c.refs.length > 3 && <span className="text-muted">+{c.refs.length - 3}</span>}
                  </span>
                ) : (
                  c.text
                )}
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}
