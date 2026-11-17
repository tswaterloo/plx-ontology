"use client";

// A record's situation in one band under its header: the two records it is read against
// (per type: an order's customer and source, a PO's supplier and what it is needed for, a
// lot's receipt and use, a shipment's or invoice's order and party), who is on it and what
// last changed. Each cell says in words when nothing is on record.

import Link from "next/link";
import { formatDate, objectHref, recordLabel } from "@/lib/format";
import type { ObjectPage, ObjectRef } from "@/lib/types";

/** Each record type's first two cells: the records it is read against, by link type. */
const HEADS: Record<string, [string, string[], string][]> = {
  SalesOrder: [["Customer", ["Customer"], "Customer not linked"], ["Made or bought on", ["AssemblyOrder", "ManufacturingOrder", "PurchaseOrder"], "No production or purchase order linked"]],
  AssemblyOrder: [["For", ["SalesOrder", "Customer"], "No customer order linked"], ["Materials from", ["PurchaseOrder"], "No purchase order linked"]],
  ManufacturingOrder: [["For", ["SalesOrder", "Customer"], "No customer order linked"], ["Materials from", ["PurchaseOrder"], "No purchase order linked"]],
  PurchaseOrder: [["Supplier", ["Supplier"], "Supplier not linked"], ["Needed for", ["SalesOrder", "AssemblyOrder", "ManufacturingOrder"], "No customer or production order linked"]],
  Lot: [["Received on", ["PurchaseOrder", "Supplier"], "No purchase order linked"], ["Used in", ["AssemblyOrder", "ManufacturingOrder", "SalesOrder", "Shipment"], "Not linked to an order or shipment"]],
  Shipment: [["Order", ["SalesOrder"], "No customer order linked"], ["Customer", ["Customer"], "Customer not linked"]],
  SalesInvoice: [["Order", ["SalesOrder", "Shipment"], "No order or shipment linked"], ["Customer", ["Customer"], "Customer not linked"]],
  PurchaseInvoice: [["Purchase order", ["PurchaseOrder"], "No purchase order linked"], ["Supplier", ["Supplier"], "Supplier not linked"]],
};

/** The record types this band is for. */
export const SITUATION_TYPES = new Set(Object.keys(HEADS));

export interface Cell {
  label: string;
  refs: ObjectRef[];
  text?: string;
  empty: string;
}

const of = (obj: ObjectPage, ...types: string[]) => (obj.related ?? []).filter((r) => types.includes(r.type));

/** The party cells a counterpart can fill when the related list does not carry it. */
const PARTY = new Set(["Supplier", "Customer"]);

export function cellsOf(obj: ObjectPage, party?: ObjectRef | null): Cell[] {
  const people = obj.people ?? [];
  const dated = (obj.timeline ?? []).filter((e) => e.at && !Number.isNaN(Date.parse(e.at)));
  dated.sort((a, b) => Date.parse(b.at!) - Date.parse(a.at!));
  const last = dated[0];
  const head: Cell[] = (HEADS[obj.type] ?? []).map(([label, types, empty]) => {
    const refs = of(obj, ...types);
    // the header's counterpart (a PO's supplier, an invoice's customer) when no link names it
    const fill = refs.length === 0 && PARTY.has(label) && party?.object_id && party.type === label ? [party] : refs;
    return { label, refs: fill, empty };
  });
  return [
    ...head,
    {
      label: "Who",
      refs: [],
      text: people.length ? people.slice(0, 2).map((p) => `${p.name} (${p.role})`).join(", ") + (people.length > 2 ? ` +${people.length - 2}` : "") : undefined,
      empty: "No one named on record",
    },
    { label: "Last change", refs: [], text: last ? `${last.title}, ${formatDate(last.at)}` : undefined, empty: "No change recorded" },
  ];
}

export function Situation({ obj, party }: { obj: ObjectPage; party?: ObjectRef | null }) {
  if (!SITUATION_TYPES.has(obj.type)) return null;
  const cells = cellsOf(obj, party);
  return (
    <section aria-label="Situation" data-testid="situation">
      <dl className="grid grid-cols-2 border-y border-border lg:grid-cols-4">
        {cells.map((c) => {
          const empty = c.refs.length === 0 && !c.text;
          return (
            <div key={c.label} className="min-w-0 border-border px-3 py-2 max-lg:odd:pl-0 lg:border-l lg:first:border-l-0 lg:first:pl-0">
              <dt className="text-[12px] leading-4 text-muted">{c.label}</dt>
              <dd className={`mt-0.5 text-[13px] leading-5 ${empty ? "text-faint" : ""}`}>
                {c.refs.length > 0 ? (
                  <span className="flex flex-wrap gap-x-2">
                    {c.refs.slice(0, 3).map((r) => (
                      <Link key={r.object_id} href={objectHref(r.type, r.object_id)} className="truncate font-medium hover:underline">
                        {recordLabel(r.type, r.label)}
                      </Link>
                    ))}
                    {c.refs.length > 3 && <span className="text-muted">+{c.refs.length - 3}</span>}
                  </span>
                ) : (
                  (c.text ?? c.empty)
                )}
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}
