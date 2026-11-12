"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/auth";
import { LabourView } from "@/components/labour-view";
import { hasTeamGrant } from "@/components/shell";
import { TeamApprovals } from "@/components/team/approvals";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { TeamDashboard } from "@/components/team/dashboard";
import { getLabour, getTeam } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { LabourPage, TeamView } from "@/lib/types";

function useLoad<T>(load: (s: AbortSignal) => Promise<T>, on: boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    if (!on) return;
    const ctl = new AbortController();
    setError(null);
    setData(null);
    load(ctl.signal).then(setData, (e) => !ctl.signal.aborted && setError(e));
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [round, on]);
  return { data, error, retry };
}

/** Team: each person's activity across systems and open work (everyone for the owner and the
 * named Team readers, else only your own row: the server decides), what waits on other people
 * (approvals) for a reader with a team grant and, for a reader with the hr or ops-management
 * grant, labour hours. */
export default function TeamPage() {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const team = hasTeamGrant(me);
  const people = useLoad<TeamView>(getTeam, !!me);
  const labour = useLoad<LabourPage>(getLabour, !!me);
  const labourOn = !!labour.data && labour.data.state !== "not_granted";
  const heading = people.data?.scope === "self" ? "Your activity" : "People";
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Team" />
      <div className="w-full space-y-10 px-4 py-5 lg:px-6">
        {!me ? <Pending page label="Checking your access" /> : null}
        {me && (
          <section id="people" aria-labelledby="people-title" className="scroll-mt-16 space-y-3">
            <h2 id="people-title" className="text-[15px] font-semibold">{heading}</h2>
            {people.error ? (
              <LoadProblem message={plainError(people.error)} busy={noticeKind(people.error) === "busy"} onRetry={people.retry} />
            ) : people.data ? (
              <TeamDashboard data={people.data} />
            ) : (
              <Pending label="Counting activity" />
            )}
          </section>
        )}
        {team && (
          <section id="approvals" aria-labelledby="approvals-title" className="max-w-3xl scroll-mt-16 space-y-3">
            <h2 id="approvals-title" className="text-[15px] font-semibold">Approvals waiting on others</h2>
            <TeamApprovals />
          </section>
        )}
        {labourOn && labour.data && (
          <section id="labour" aria-labelledby="labour-title" className="max-w-3xl scroll-mt-16 space-y-3" data-testid="team-labour">
            <h2 id="labour-title" className="text-[15px] font-semibold">Labour hours</h2>
            <LabourView page={labour.data} />
          </section>
        )}
        {labour.error ? <LoadProblem message={plainError(labour.error)} onRetry={labour.retry} /> : null}
      </div>
    </div>
  );
}
