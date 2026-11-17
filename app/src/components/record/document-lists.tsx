"use client";

// A customer's, supplier's or item's lists from what customers and suppliers sent: quality
// issues, forecasts and how they compared with orders, price sheets against invoices, and
// the questionnaire answers we gave. Rows come from the page's documents section only; a
// list states why it is empty, what it left out and what access it needs.

import Link from "next/link";
import { useMemo, useState } from "react";
import { ShowMore } from "@/components/ui/show-more";
import { cn } from "@/lib/cn";
import { formatDate } from "@/lib/format";
import type { DocList, DocRow, RecordDocuments } from "@/lib/types";
import { SectionTitle } from "./parts";

const SHOWN = 5;
const TONE: Record<string, string> = { bad: "text-late", warn: "text-warn", good: "text-ok" };
// what a list with no row says, by list
const NONE: Record<DocList["view"], string> = {
  supplier_issues: "quality issues",
  forecasts: "forecasts",
  forecast_accuracy: "scored forecast months",
  price_sheets: "price sheets compared with invoices",
  answer_bank: "questionnaire answers",
};

const chip = (on: boolean) =>
  cn(
    "inline-flex h-6 shrink-0 cursor-pointer items-center rounded-[3px] px-2 text-[12px]",
    on ? "bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover",
  );

function joinWords(words: string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} or ${words[words.length - 1]}`;
}

function Row({ r }: { r: DocRow }) {
  const day = formatDate(r.dated, { short: true });
  return (
    <li className="py-2" data-testid="doc-row">
      <div className="flex items-baseline justify-between gap-3">
        <span className="min-w-0 text-[13px] leading-5 break-words">{r.title}</span>
        {day && <span className="shrink-0 text-[12px] text-muted tabular-nums">{day}</span>}
      </div>
      {r.detail && <p className="mt-0.5 text-[13px] leading-5 text-muted break-words">{r.detail}</p>}
      {(r.figures ?? []).length > 0 && (
        <dl className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[12px] leading-4">
          {(r.figures ?? []).map((f, i) => (
            <div key={`${f.label}-${i}`} className="flex gap-1.5">
              <dt className="text-muted">{f.label}</dt>
              <dd className="tabular-nums">
                {f.value}
                {f.unit ? (f.unit === "%" ? "%" : ` ${f.unit}`) : ""}
              </dd>
            </div>
          ))}
        </dl>
      )}
      <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[12px] leading-4">
        {r.status && <span className={cn(r.tone ? TONE[r.tone] : "text-muted")}>{r.status}</span>}
        {(r.records ?? []).map((x) => (
          <Link key={x.object_id} href={x.open_url} className="text-muted hover:text-fg hover:underline">
            {x.label}
          </Link>
        ))}
        {r.source && (
          <Link href={r.source.open_url} className="text-muted hover:text-fg hover:underline" data-testid="doc-source">
            {r.source.label}
          </Link>
        )}
      </div>
    </li>
  );
}

export function OneList({ list }: { list: DocList }) {
  const [filter, setFilter] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const rows = list.rows ?? [];
  const states = useMemo(() => {
    const n = new Map<string, number>();
    for (const r of rows) if (r.status) n.set(r.status, (n.get(r.status) ?? 0) + 1);
    return [...n.entries()];
  }, [rows]);
  const shown = filter ? rows.filter((r) => r.status === filter) : rows;
  const visible = open ? shown : shown.slice(0, SHOWN);
  return (
    <section data-testid={`doc-list-${list.view}`}>
      <SectionTitle as="h3" aside={<span className="text-[12px] text-muted tabular-nums">{rows.length}</span>}>
        {list.title}
      </SectionTitle>
      {states.length > 1 && (
        <div className="mb-1 flex flex-wrap gap-1" role="group" aria-label={`Filter ${list.title.toLowerCase()}`}>
          <button type="button" className={chip(filter === null)} aria-pressed={filter === null} onClick={() => setFilter(null)}>
            All
          </button>
          {states.map(([s, n]) => (
            <button key={s} type="button" className={chip(filter === s)} aria-pressed={filter === s} onClick={() => setFilter(filter === s ? null : s)}>
              {s} <span className="ml-1 text-muted tabular-nums">{n}</span>
            </button>
          ))}
        </div>
      )}
      <ul className="divide-y divide-border">
        {visible.map((r, i) => (
          <Row key={`${r.source?.object_id ?? ""}-${r.title}-${i}`} r={r} />
        ))}
      </ul>
      <ShowMore hidden={shown.length - SHOWN} open={open} onToggle={() => setOpen((v) => !v)} className="mt-1" />
      <Notes list={list} />
    </section>
  );
}

function Notes({ list }: { list: DocList }) {
  const notes: string[] = [];
  if (list.withheld > 0) notes.push(`${list.withheld} left out: ${list.withheld === 1 ? "it cites" : "they cite"} a record you can't open`);
  if (!list.complete) notes.push("the read was cut, so some may be missing");
  if (notes.length === 0) return null;
  return <p className="mt-1 text-[12px] leading-4 text-muted">{notes.join("; ").replace(/^./, (c) => c.toUpperCase())}.</p>;
}

export function DocumentLists({ docs }: { docs: RecordDocuments | null | undefined }) {
  if (!docs) return null;
  const lists = docs.lists ?? [];
  const full = lists.filter((l) => (l.rows ?? []).length > 0);
  const empty = lists.filter((l) => (l.rows ?? []).length === 0 && !l.error && !l.needs);
  const unread = lists.filter((l) => (l.rows ?? []).length === 0 && (l.error || l.needs));
  if (lists.length === 0) return null;
  return (
    <section data-testid="doc-lists" id="sent-documents" className="space-y-6">
      <SectionTitle>Sheets and mail</SectionTitle>
      {full.map((l) => (
        <OneList key={l.view} list={l} />
      ))}
      {unread.map((l) => (
        <p key={l.view} className="text-[13px] leading-5 text-muted" data-testid={`doc-list-${l.view}-unread`}>
          {l.title}: {l.needs ? `not read; it needs ${l.needs} access, which you don't hold.` : l.error}
        </p>
      ))}
      {empty.length > 0 && (
        <p className="text-[13px] leading-5 text-muted" data-testid="doc-lists-empty">
          No {joinWords(empty.map((l) => NONE[l.view]))} name this record
          {empty.some((l) => l.withheld > 0) ? " that you can open" : ""}
          {empty.some((l) => !l.complete) ? " (the read was cut, so some may be missing)" : ""}.
        </p>
      )}
    </section>
  );
}
