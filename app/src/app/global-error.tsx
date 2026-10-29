"use client";

// No inline styles: the CSP allows only nonce-stamped styles, and this page
// replaces the root layout. Plain HTML stays readable unstyled.
export default function GlobalError({ reset }: { error: Error; reset: () => void }) {
  return (
    <html lang="en-CA">
      <body>
        <main role="alert">
          <h1>The Workbench hit a problem</h1>
          <p>Try again; if it keeps happening, tell the Workbench team.</p>
          <button type="button" onClick={reset}>
            Try again
          </button>
        </main>
      </body>
    </html>
  );
}
