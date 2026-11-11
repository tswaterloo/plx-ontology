"use client";

// An order's situation in one band under its header: what it is for, what it waits on, who
// is on it and what last changed. A production or customer order is read for these first;
// each cell says in words when nothing is on record.

import Link from "next/link";
import { formatDate, objectHref, recordLabel } from "@/lib/format";
import type { ObjectPage, ObjectRef } from "@/lib/types";

/** The order types this band is for. */
export const SITUATION_TYPES = new Set(["SalesOrder", "AssemblyOrder", "ManufacturingOrder"]);

export interface Cell {
  label: string;
  refs: ObjectRef[];
  text?: string;
  empty: string;
}

const of = (obj: ObjectPage, ...types: string[]) => (obj.related ?? []).filter((r) => types.includes(r.type));

export function cellsOf(obj: ObjectPage): Cell[] {
  const people = obj.people ?? [];
  const dated = (obj.timeline ?? []).filter((e) => e.at && !Number.isNaN(Date.parse(e.at)));
  dated.sort((a, b) => Date.parse(b.at!) - Date.parse(a.at!));
  const last = dated[0];
  const production = obj.type !== "SalesOrder";
  const head: Cell[] = production
    ? [
        { label: "For", refs: of(obj, "SalesOrder", "Customer"), empty: "No customer order linked" },
        { label: "Materials from", refs: of(obj, "PurchaseOrder"), empty: "No purchase order linked" },
      ]
    : [
        { label: "Customer", refs: of(obj, "Customer"), empty: "Customer not linked" },
        { label: "Made or bought on", refs: of(obj, "AssemblyOrder", "ManufacturingOrder", "PurchaseOrder"), empty: "No production or purchase order linked" },
      ];
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

export function Situation({ obj }: { obj: ObjectPage }) {
  if (!SITUATION_TYPES.has(obj.type)) return null;
  const cells = cellsOf(obj);
  return (
    <section aria-label="Situation" data-testid="situation">
      <dl className="grid grid-cols-2 border-y border-border lg:grid-cols-4">
        {cells.map((c) => {
          const empty = c.refs.length === 0 && !c.text;
          return (
            <div key={c.label} className="min-w-0 border-border px-3 py-2 first:pl-0 lg:border-l lg:first:border-l-0">
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
