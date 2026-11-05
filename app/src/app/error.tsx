"use client";

import { GENERIC_ERROR } from "@/lib/errors";

// Never render the error itself: no messages, digests or stacks reach the page.
export default function RouteError({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto w-full max-w-[1200px] space-y-3 px-4 pt-[12vh] pb-16 sm:px-8" role="alert">
      <h1 className="text-display">This page hit a problem</h1>
      <p className="text-muted">{GENERIC_ERROR}</p>
      <button
        type="button"
        onClick={reset}
        className="mt-2 inline-flex h-8 items-center rounded bg-accent px-3 text-row font-medium text-accent-fg hover:brightness-110 cursor-pointer"
      >
        Try again
      </button>
    </div>
  );
}
