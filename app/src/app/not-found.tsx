import Link from "next/link";
import { AppShell } from "@/components/shell";

export default function NotFound() {
  return (
    <AppShell>
      <div className="mx-auto w-full max-w-[1200px] px-4 pt-[12vh] pb-16 sm:px-8">
        <div className="max-w-[560px] space-y-3">
          <p className="text-eyebrow uppercase text-muted">Page not found</p>
          <h1 className="text-display">We couldn&apos;t find that page</h1>
          <p className="text-body text-muted">The link may be old, or the number may be off by a digit. Search above, or go back to Today.</p>
          <Link href="/" className="mt-2 inline-flex h-8 items-center rounded bg-accent px-3 text-row font-medium text-accent-fg hover:brightness-110">
            Go to Today
          </Link>
        </div>
      </div>
    </AppShell>
  );
}
