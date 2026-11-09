"use client";

import { notFound, useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { BriefView } from "@/components/brief-view";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { BRIEF_TYPES, getMeetingBrief } from "@/lib/api";
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
  const [brief, setBrief] = useState<Brief | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    getMeetingBrief(type, objectId, ctl.signal).then(setBrief, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [type, objectId, round]);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar title={brief?.title ?? "Brief"} parent={{ label: "Meetings", href: "/meetings" }} />
      <div className="p-5">
        {error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />
        ) : brief ? (
          <BriefView brief={brief} />
        ) : (
          <Pending label="Preparing the brief" />
        )}
      </div>
    </div>
  );
}
