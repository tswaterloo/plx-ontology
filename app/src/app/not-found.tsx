import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto flex min-h-dvh max-w-md flex-col items-center justify-center gap-3 p-6 text-center">
      <p className="text-sm font-medium text-muted">Page not found</p>
      <h1 className="text-2xl font-semibold tracking-tight">We couldn&apos;t find that page</h1>
      <p className="text-muted">The link may be old, or the number may be off by a digit.</p>
      <Link href="/" className="mt-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg">
        Go home
      </Link>
    </div>
  );
}
