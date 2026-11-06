"use client";

import "./globals.css";

// No inline styles: the CSP allows only nonce-stamped styles, and this page replaces the
// root layout. The app's stylesheet is imported for its classes; unstyled it still reads.
export default function GlobalError({ reset }: { error: Error; reset: () => void }) {
  return (
    <html lang="en-CA">
      <body className="bg-sidebar text-fg antialiased">
        <main role="alert" className="flex min-h-dvh flex-col items-center justify-center px-6 py-16">
          <div className="flex w-full max-w-[340px] flex-col items-center rounded-xl bg-bg px-6 pt-7 pb-6 text-center shadow-lift">
            <h1 className="text-[15px] leading-5 font-semibold">The Workbench hit a problem</h1>
            <p className="mt-1 text-[13px] leading-5 text-muted">Try again; if it keeps happening, tell the Workbench team.</p>
            <button
              type="button"
              onClick={reset}
              className="mt-5 inline-flex h-8 items-center rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg hover:brightness-110 cursor-pointer"
            >
              Try again
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
