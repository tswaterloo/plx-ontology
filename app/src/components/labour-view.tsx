import type { LabourPage } from "@/lib/types";

/** Department and week labour hours: totals only (hours, overtime, headcount-days), the
 * overtime trend and spikes. Never a person; the server sends nothing per person. */
export function LabourView({ page }: { page: LabourPage }) {
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
      <h1 className="text-lg font-semibold">Labour hours</h1>
      {page.alerts.length > 0 && (
        <ul aria-label="Overtime spikes" className="space-y-1">
          {page.alerts.map((a) => (
            <li key={a.department + a.week} className="rounded-md border border-amber-400 px-3 py-2 text-[13px]">
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
      <p className="text-meta text-muted">{page.note} Hours only; pay is never read.</p>
    </div>
  );
}
