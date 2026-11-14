"use client";

import { useReaderWords } from "@/components/view-as";
import { MyWorkList } from "@/components/my-work";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem, StaleNote } from "@/components/ui/notice";
import { getMyWork } from "@/lib/api";
import { useRead } from "@/lib/use-read";
import { useStaleRefresh } from "@/lib/stale-refresh";
import { noticeKind, plainError } from "@/lib/errors";
import type { MyWork } from "@/lib/types";

// The work the autopilots prepared for you from new mail, tasks, approvals, QC results,
// meetings and record changes. A message goes out only through its preview and your
// confirm; an entry is a dry-run write intent until writes are granted.
export default function MyWorkPage() {
  const w = useReaderWords();
  const { data: work, error, retry } = useRead<MyWork>("my-work", (signal) => getMyWork(signal));
  useStaleRefresh(work?.stale_since, retry);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="My work" />
      <div className="w-full px-6 py-5">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : work ? (
          <>
            <StaleNote since={work.stale_since} readAt={work.read_at} className="mb-2" />
            <MyWorkList work={work} />
          </>
        ) : (
          <Pending page label={`Reading the work prepared for ${w.you}`} />
        )}
      </div>
    </div>
  );
}
