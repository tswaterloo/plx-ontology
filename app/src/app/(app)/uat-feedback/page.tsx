"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { getUatFeedback } from "@/lib/api";
import { formatDate, plural } from "@/lib/format";
import { useRead } from "@/lib/use-read";
import { noticeKind, plainError } from "@/lib/errors";
import type { UatFeedback, UatItem } from "@/lib/types";

const STATUSES: [string, string][] = [
  ["", "All"],
  ["open", "Open"],
  ["fixed", "Fixed"],
  ["answered", "Answered"],
  ["unknown", "No status"],
];
const NO_PRIORITY = "No priority";

// The lists word a level and then what it means ("Critical — Blocks work"): the level counts.
function levelOf(word: string | null | undefined): string {
  return (word ?? "").trim().split(/\s+[-\u2013\u2014]+\s+/)[0];
}

function priorityRank(word: string | null | undefined): number {
  const w = levelOf(word).toLowerCase();
  if (["critical", "blocker", "urgent"].includes(w)) return 0;
  if (["high", "major"].includes(w)) return 1;
  return 2;
}

function itemHref(record: string): string {
  return `/object/${encodeURIComponent(record.split(":")[0])}/${encodeURIComponent(record)}`;
}

// The MRP UAT feedback lists as you may read them, read only: totals by status and by
// priority (open items), filters, a title search and aging for open items. Items you may
// not open are counted, never listed.
export default function UatFeedbackPage() {
  const [status, setStatus] = useState("");
  const { data, error, retry } = useRead<UatFeedback>(`uat-feedback:${status}`, (signal) => getUatFeedback(status || undefined, signal));
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="UAT feedback" />
      <div className="mx-auto w-full max-w-5xl px-6 py-6">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : data ? (
          <UatView data={data} status={status} setStatus={setStatus} />
        ) : (
          <Pending page label="Reading the UAT feedback lists" />
        )}
      </div>
    </div>
  );
}

