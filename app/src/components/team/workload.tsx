"use client";

// Team > Workload: who is overloaded. Per team, the threads awaiting a reply, open tasks and
// other alerts; per person only your own and the people of the teams the owner granted you.
// A team of fewer than three shows no reply total, as it would name one person. Counts only,
// never message content. The owner also gets the grant sheet: each person's team and the
// teams a manager sees person by person.

import { StoredAge } from "@/components/ui/stored-age";
import { useEffect, useMemo, useState } from "react";
import { useRead } from "@/lib/use-read";
import { Button } from "@/components/ui/button";
import { ErrorNotice, LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { useViewingAs } from "@/components/view-as";
import { getWorkload, getWorkloadGrants, setWorkloadGrants } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import type { Workload, WorkloadGrantPerson, WorkloadGrantSheet, WorkloadGrants } from "@/lib/types";

const NOT_ASSIGNED = "not_assigned";
const TEAM_CODE = /^[A-Z][A-Z0-9_]{0,31}$/;

/** A team code in words: "CUSTOMER_SERVICE" is "Customer service"; a two-letter word is an
 * initialism and keeps its capitals ("QA", "QC", "WH"), never "Qa". */
export function teamName(code: string): string {
  if (code === NOT_ASSIGNED) return "No team yet";
  return code
    .split("_")
    .filter(Boolean)
    .map((w, i) => (w.length <= 2 ? w.toUpperCase() : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase()))
    .join(" ");
}

/** A team code as the server keeps it: capitals, digits and underscores, starting with a letter. */
export function teamCode(text: string): string | null {
  const code = text.trim().toUpperCase().replace(/[\s-]+/g, "_");
  return TEAM_CODE.test(code) ? code : null;
}

function Num({ n, warn }: { n: number | null | undefined; warn?: boolean }) {
  if (n === null || n === undefined) return <span className="text-faint" title="Fewer than three people: a total would name one of them">·</span>;
  return <span className={cn("num", n > 0 && warn ? "font-medium text-risk" : n === 0 ? "text-faint" : undefined)}>{n}</span>;
}

export function WorkloadTables({ data }: { data: Workload }) {
  const teams = [...(data.teams ?? [])].sort((a, b) => (a.team === NOT_ASSIGNED ? 1 : b.team === NOT_ASSIGNED ? -1 : (b.awaiting_reply ?? -1) - (a.awaiting_reply ?? -1) || a.team.localeCompare(b.team)));
  const people = [...(data.people ?? [])].sort((a, b) => Number(b.self) - Number(a.self) || b.awaiting_reply - a.awaiting_reply || (a.name ?? "").localeCompare(b.name ?? ""));
  const granted = data.granted_teams ?? [];
  const head = "px-2 py-1.5 font-normal";
  return (
    <div className="grid gap-8 lg:grid-cols-2">
      <div>
        <h3 className="mb-1 text-[12px] font-medium text-muted">By team</h3>
        {teams.length === 0 ? (
          <p className="text-[13px] text-muted">Nothing open is counted yet.</p>
        ) : (
          <table className="w-full border-collapse text-[13px]" data-testid="workload-teams">
            <thead className="text-left text-[11.5px] text-muted">
              <tr className="border-b border-border">
                <th scope="col" className={head}>Team</th>
                <th scope="col" className={cn(head, "text-right")}>Awaiting reply</th>
                <th scope="col" className={cn(head, "text-right")}>Tasks</th>
                <th scope="col" className={cn(head, "text-right")}>Alerts</th>
              </tr>
            </thead>
            <tbody>
              {teams.map((t) => (
                <tr key={t.team} className="border-b border-border" data-testid="workload-team">
                  <td className="px-2 py-1.5">{teamName(t.team)}</td>
                  <td className="px-2 py-1.5 text-right"><Num n={t.awaiting_reply} warn /></td>
                  <td className="px-2 py-1.5 text-right"><Num n={t.tasks} /></td>
                  <td className="px-2 py-1.5 text-right"><Num n={t.alerts} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {teams.some((t) => t.awaiting_reply === null) && <p className="mt-1.5 text-[12px] text-muted">A dot: the team has fewer than three people, so its reply total would name one of them.</p>}
      </div>
      <div>
        <h3 className="mb-1 text-[12px] font-medium text-muted">{granted.length ? `You and ${granted.map(teamName).join(", ")}` : "You"}</h3>
        {people.length === 0 ? (
          <p className="text-[13px] text-muted">Nothing open for you right now.</p>
        ) : (
          <table className="w-full border-collapse text-[13px]" data-testid="workload-people">
            <thead className="text-left text-[11.5px] text-muted">
              <tr className="border-b border-border">
                <th scope="col" className={head}>Person</th>
                <th scope="col" className={head}>Team</th>
                <th scope="col" className={cn(head, "text-right")}>Awaiting reply</th>
                <th scope="col" className={cn(head, "text-right")}>Tasks</th>
                <th scope="col" className={cn(head, "text-right")}>Alerts</th>
              </tr>
            </thead>
            <tbody>
              {people.map((p) => (
                <tr key={p.person} className="border-b border-border" data-testid="workload-person">
                  <td className="px-2 py-1.5">
                    {p.self ? "You" : p.name || "Someone not in the team list"}
                  </td>
                  <td className="px-2 py-1.5 text-muted">{teamName(p.team)}</td>
                  <td className="px-2 py-1.5 text-right"><Num n={p.awaiting_reply} warn /></td>
                  <td className="px-2 py-1.5 text-right"><Num n={p.tasks} /></td>
                  <td className="px-2 py-1.5 text-right"><Num n={p.alerts} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

type Draft = Record<string, { team: string; sees: string[] }>;

const draftOf = (people: WorkloadGrantPerson[]): Draft => Object.fromEntries(people.map((p) => [p.oid, { team: p.team ?? "", sees: [...(p.sees ?? [])] }]));

/** The body to save: every listed person's team and grants, nothing for an empty choice. */
export function grantsOf(draft: Draft): WorkloadGrants {
  const teams: Record<string, string> = {};
  const managers: Record<string, string[]> = {};
  for (const [oid, row] of Object.entries(draft)) {
    if (row.team) teams[oid] = row.team;
    if (row.sees.length) managers[oid] = [...new Set(row.sees)].sort();
  }
  return { teams, managers };
}

/** The owner's sheet: one row per person, their team and the teams they see one by one. */
export function GrantSheet({ sheet, onSaved }: { sheet: WorkloadGrantSheet; onSaved: () => void }) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(sheet.people ?? []));
  const [extra, setExtra] = useState<string[]>([]);
  const [adding, setAdding] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => setDraft(draftOf(sheet.people ?? [])), [sheet]);
  const codes = useMemo(() => [...new Set([...(sheet.teams ?? []), ...extra, ...Object.values(draft).flatMap((r) => [r.team, ...r.sees]).filter(Boolean)])].sort(), [sheet, extra, draft]);
  const changed = JSON.stringify(grantsOf(draft)) !== JSON.stringify(grantsOf(draftOf(sheet.people ?? [])));
  const set = (oid: string, row: Partial<Draft[string]>) => {
    setSaved(false);
    setDraft((d) => ({ ...d, [oid]: { ...d[oid], ...row } }));
  };
  const addTeam = () => {
    const code = teamCode(adding);
    if (!code) return setProblem("A team name is letters, digits and spaces, starting with a letter, up to 32 characters.");
    setProblem(null);
    setExtra((x) => (x.includes(code) ? x : [...x, code]));
    setAdding("");
  };
  const save = async () => {
    setBusy(true);
    setProblem(null);
    try {
      await setWorkloadGrants(grantsOf(draft));
      setSaved(true);
      onSaved();
    } catch (e) {
      setProblem(e instanceof ApiError && e.detail ? e.detail : plainError(e));
    } finally {
      setBusy(false);
    }
  };
  const people = sheet.people ?? [];
  return (
    <div className="space-y-3" data-testid="grant-sheet">
      <p className="max-w-[70ch] text-[13px] leading-5 text-muted">
        Put each person on a team; their threads awaiting reply count under it. A person who sees a team gets its people one by one on their own Workload. The owner and the people the Workbench leaves out are never listed here.
        {sheet.held_back ? ` ${sheet.held_back === 1 ? "One entry" : `${sheet.held_back} entries`} for them stay${sheet.held_back === 1 ? "s" : ""} as ${sheet.held_back === 1 ? "it is" : "they are"}.` : ""}
      </p>
      {people.length === 0 ? (
        <p className="text-[13px] text-muted">No one is in the team list yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[13px]">
            <thead className="text-left text-[11.5px] text-muted">
              <tr className="border-b border-border">
                <th scope="col" className="px-2 py-1.5 font-normal">Person</th>
                <th scope="col" className="px-2 py-1.5 font-normal">Team</th>
                <th scope="col" className="px-2 py-1.5 font-normal">Sees, person by person</th>
              </tr>
            </thead>
            <tbody>
              {people.map((p) => {
                const row = draft[p.oid] ?? { team: "", sees: [] };
                const name = p.name || `Not in the team list (${p.oid.slice(0, 8)})`;
                return (
                  <tr key={p.oid} className="border-b border-border align-top" data-testid="grant-row">
                    <td className="px-2 py-1.5">{name}</td>
                    <td className="px-2 py-1.5">
                      <select aria-label={`Team for ${name}`} value={row.team} onChange={(e) => set(p.oid, { team: e.target.value })} className="h-7 cursor-pointer rounded-md border border-border bg-bg px-2 text-[13px] text-fg outline-none transition-colors hover:border-border-strong focus:border-accent">
                        <option value="">No team</option>
                        {codes.map((c) => (
                          <option key={c} value={c}>{teamName(c)}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-2 py-1.5">
                      <div className="flex flex-wrap gap-x-3 gap-y-1">
                        {codes.map((c) => (
                          <label key={c} className="inline-flex cursor-pointer items-center gap-1.5">
                            <input type="checkbox" className="cursor-pointer accent-[var(--accent)]" checked={row.sees.includes(c)} onChange={(e) => set(p.oid, { sees: e.target.checked ? [...row.sees, c] : row.sees.filter((x) => x !== c) })} aria-label={`${name} sees ${teamName(c)}`} />
                            {teamName(c)}
                          </label>
                        ))}
                        {codes.length === 0 && <span className="text-muted">Add a team first</span>}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <input value={adding} onChange={(e) => setAdding(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addTeam()} placeholder="New team, e.g. Sales" aria-label="New team" maxLength={32} className="h-8 w-48 rounded-md border border-border bg-bg px-2.5 text-[13px] text-fg outline-none transition-colors hover:border-border-strong focus:border-accent" />
        <Button size="sm" variant="secondary" onClick={addTeam} disabled={!adding.trim()}>Add team</Button>
        <span className="flex-1" />
        {saved && !changed && <span className="text-[12px] text-ok" role="status">Saved</span>}
        <Button size="sm" loading={busy} onClick={save} disabled={busy || !changed} data-testid="grant-save">{busy ? "Saving" : "Save teams"}</Button>
      </div>
      {problem && <ErrorNotice message={problem} />}
    </div>
  );
}

/** The Team page's #workload section; the grant sheet only when the server says you are the owner. */
export function WorkloadSection({ owner: isOwner }: { owner: boolean }) {
  // viewing as someone: their Workload, never the owner's sheet (the server refuses writes then)
  const owner = isOwner && !useViewingAs();
  const load = useRead("/api/workload", getWorkload);
  const sheet = useRead("/api/workload/grants", getWorkloadGrants, owner);
  const [editing, setEditing] = useState(false);
  const refused = sheet.error instanceof ApiError && sheet.error.status === 403;
  return (
    <div className="space-y-4">
      {load.error ? (
        <LoadProblem message={plainError(load.error)} busy={noticeKind(load.error) === "busy"} onRetry={load.retry} />
      ) : load.data ? (
        <>
          <StoredAge at={load.at} />
          <WorkloadTables data={load.data} />
        </>
      ) : (
        <Pending label="Counting open work" />
      )}
      {owner && !refused && (
        <div className="max-w-5xl border-t border-border pt-4" data-testid="grant-editor">
          <div className="mb-2 flex items-baseline justify-between gap-3">
            <h3 className="text-[13px] font-semibold">Teams and who sees whom</h3>
            {sheet.data && (
              <button type="button" className="cursor-pointer text-[12px] font-medium text-[color:var(--accent)] hover:underline" onClick={() => setEditing((v) => !v)} aria-expanded={editing}>
                {editing ? "Close" : "Change"}
              </button>
            )}
          </div>
          {sheet.data ? <StoredAge at={sheet.at} className="mb-2" /> : null}
          {sheet.error ? (
            <LoadProblem message={sheet.error instanceof ApiError && sheet.error.detail ? sheet.error.detail : plainError(sheet.error)} onRetry={sheet.retry} />
          ) : !sheet.data ? (
            <Pending label="Reading the teams" />
          ) : editing ? (
            <GrantSheet sheet={sheet.data} onSaved={() => { sheet.retry(); load.retry(); }} />
          ) : (
            <p className="text-[13px] text-muted">
              {(sheet.data.people ?? []).filter((p) => p.team).length} of {(sheet.data.people ?? []).length} people on a team
              {(sheet.data.people ?? []).some((p) => (p.sees ?? []).length) ? `; ${(sheet.data.people ?? []).filter((p) => (p.sees ?? []).length).length} see a team person by person` : ""}.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
