import Link from "next/link";
import { AppShell } from "@/components/shell";

export default function NotFound() {
  return (
    <AppShell>
      <div className="mx-auto flex w-full max-w-md flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
        <p className="text-meta text-muted">Page not found</p>
        <h1 className="text-title">We couldn&apos;t find that page</h1>
        <p className="text-body text-muted">The link may be old, or the number may be off by a digit. Search above, or go back to Today.</p>
        <Link href="/" className="mt-2 inline-flex h-10 items-center rounded-lg bg-accent px-4 text-row font-medium text-accent-fg">
          Go to Today
        </Link>
      </div>
    </AppShell>
  );
}