function UatView({ data, status, setStatus }: { data: UatFeedback; status: string; setStatus: (s: string) => void }) {
  const [priority, setPriority] = useState("");
  const [search, setSearch] = useState("");
  const [order, setOrder] = useState<"triage" | "newest" | "oldest">("triage");
  const items: UatItem[] = data.items ?? [];
  const totals = data.totals ?? {};
  const prios = data.open_priorities ?? [];
  const multi = (data.lists ?? []).length > 1;

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = items.filter(
      (i) => (!priority || (levelOf(i.priority) || NO_PRIORITY) === priority) && (!q || (i.title ?? "").toLowerCase().includes(q)),
    );
    const day = (i: UatItem) => i.modified ?? "";
    return [...list].sort((a, b) => {
      if (order === "newest") return day(b).localeCompare(day(a));
      if (order === "oldest") return day(a).localeCompare(day(b));
      return (a.status === "open" ? 0 : 1) - (b.status === "open" ? 0 : 1) || priorityRank(a.priority) - priorityRank(b.priority) || day(b).localeCompare(day(a));
    });
  }, [items, priority, search, order]);

  if (!data.served) {
    return <p className="text-[13px] text-muted" data-testid="uat-unserved">The UAT feedback lists could not be read here yet. Try again shortly.</p>;
  }
  const word = (n: number, w: string) => `${n} ${w}`;
  const statusLine = [
    word(totals.open ?? 0, "open"),
    word(totals.fixed ?? 0, "fixed"),
    word(totals.answered ?? 0, "answered"),
    ...((totals.unknown ?? 0) > 0 ? [word(totals.unknown ?? 0, "with no status")] : []),
  ].join(", ");
  const sortedPrios = [...prios].sort((a, b) => priorityRank(a.word) - priorityRank(b.word));
  const filtered = Boolean(priority || search.trim());

  return (
    <div className="space-y-4" data-testid="uat-page">
      {data.total === 0 ? (
        <p className="text-[13px] text-muted" data-testid="uat-empty">
          No UAT feedback items you can open.{(data.withheld ?? 0) > 0 ? ` ${plural(data.withheld ?? 0, "item")} you may not open ${data.withheld === 1 ? "is" : "are"} not shown.` : ""}
        </p>
      ) : (
        <>
          <p className="text-[13px] text-fg" data-testid="uat-totals">
            {plural(data.total, "item")}: {statusLine}.
          </p>
          <p className="text-[13px] text-fg" data-testid="uat-priorities">
            Open by priority:{" "}
            {sortedPrios.length === 0
              ? "none open."
              : sortedPrios.map((p, k) => (
                  <span key={p.word}>
                    {k > 0 ? ", " : ""}
                    <button type="button" className={`hover:underline ${priority === p.word ? "font-medium underline" : ""} ${priorityRank(p.word) < 2 ? "font-medium" : ""}`} onClick={() => setPriority(priority === p.word ? "" : p.word)}>
                      {p.count} {p.word}
                    </button>
                  </span>
                ))}
            {sortedPrios.length > 0 ? "." : ""}
          </p>
        </>
      )}
      {(data.withheld ?? 0) > 0 && data.total > 0 && (
        <p className="text-[12px] text-muted" data-testid="uat-withheld">
          {plural(data.withheld ?? 0, "item")} you may not open {data.withheld === 1 ? "is" : "are"} not counted above and not shown.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <label className="flex items-center gap-1">
          <span className="text-muted">Status</span>
          <select aria-label="Status" className="rounded border border-border bg-bg px-1 py-0.5" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          <span className="text-muted">Priority</span>
          <select aria-label="Priority" className="rounded border border-border bg-bg px-1 py-0.5" value={priority} onChange={(e) => setPriority(e.target.value)}>
            <option value="">All</option>
            {sortedPrios.map((p) => (
              <option key={p.word} value={p.word}>
                {p.word}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          <span className="text-muted">Sort</span>
          <select aria-label="Sort" className="rounded border border-border bg-bg px-1 py-0.5" value={order} onChange={(e) => setOrder(e.target.value as typeof order)}>
            <option value="triage">Open first, by priority</option>
            <option value="newest">Newest change</option>
            <option value="oldest">Oldest change</option>
          </select>
        </label>
        <input aria-label="Search titles" type="search" placeholder="Search titles" className="rounded border border-border bg-bg px-2 py-0.5" value={search} onChange={(e) => setSearch(e.target.value)} />
      </div>
      {data.cut && (
        <p className="text-[12px] text-muted" data-testid="uat-cut">
          Showing {items.length} of {data.matched}.
        </p>
      )}
      {data.total > 0 && shown.length === 0 ? (
        <p className="text-[13px] text-muted" data-testid="uat-none">
          {filtered ? "No item matches that priority or search." : "No items with that status."}
        </p>
      ) : (
        shown.length > 0 && (
          <table className="w-full text-left text-[13px]" data-testid="uat-table">
            <thead className="text-muted">
              <tr>
                <th className="py-1 pr-3 font-normal">Item</th>
                {multi && <th className="py-1 pr-3 font-normal">List</th>}
                <th className="py-1 pr-3 font-normal">Status</th>
                <th className="py-1 pr-3 font-normal">Priority</th>
                <th className="py-1 pr-3 font-normal">Changed</th>
                <th className="py-1 font-normal">Open for</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((i) => (
                <tr key={i.record} className="border-t border-border align-top" data-testid="uat-row">
                  <td className="max-w-[28rem] py-1 pr-3">
                    <Link className="break-words font-medium hover:underline" href={itemHref(i.record)}>
                      {i.title?.trim() || "(no title)"}
                    </Link>
                  </td>
                  {multi && <td className="py-1 pr-3 text-muted">{i.list}</td>}
                  <td className="py-1 pr-3">
                    {i.status_word || i.status}
                    {i.resolution_word && i.resolution_word !== i.status_word ? <span className="text-muted"> · {i.resolution_word}</span> : null}
                  </td>
                  <td className="py-1 pr-3">{i.priority?.trim() || <span className="text-muted">none</span>}</td>
                  <td className="py-1 pr-3 text-muted">{i.modified ? formatDate(i.modified, { short: true }) : "not dated"}</td>
                  <td className="py-1 text-muted">{i.age_days == null ? "" : plural(i.age_days, "day")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}
    </div>
  );
}
