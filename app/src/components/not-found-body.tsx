"use client";

import Link from "next/link";
import { useCommandPalette } from "@/components/command-palette";
import { Button } from "@/components/ui/button";
import { Toolbar } from "@/components/ui/toolbar";

/** "Page not found" inside the app's frame: the root not-found adds the frame, a page's notFound() is already in it. */
export function NotFoundBody() {
  const { open } = useCommandPalette();
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Page not found" />
      <div className="w-full max-w-[640px] px-6 py-5">
        <p className="text-row text-fg">There is no page at this address.</p>
        <p className="mt-0.5 text-meta text-muted">Check the address or the record number, or search for the record.</p>
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" type="button" onClick={open}>
            Search
          </Button>
          <Button size="sm" variant="secondary" asChild>
            <Link href="/">Open My work</Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
