"use client";

import { AppIcon } from "@/components/shell";
import { GENERIC_ERROR } from "@/lib/errors";

// Never render the error itself: no messages, digests or stacks reach the page.
export default function RouteError({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-sidebar px-6 py-16" role="alert">
      <div className="flex w-full max-w-[340px] flex-col items-center rounded-xl bg-bg px-6 pt-7 pb-6 text-center shadow-lift">
        <AppIcon className="size-9 rounded-[10px] [&>svg]:size-6" />
        <h1 className="mt-4 text-[15px] leading-5 font-semibold tracking-[-0.01em]">This page didn&apos;t load</h1>
        <p className="mt-1 text-[13px] leading-5 text-muted">{GENERIC_ERROR}</p>
        <div className="mt-5 flex gap-2">
          <a
            href="/"
            className="inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]"
          >
            Open Today
          </a>
          <button
            type="button"
            onClick={reset}
            className="inline-flex h-8 items-center rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg shadow-sm hover:brightness-110 cursor-pointer"
          >
            Try again
          </button>
        </div>
      </div>
    </div>
  );
}
