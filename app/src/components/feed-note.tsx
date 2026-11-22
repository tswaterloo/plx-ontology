"use client";

import { feedLine, useFeed } from "@/lib/feeds";

/** One line under a list made by a background job, only when that job is stopped or unchecked. */
export function FeedNote({ feed, className }: { feed: string; className?: string }) {
  const line = feedLine(useFeed(feed));
  if (!line) return null;
  return (
    <p role="status" data-testid={`feed-note-${feed}`} className={className ?? "px-5 py-1.5 text-[12px] text-risk"}>
      {line}
    </p>
  );
}
