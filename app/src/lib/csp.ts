// The app's Content-Security-Policy. A fresh nonce per request (src/proxy.ts)
// lets Next.js stamp its own scripts and inline styles; nothing else inline
// may run, and 'strict-dynamic' lets only those trusted scripts load chunks.
// connect-src: the app's own API plus Microsoft sign-in (MSAL token calls).

export const MICROSOFT_LOGIN = "https://login.microsoftonline.com";

export function buildCsp(nonce: string, { dev = false }: { dev?: boolean } = {}): string {
  if (!/^[A-Za-z0-9+/=_-]{16,}$/.test(nonce)) throw new Error("bad nonce");
  return [
    "default-src 'self'",
    // 'unsafe-eval' only in development (React's dev tooling); never in production.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src 'self' ${MICROSOFT_LOGIN}`,
    "object-src 'none'",
    `frame-src ${MICROSOFT_LOGIN}`, // MSAL's silent token renewal runs in a hidden iframe
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}
