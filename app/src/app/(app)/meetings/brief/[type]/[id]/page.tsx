"use client";

import { notFound, useParams } from "next/navigation";

import { BriefView } from "@/components/brief-view";
import { AddContext } from "@/components/brief-context";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { BRIEF_TYPES, getMeetingBrief } from "@/lib/api";
import { StoredAge } from "@/components/ui/stored-age";
import { useRead } from "@/lib/use-read";
import { noticeKind, plainError } from "@/lib/errors";
import type { Brief } from "@/lib/types";

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export default function BriefPage() {
  const { type, id } = useParams<{ type: string; id: string }>();
  // A type no brief is prepared for fails closed: no request is made.
  if (!BRIEF_TYPES.includes(type) || !id) notFound();
  const objectId = safeDecode(id);
  const { data: brief, error, retry, at } = useRead<Brief>(`/api/brief/${encodeURIComponent(type)}/${encodeURIComponent(objectId)}`, (signal) => getMeetingBrief(type, objectId, signal));
  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg">
      <Toolbar heading={!brief} title={brief?.title ?? "Brief"} parent={{ label: "Meetings", href: "/meetings" }} />
      <div className="mx-auto w-full max-w-[1160px] px-4 py-5 sm:px-6">
        <StoredAge at={at} className="pb-2" />
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : brief ? (
          <div className="space-y-6">
            <BriefView brief={brief} />
            <div className="border-t border-border pt-5">
              <AddContext brief={brief} onSaved={retry} />
            </div>
          </div>
        ) : (
          <Pending page limit={null} label="Preparing the brief" />
        )}
      </div>
    </div>
  );
}
