"use client";

import { StoredAge } from "@/components/ui/stored-age";
import { useRead } from "@/lib/use-read";
import { useMemo } from "react";
import { getAutopilot, getAutopilotDays } from "@/lib/api";
import { countdownText, workflowLabel } from "@/lib/autopilot";
import { cn } from "@/lib/cn";
import type { AutopilotArea, AutopilotScorecard, AutopilotScoreDay, AutopilotWorkflowRow } from "@/lib/types";

const dayLabel = (iso: string, today: string) =>
  iso === today
    ? "Today"
    : new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).replace("Sept", "Sep");

const COLS: { key: keyof Omit<AutopilotScoreDay, "day">; label: string }[] = [
  { key: "prepared", label: "Prepared" },
  { key: "accepted", label: "Accepted" },
  { key: "edited", label: "Edited" },
  { key: "rejected", label: "Rejected" },
];

/** What the autopilot prepared for you each day and what you did with it, from your own
 * work log: one row a day, the last week, quiet days left out. Hidden until it has
 * prepared anything. */
export function AutopilotScorecardPanel({ days = 7 }: { days?: number }) {
  const { data, at } = useRead<AutopilotScorecard>(`/api/work-log/autopilot-days?days=${days}`, (s) => getAutopilotDays(days, s));
  // your own drafted workflows and their gate count (the autopilot area as you may see it)
  const { data: area, at: areaAt } = useRead<AutopilotArea>("/api/autopilot", (s) => getAutopilot(s));
  const gates = useMemo<AutopilotWorkflowRow[]>(
    () => (area?.workflows ?? []).filter((r) => r.draft && r.person && (area?.people ?? []).some((p) => p.you && p.id === r.person)),
    [area],
  );
  if (!data?.available) return null;
  const rows = data.days ?? [];
  const total = (k: (typeof COLS)[number]["key"]) => rows.reduce((n, d) => n + d[k], 0);
  if (total("prepared") + total("accepted") + total("edited") + total("rejected") === 0) return null;
  const decided = total("accepted") + total("edited") + total("rejected");
  const kept = total("accepted") + total("edited");
  const today = rows[0]?.day ?? "";
  return (
    <section aria-label="Autopilot scorecard" className="border-b border-border px-6 py-3" data-testid="autopilot-scorecard">
      <StoredAge at={at ?? areaAt} />
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-[12px] font-medium text-muted">Assist, last {rows.length} days</h2>
        <p className="num text-[12px] text-muted">
          {decided ? `${kept} of ${decided} decided items used (${Math.round((kept * 100) / decided)}%), ${total("edited")} after an edit` : "Nothing decided yet"}
        </p>
      </div>
      <table className="mt-1 w-full max-w-xl text-left text-[12px]">
        <thead className="text-[11px] text-faint">
          <tr>
            <th className="py-0.5 pr-3 font-normal">Day</th>
            {COLS.map((c) => (
              <th key={c.key} className="py-0.5 pr-3 text-right font-normal">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.filter((d) => COLS.some((c) => d[c.key] > 0)).map((d) => (
            <tr key={d.day} className={cn(d.day === today && "font-medium")}>
              <td className="py-0.5 pr-3">{dayLabel(d.day, today)}</td>
              {COLS.map((c) => (
                <td key={c.key} className={cn("num py-0.5 pr-3 text-right", d[c.key] === 0 && "text-faint", c.key === "rejected" && d[c.key] > 0 && "text-risk")}>
                  {d[c.key] || "–"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {gates.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-[12px]" aria-label="Checks per workflow" data-testid="scorecard-gates">
          {gates.map((r) => (
            <li key={`${r.role}/${r.workflow}`} className="flex gap-2">
              <span className="w-40 shrink-0 font-medium">{workflowLabel(r.workflow)}</span>
              <span className="num text-muted">{countdownText(r.countdown) ?? "Not enough checked yet"}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
