"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/components/auth";
import { LabourView } from "@/components/labour-view";
import { hasTeamGrant } from "@/components/shell";
import { TeamApprovals } from "@/components/team/approvals";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { readPlannerAhead, TeamDashboard } from "@/components/team/dashboard";
import { WorkloadSection } from "@/components/team/workload";
import { getLabour, getTeam } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { StoredAge } from "@/components/ui/stored-age";
import { ApiError, noticeKind, plainError } from "@/lib/errors";
import type { LabourPage, TeamView } from "@/lib/types";

/** A read of `key` (its API path) drawn from the kept reads, read again behind it. */
function useLoad<T>(key: string, load: (s: AbortSignal) => Promise<T>, on: boolean) {
  return useRead<T>(key, load, on);
}

/** Team: each person's activity across systems and open work (everyone for the owner and the
 * named Team readers, else only your own row: the server decides), what waits on other people
 * (approvals) for a reader with a team grant and, for a reader with the hr or ops-management
 * grant, labour hours. */
export default function TeamPage() {
  const { state } = useAuth();
  const me = state.status === "signed-in" ? state.me : null;
  const team = hasTeamGrant(me);
  // the dashboard's planner counts, asked for beside the people (not once they answered)
  useEffect(() => {
    if (me) readPlannerAhead();
  }, [!!me]); // eslint-disable-line react-hooks/exhaustive-deps
  const people = useLoad<TeamView>("/api/team", (s) => getTeam(s), !!me);
  const labour = useLoad<LabourPage>("/api/labour", (s) => getLabour(s), !!me);
  const labourOn = !!labour.data && labour.data.state !== "not_granted";
  // /labour lands on #labour: a reader without the grant is told so there, never left on Team
  // wondering where the hours went (nobody else sees a word about labour)
  const [hash, setHash] = useState("");
  const [person, setPerson] = useState("");
  useEffect(() => {
    setPerson(new URLSearchParams(window.location.search).get("person") ?? "");
    const read = () => setHash(window.location.hash);
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);
  const labourRefused =
    (labour.data?.state === "not_granted" || (labour.error instanceof ApiError && (labour.error.status === 403 || labour.error.status === 404))) && hash === "#labour";
  const heading = people.data?.scope === "self" ? "Your activity" : "People";
  // the pinned "Approvals" (/team#approvals) opens on the approvals: the section comes first,
  // above the people table (the browser's own jump to #approvals fires before it is drawn)
  const approvalsFirst = team && hash === "#approvals";
  const approvals = (
    <section id="approvals" aria-labelledby="approvals-title" className="max-w-6xl scroll-mt-16 space-y-3" data-testid="team-approvals">
      <h2 id="approvals-title" className="text-[15px] font-semibold">Approvals waiting on others</h2>
      <TeamApprovals />
    </section>
  );
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Team" />
      <PageIntro path="/team" />
      <StoredAge at={people.at} className="px-6 pb-1" />
      <div className="w-full space-y-10 px-4 py-5 lg:px-6">
        {!me ? <Pending page label="Checking your access" /> : null}
        {approvalsFirst && approvals}
        {me && (
          <section id="people" aria-labelledby="people-title" className="scroll-mt-16 space-y-3">
            <h2 id="people-title" className="text-[15px] font-semibold">{heading}</h2>
            {people.error ? (
              <LoadProblem message={plainError(people.error)} busy={noticeKind(people.error) === "busy"} onRetry={people.retry} />
            ) : people.data ? (
              <TeamDashboard data={people.data} person={person} />
            ) : (
              <Pending label="Counting activity" />
            )}
          </section>
        )}
        {me && (
          <section id="workload" aria-labelledby="workload-title" className="max-w-6xl scroll-mt-16 space-y-3">
            <h2 id="workload-title" className="text-[15px] font-semibold">Workload</h2>
            <WorkloadSection owner={!!me.owner} />
          </section>
        )}
        {team && !approvalsFirst && approvals}
        {labourOn && labour.data && (
          <section id="labour" aria-labelledby="labour-title" className="max-w-3xl scroll-mt-16 space-y-3" data-testid="team-labour">
            <h2 id="labour-title" className="text-[15px] font-semibold">Labour hours</h2>
            <LabourView page={labour.data} />
          </section>
        )}
        {labourRefused && (
          <section id="labour" aria-labelledby="labour-title" className="max-w-3xl scroll-mt-16 space-y-2" data-testid="team-labour-refused">
            <h2 id="labour-title" className="text-[15px] font-semibold">Labour hours</h2>
            <p className="text-[13px] leading-5 text-muted">
              Labour hours by department are open to readers with the HR or operations-management grant, which you don&apos;t have. Ask the owner if you need them.
            </p>
          </section>
        )}
        {labour.error && !(labour.error instanceof ApiError && (labour.error.status === 403 || labour.error.status === 404)) ? (
          <section aria-labelledby="labour-title" className="max-w-3xl space-y-3">
            <h2 id="labour-title" className="text-[15px] font-semibold">Labour hours</h2>
            <LoadProblem message={plainError(labour.error)} onRetry={labour.retry} />
          </section>
        ) : null}
      </div>
    </div>
  );
}
