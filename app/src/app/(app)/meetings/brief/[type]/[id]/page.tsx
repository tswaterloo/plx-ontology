"use client";

import { notFound, useParams } from "next/navigation";

import { BriefView } from "@/components/brief-view";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { BRIEF_TYPES, getMeetingBrief } from "@/lib/api";
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
  const { data: brief, error, retry } = useRead<Brief>(`brief ${type} ${objectId}`, (signal) => getMeetingBrief(type, objectId, signal));
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading={!brief} title={brief?.title ?? "Brief"} parent={{ label: "Meetings", href: "/meetings" }} />
      <div className="p-5">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : brief ? (
          <BriefView brief={brief} />
        ) : (
          <Pending page label="Preparing the brief" />
        )}
      </div>
    </div>
  );
}
