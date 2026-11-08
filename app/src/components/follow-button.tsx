"use client";

import { Bell, BellRing } from "lucide-react";
import { useEffect, useState } from "react";
import { getWatchlist, setFollowing } from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
import { cn } from "@/lib/cn";
import { ToolbarButton } from "./ui/toolbar";

/** Follow this record: an alert in your own Watching inbox when it, or anything linked to
 * it, changes. Kept in the Workbench only; nothing is written to a source system. */
export function FollowButton({ objectId }: { objectId: string }) {
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    getWatchlist(ctl.signal)
      .then((w) => setOn(w.configured ? w.follows.some((f) => f.object_id === objectId) : null))
      .catch(() => setOn(null));
    return () => ctl.abort();
  }, [objectId]);

  if (on === null) return null; // no watchlists on this server, or not read yet
  const toggle = async () => {
    setBusy(true);
    setProblem(null);
    try {
      setOn((await setFollowing(objectId, !on)).following);
    } catch (err) {
      setProblem(err instanceof ApiError && err.detail ? err.detail : plainError(err));
    } finally {
      setBusy(false);
    }
  };
  const Icon = on ? BellRing : Bell;
  return (
    <ToolbarButton aria-pressed={on} disabled={busy} onClick={toggle} className={cn(on && "text-fg")} title={problem ?? (on ? "Following: you get alerts when this changes" : "Follow: get alerts when this changes")}>
      <Icon className={cn(on && "fill-current")} strokeWidth={2} aria-hidden />
      <span className="max-sm:sr-only">{on ? "Following" : "Follow"}</span>
    </ToolbarButton>
  );
}
