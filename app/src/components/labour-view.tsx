import { useState } from "react";
import { getLabour } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { LabourPage } from "@/lib/types";

/** Department and week labour hours: totals only (hours, overtime, headcount-days), the
 * overtime trend and spikes. Never a person; the server sends nothing per person. */
export function LabourView({ page: first }: { page: LabourPage }) {
  // the last 12 weeks come first (the page was 355 KB with every week since 2023)
  const [every, setEvery] = useState<LabourPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const page = every ?? first;
  const earlier = async () => {
    setBusy(true);
    setProblem(null);
    try {
      setEvery(await getLabour(undefined, true));
    } catch (e) {
      setProblem(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  if (page.state !== "ready") {
    return (
      <p role="note" className="text-[13px] text-muted" data-testid="labour-note">
        {page.note}
      </p>
    );
  }
  const latest = new Map<string, LabourPage["weeks"][number]>();
  for (const w of page.weeks) if (!w.run) latest.set(w.department, w);
  const runs = page.weeks.filter((w) => w.run);
  return (
    <div className="space-y-4">
      {page.alerts.length > 0 && (
        <ul aria-label="Overtime spikes" className="space-y-1">
          {page.alerts.map((a) => (
            <li key={a.department + a.week} className="rounded-md bg-risk-soft px-3 py-2 text-[13px]">
              Overtime spike in {a.department}: {a.overtime_hours} h in the week of {a.week}, {a.rise_percent}% over its{" "}
              {a.baseline_weeks}-week median of {a.baseline_median} h.
            </li>
          ))}
        </ul>
      )}
      <table className="w-full text-[13px]" aria-label="Latest week by department">
        <thead>
          <tr className="text-left text-muted">
            <th>Department</th><th>Week of</th><th>Hours</th><th>Overtime</th><th>Headcount-days</th><th>Sheets</th>
          </tr>
        </thead>
        <tbody>
          {[...latest.values()].map((w) => (
            <tr key={w.department}>
              <td>{w.department}</td><td>{w.week}</td><td>{w.hours}</td><td>{w.overtime_hours ?? (w.overtime_basis === "fewer_than_3" ? "fewer than 3 people" : "not on the sheets")}</td>
              <td>{w.headcount_days ?? "not on the sheets"}</td><td>{w.sources.length}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {page.trend.map((t) => (
        <section key={t.department} aria-label={`${t.department} trend`}>
          <h2 className="text-[13px] font-medium">{t.department}: weekly hours (overtime)</h2>
          <ol className="flex flex-wrap gap-2 text-meta text-muted">
            {t.points.slice(-12).map((p) => (
              <li key={p.week}>{p.week}: {p.hours} ({p.overtime_hours ?? "n/a"})</li>
            ))}
          </ol>
        </section>
      ))}
      {runs.length > 0 && (
        <table className="w-full text-[13px]" aria-label="Hours by run or order">
          <thead><tr className="text-left text-muted"><th>Department</th><th>Week of</th><th>Run or order</th><th>Hours</th></tr></thead>
          <tbody>{runs.map((w) => (<tr key={w.department + w.week + w.run}><td>{w.department}</td><td>{w.week}</td><td>{w.run}</td><td>{w.hours}</td></tr>))}</tbody>
        </table>
      )}
      {page.older && (
        <p className="text-meta text-muted">
          The last 12 weeks are shown.{" "}
          <button type="button" className="underline cursor-pointer disabled:cursor-default" disabled={busy} onClick={earlier}>Show every week</button>
          {problem && <span role="alert"> {problem}</span>}
        </p>
      )}
      <p className="text-meta text-muted">{page.note} Hours only; pay is never read.</p>
    </div>
  );
}
