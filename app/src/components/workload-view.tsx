import type { Workload } from "@/lib/types";

// Who is overloaded: counts per team for everyone; per person only your own counts and
// those of the teams the owner granted you. Counts only, never message content.
export function WorkloadView({ data }: { data: Workload }) {
  const teams = data.teams ?? [];
  const people = data.people ?? [];
  return (
    <div className="space-y-4" data-testid="workload">
      <h1 className="text-lg font-semibold">Workload</h1>
      <table className="w-full text-[13px]" data-testid="workload-teams">
        <thead>
          <tr className="text-left text-muted"><th>Team</th><th>Awaiting reply</th><th>Open tasks</th><th>Alerts</th></tr>
        </thead>
        <tbody>
          {teams.map((t) => (
            <tr key={t.team}><td>{t.team === "not_assigned" ? "Not assigned" : t.team}</td><td>{t.suppressed || t.awaiting_reply == null ? "Not shown (under 3 people)" : t.awaiting_reply}</td><td>{t.tasks}</td><td>{t.alerts}</td></tr>
          ))}
        </tbody>
      </table>
      {people.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-[14px] font-medium">
            {(data.granted_teams ?? []).length > 0 ? `People of ${(data.granted_teams ?? []).join(", ")}` : "Your counts"}
          </h2>
          <table className="w-full text-[13px]" data-testid="workload-people">
            <thead>
              <tr className="text-left text-muted"><th>Person</th><th>Team</th><th>Awaiting reply</th><th>Open tasks</th><th>Alerts</th></tr>
            </thead>
            <tbody>
              {people.map((p) => (
                <tr key={p.person}><td>{p.self ? "You" : p.person}</td><td>{p.team === "not_assigned" ? "Not assigned" : p.team}</td><td>{p.awaiting_reply}</td><td>{p.tasks}</td><td>{p.alerts}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
