"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth";
import { LabourView } from "@/components/labour-view";
import { hasTeamGrant } from "@/components/shell";
import { TeamApprovals } from "@/components/team/approvals";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { OwnerWorkloadView, WorkloadView } from "@/components/workload-view";
import { getLabour, getOwnerWorkload, getWorkload } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import type { LabourPage, Workload } from "@/lib/types";

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

/** Team: what waits on other people (approvals), each team's load and, for a reader with
 * the hr or ops-management grant, labour hours. A reader with neither a team grant nor a
 * labour grant goes to My work. */
export default function TeamPage() {
  const { state } = useAuth();
  const router = useRouter();
  const me = state.status === "signed-in" ? state.me : null;
  const team = hasTeamGrant(me);
  const workload = useLoad<Workload>(getWorkload, team);
  const labour = useLoad<LabourPage>(getLabour, !!me);
  // the per-person view is the owner's: any other reader is refused and sees nothing of it
  const owner = useLoad(getOwnerWorkload, team);
  const labourOn = !!labour.data && labour.data.state !== "not_granted";
  useEffect(() => {
    if (me && !team && labour.data && !labourOn) router.replace("/");
  }, [me, team, labour.data, labourOn, router]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Team" />
      <div className="mx-auto w-full max-w-3xl space-y-10 px-6 py-6">
        {!me || (!team && !labour.data && !labour.error) ? <Pending label="Checking your access" /> : null}
        {team && (
          <section id="approvals" aria-labelledby="approvals-title" className="scroll-mt-16 space-y-3">
            <h2 id="approvals-title" className="text-[15px] font-semibold">Approvals waiting on others</h2>
            <TeamApprovals />
          </section>
        )}
        {team && (
          <section id="workload" aria-labelledby="workload-title" className="scroll-mt-16 space-y-3">
            <h2 id="workload-title" className="text-[15px] font-semibold">Workload</h2>
            {workload.error ? (
              <LoadProblem message={plainError(workload.error)} busy={noticeKind(workload.error) === "busy"} onRetry={workload.retry} />
            ) : workload.data ? (
              <div className="space-y-6">
                <WorkloadView data={workload.data} />
                {owner.data && <OwnerWorkloadView data={owner.data} />}
              </div>
            ) : (
              <Pending label="Counting open work" />
            )}
          </section>
        )}
        {labourOn && labour.data && (
          <section id="labour" aria-labelledby="labour-title" className="scroll-mt-16 space-y-3" data-testid="team-labour">
            <h2 id="labour-title" className="text-[15px] font-semibold">Labour hours</h2>
            <LabourView page={labour.data} />
          </section>
        )}
        {labour.error && team ? <LoadProblem message={plainError(labour.error)} onRetry={labour.retry} /> : null}
      </div>
    </div>
  );
}
