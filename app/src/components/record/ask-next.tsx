"use client";

// A record's side panel when its header already holds every fact: the questions a person
// asks next about that kind of record, each one click into Ask scoped to this record.

import { CornerDownRight } from "lucide-react";
import Link from "next/link";

type Q = (label: string, party: string | null) => string;

const NEXT: Record<string, Q[]> = {
  PurchaseOrder: [
    (l) => `Which customer orders wait on ${l}?`,
    (l, p) => (p ? `What did ${p} last say about ${l}?` : `What was last said about ${l}?`),
    (l) => `Landed cost of ${l}`,
  ],
  SalesOrder: [(l) => `Is ${l} on track to ship?`, (l, p) => (p ? `What did we last tell ${p} about ${l}?` : `What did we last tell the customer about ${l}?`), (l) => `Margin on ${l}`],
  AssemblyOrder: [(l) => `What is ${l} waiting on?`, (l) => `Which customer order is ${l} for?`],
  Lot: [(l) => `Where was ${l} used?`, (l) => `Is ${l} on hold or near expiry?`],
  Shipment: [(l) => `What was in ${l}?`, (l) => `Was ${l} invoiced?`],
  SalesInvoice: [(l) => `Is ${l} paid?`, (l) => `Which shipment does ${l} bill?`],
  PurchaseInvoice: [(l) => `Does ${l} match its PO and receipt?`, (l) => `When is ${l} due to be paid?`],
};

export function questionsFor(type: string, label: string, party: string | null): string[] {
  return (NEXT[type] ?? [(l: string) => `What's the latest on ${l}?`]).map((q) => q(label, party));
}

export function AskNext({ type, objectId, label, party }: { type: string; objectId: string; label: string; party: string | null }) {
  return (
    <section data-testid="ask-next">
      <h3 className="mb-2 text-[12px] font-medium leading-4 text-muted">Ask next</h3>
      <ul className="-mx-2">
        {questionsFor(type, label, party).map((q) => (
          <li key={q}>
            <Link href={`/ask?q=${encodeURIComponent(q)}&object=${encodeURIComponent(objectId)}`} className="group flex min-h-8 items-start gap-2 rounded-md px-2 py-1 text-[13px] leading-5 hover:bg-hover">
              <CornerDownRight className="mt-[3px] size-3.5 shrink-0 text-faint group-hover:text-fg" strokeWidth={2} aria-hidden />
              <span className="min-w-0">{q}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
