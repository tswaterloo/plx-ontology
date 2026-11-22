"use client";

import { useEffect, useState } from "react";
import { getFeeds } from "@/lib/api";
import type { FeedState } from "@/lib/types";

// one read per minute for every screen that asks: Home and the late list share it
let kept: { at: number; read: Promise<FeedState[] | null> } | null = null;
const KEEP_MS = 60_000;

function read(): Promise<FeedState[] | null> {
  if (!kept || Date.now() - kept.at > KEEP_MS) {
    kept = { at: Date.now(), read: getFeeds().then((f) => f.feeds, () => null) };
  }
  return kept.read;
}

/** Forget the kept read (tests, and a page that just changed a job). */
export function forgetFeeds(): void {
  kept = null;
}

/** The state of one background feed (null until read, or when it could not be read). */
export function useFeed(key: string): FeedState | null {
  const [feed, setFeed] = useState<FeedState | null>(null);
  useEffect(() => {
    let live = true;
    read().then((all) => {
      if (live) setFeed(all?.find((f) => f.key === key) ?? null);
    });
    return () => {
      live = false;
    };
  }, [key]);
  return feed;
}

/** What a screen says when the job behind its list is not running: a stopped job is not "nothing to do". */
export function feedLine(f: FeedState | null): string | null {
  if (!f || f.state === "running") return null;
  if (f.state === "not_running") return `${f.why || `${f.label} are not being made right now.`} An empty list here is not "nothing to do".`;
  return `Not checked: whether ${f.label.toLowerCase()} are being made. An empty list here is not "nothing to do".`;
}
