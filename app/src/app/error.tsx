"use client";

import { GENERIC_ERROR } from "@/lib/errors";

// Never render the error itself: no messages, digests or stacks reach the page.
export default function RouteError({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center justify-center gap-3 p-10 text-center" role="alert">
      <h1 className="text-xl font-semibold tracking-tight">This page hit a problem</h1>
      <p className="text-muted">{GENERIC_ERROR}</p>
      <button
        type="button"
        onClick={reset}
        className="mt-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg cursor-pointer"
      >
        Try again
      </button>
    </div>
  );
}
