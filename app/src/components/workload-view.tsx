import Link from "next/link";
import { useEffect, useState } from "react";
import { getOwnerWorkloadItems } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { OwnerWorkload, OwnerWorkloadBucket, OwnerWorkloadItems, Workload } from "@/lib/types";

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

// Per person, for the owner only (the page asks for it and shows nothing on a refusal).
// Click a person's count to see the items behind it. Beside mail, Teams and tasks it counts the
// records each person created or changed per captured system; the note names exactly which
// systems are covered. It undercounts floor and lab work.
export function OwnerWorkloadView({ data }: { data: OwnerWorkload }) {
  const [open, setOpen] = useState<{ person: string; bucket: OwnerWorkloadBucket } | null>(null);
  const count = (person: string, bucket: OwnerWorkloadBucket, n: number) =>
    n > 0 ? (
      <button type="button" className="underline hover:no-underline" data-testid={`drill-${person}-${bucket}`}
        onClick={() => setOpen({ person, bucket })}>{n}</button>
    ) : <>{n}</>;
  return (
    <section className="space-y-2" data-testid="owner-workload">
      <h2 className="text-[14px] font-medium">Each person (owner only)</h2>
      <p className="text-[12px] text-muted" data-testid="owner-workload-note">{data.note}</p>
      <table className="w-full text-[13px]">
        <thead>
          <tr className="text-left text-muted"><th>Person</th><th>Team</th><th>Awaiting reply</th><th>Open tasks</th><th>Alerts</th><th>Alerts closed ({data.window_days} days)</th><th>Records in systems ({data.window_days} days)</th></tr>
        </thead>
        <tbody>
          {(data.people ?? []).map((p) => (
            <tr key={p.person}><td>{p.person}</td><td>{p.team === "not_assigned" ? "Not assigned" : p.team}</td><td>{count(p.person, "awaiting_reply", p.awaiting_reply)}</td><td>{count(p.person, "tasks", p.tasks)}</td><td>{count(p.person, "alerts", p.alerts)}</td><td>{p.alerts_closed}</td><td data-testid={`systems-${p.person}`}>{systemsText(p.systems)}</td></tr>
          ))}
        </tbody>
      </table>
      {open && <OwnerItems key={`${open.person}/${open.bucket}`} person={open.person} bucket={open.bucket} onClose={() => setOpen(null)} />}
    </section>
  );
}

const SYSTEM_WORDS: Record<string, string> = {
  bc: "Business Central", portal: "Portal", sage: "Sage", sharepoint: "SharePoint",
  mission_control: "Mission Control", planner: "Planner", calendar: "Calendar",
};

function systemsText(systems?: Record<string, number>): string {
  const parts = Object.entries(systems ?? {}).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  return parts.length ? parts.map(([s, n]) => `${SYSTEM_WORDS[s] ?? s} ${n}`).join(", ") : "None seen";
}

const BUCKET_WORDS: Record<OwnerWorkloadBucket, string> = {
  awaiting_reply: "Threads awaiting reply", tasks: "Open tasks", alerts: "Alerts",
};

// The items behind one count. The full message opens through its own page.
function OwnerItems({ person, bucket, onClose }: { person: string; bucket: OwnerWorkloadBucket; onClose: () => void }) {
  const [data, setData] = useState<OwnerWorkloadItems | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const ctl = new AbortController();
    getOwnerWorkloadItems(person, bucket, ctl.signal).then(setData, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [person, bucket]);
  return (
    <div className="space-y-2 rounded border p-3" data-testid="owner-items">
      <div className="flex items-center justify-between">
        <h3 className="text-[13px] font-medium">{BUCKET_WORDS[bucket]}: {person}</h3>
        <button type="button" className="text-[12px] underline" onClick={onClose}>Close</button>
      </div>
      {error ? <p className="text-[13px]">{plainError(error)}</p> : !data ? <p className="text-[13px] text-muted">Loading</p> : (
        <>
          {data.withheld > 0 && <p className="text-[12px] text-muted" data-testid="owner-items-withheld">{data.withheld} not shown: marked or not open to you.</p>}
          <ul className="space-y-2 text-[13px]">
            {(data.items ?? []).map((it) => (
              <li key={it.id} data-testid="owner-item">
                <div className="font-medium">{it.title}</div>
                <div className="text-muted">
                  {it.counterpart ? `${it.counterpart} · ` : ""}{it.age_days == null ? "age unknown" : `${it.age_days} days`} · waiting on {it.waiting_on}
                </div>
                {it.snippet && <div>{it.snippet}</div>}
                {it.record_id && it.record_type && (
                  <Link className="font-medium hover:underline" href={`/object/${encodeURIComponent(it.record_type)}/${encodeURIComponent(it.record_id)}`}>
                    Open
                  </Link>
                )}
              </li>
            ))}
          </ul>
          {data.truncated && <p className="text-[12px] text-muted">Oldest {(data.items ?? []).length} of {data.total} shown.</p>}
        </>
      )}
    </div>
  );
}
