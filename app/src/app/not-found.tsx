import Link from "next/link";
import { AppShell } from "@/components/shell";
import { OpenRing } from "@/components/ui/glyphs";
import { Toolbar } from "@/components/ui/toolbar";

export default function NotFound() {
  return (
    <AppShell>
      <div className="flex min-h-0 flex-1 flex-col">
        <Toolbar title="" className="max-lg:hidden" />
        <div className="flex flex-1 items-center justify-center px-6 py-16">
          <div className="flex max-w-[320px] flex-col items-center text-center">
            <OpenRing size={36} tone="neutral" />
            <h1 className="mt-4 text-[15px] leading-5 font-semibold tracking-[-0.01em]">Page not found</h1>
            <p className="mt-1 text-[13px] leading-5 text-muted">Check the address or the record number, or search for it.</p>
            <Link
              href="/"
              className="mt-5 inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)]"
            >
              Open Today
            </Link>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
