"use client";

import { AuditView } from "@/components/autopilot/audit";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { LoadProblem } from "@/components/ui/notice";
import { getAutopilot } from "@/lib/api";
import { NO_FILTERS } from "@/lib/autopilot";
import { useRead } from "@/lib/use-read";
import { noticeKind, plainError } from "@/lib/errors";
import type { AutopilotArea } from "@/lib/types";

// What the autopilot did and what each person did with it, with undo: the owner's, under Admin.
export default function AutopilotAuditPage() {
  const { data: area, error, retry } = useRead<AutopilotArea>("autopilot", (signal) => getAutopilot(signal));
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Autopilot log" />
      <PageIntro path="/autopilot-log" />
      <div className="w-full px-6 py-5">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : area ? (
          area.owner ? <AuditView area={area} filters={NO_FILTERS} /> : <p className="text-[13px] text-muted">The log is for the owner.</p>
        ) : (
          <Pending page label="Reading the log" />
        )}
      </div>
    </div>
  );
}
