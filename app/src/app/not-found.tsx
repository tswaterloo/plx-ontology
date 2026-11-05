import Link from "next/link";
import { AppShell } from "@/components/shell";

export default function NotFound() {
  return (
    <AppShell>
      <div className="mx-auto w-full max-w-[1200px] px-4 pt-8 pb-16 sm:px-8">
        <div className="max-w-[560px] space-y-2">
          <h1 className="border-b border-border pb-3 text-display">Page not found</h1>
          <p className="text-row text-muted">Check the address or the record number, or search for it above.</p>
          <Link href="/" className="inline-block text-row text-accent hover:underline">
            Go to Today
          </Link>
        </div>
      </div>
    </AppShell>
  );
}
