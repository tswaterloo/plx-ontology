"use client";

import { useCallback, useEffect, useState } from "react";
import { ReplyList } from "@/components/reply-list";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadProblem } from "@/components/ui/notice";
import { getReplies } from "@/lib/api";
import { noticeKind, plainError } from "@/lib/errors";
import { plural } from "@/lib/format";
import type { ReplyTriage } from "@/lib/types";

// What needs my reply today: your own mail and one-to-one chats where someone asked you
// something you have not answered, heaviest first. A reply is a draft: it goes out only
// through the card's preview and your confirm.
export default function RepliesPage() {
  const [triage, setTriage] = useState<ReplyTriage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);
  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    setTriage(null);
    getReplies(ctl.signal).then(setTriage, (e) => {
      if (!ctl.signal.aborted) setError(e);
    });
    return () => ctl.abort();
  }, [round]);
  if (error) return <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={retry} />;
  if (!triage) return <Skeleton className="h-64 w-full" />;
  return <ReplyList triage={triage} />;
}
