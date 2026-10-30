"use client";

import { ChevronDown, FlaskConical, Loader2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/auth";
import { DevBanner, Logo } from "@/components/shell";
import { ThemeToggle } from "@/components/theme";
import { ErrorNotice } from "@/components/ui/notice";
import { Skeleton } from "@/components/ui/skeleton";
import { devLogin } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { plainServerNote, safeNext } from "@/lib/format";
import { takeTicket } from "@/lib/ticket";

function MicrosoftMark() {
  return (
    <svg viewBox="0 0 21 21" className="size-[18px]" aria-hidden>
      <rect x="1" y="1" width="9" height="9" fill="#f25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
      <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
      <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
    </svg>
  );
}

function SignIn() {
  const { state, refresh, signOut, redirectNext } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const next = redirectNext ?? safeNext(params.get("next"));
  const [ticket, setTicket] = useState("");
  const [showTicket, setShowTicket] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const autoTried = useRef(false);

  useEffect(() => {
    if (state.status === "signed-in") router.replace(next);
  }, [state.status, router, next]);

  const config = state.config;
  const entra = config?.mode === "entra";
  const devAvailable = !!config && config.mode !== "entra" && config.dev_login;

  async function demo(t?: string) {
    setBusy(true);
    setError(null);
    try {
      await devLogin(t);
      refresh();
    } catch (err) {
      setError(plainError(err));
      if ((err as { code?: string }).code === "dev_ticket_required") setShowTicket(true);
    } finally {
      setBusy(false);
    }
  }

  // An access link (…#ticket=…) signs in once. The auth layer already removed
  // the fragment from the URL on whichever page the link opened.
  useEffect(() => {
    if (autoTried.current || state.status !== "signed-out" || !devAvailable) return;
    const t = takeTicket();
    if (!t) return;
    autoTried.current = true;
    void demo(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.status, devAvailable]);

  async function microsoft() {
    setBusy(true);
    setError(null);
    try {
      const msal = await import("@/lib/msal");
      await msal.entraSignIn(next);
    } catch (err) {
      setError(plainError(err));
      setBusy(false);
    }
  }

  const note = plainServerNote(config?.message);
  const accessNote = state.status === "signed-out" ? state.message : undefined;

  return (
    <div className="flex min-h-dvh flex-col bg-bg-subtle">
      <DevBanner />
      <div className="flex justify-end p-4">
        <ThemeToggle />
      </div>
      <main id="main" className="flex flex-1 items-start justify-center px-4 pt-[6vh] pb-16">
        <div className="w-full max-w-sm space-y-8">
          <div className="space-y-3 text-center">
            <Logo className="text-lg" />
            <h1 className="text-2xl font-semibold tracking-tight">Sign in to ask anything</h1>
            <p className="text-sm text-muted">
              Orders, suppliers, customers and lots across Business Central, the Portal, mail, Teams and files — with sources.
            </p>
          </div>
          <div className="space-y-4 rounded-2xl border border-border bg-surface p-6 shadow-card">
            {state.status === "loading" || state.status === "signed-in" ? (
              <div className="space-y-3" role="status" aria-label="Checking your session">
                <Skeleton className="h-11 w-full rounded-xl" />
              </div>
            ) : state.status === "error" ? (
              <ErrorNotice message={state.message} onRetry={refresh} />
            ) : accessNote ? (
              <div className="space-y-3">
                <ErrorNotice message={accessNote} />
                <button
                  type="button"
                  onClick={() => void signOut()}
                  className="h-11 w-full rounded-xl border border-border-strong text-sm font-medium hover:bg-surface-2 cursor-pointer"
                >
                  Sign out and use another account
                </button>
              </div>
            ) : (
              <>
                <button
                  type="button"
                  onClick={microsoft}
                  disabled={!entra || busy}
                  className="flex h-11 w-full items-center justify-center gap-3 rounded-xl border border-border-strong bg-surface text-sm font-medium text-fg transition-colors hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-55 cursor-pointer"
                >
                  {busy && entra ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <MicrosoftMark />}
                  Sign in with Microsoft
                </button>
                <p className="text-center text-xs text-muted">
                  {entra
                    ? "Use your PLX work account. You'll only see what your role allows."
                    : "Microsoft sign-in is off on this demo server."}
                </p>
                {devAvailable && (
                  <div className="space-y-3 border-t border-border pt-4">
                    <p className="flex items-center gap-1.5 text-xs font-medium text-warn">
                      <FlaskConical className="size-3.5" aria-hidden /> DEV mode
                    </p>
                    {note && <p className="text-sm text-muted">{note}</p>}
                    <form
                      className="space-y-3"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void demo(showTicket ? ticket : undefined);
                      }}
                    >
                      {showTicket ? (
                        <div>
                          <label htmlFor="ticket" className="mb-1 block text-xs text-muted">
                            Access link or code
                          </label>
                          <input
                            id="ticket"
                            type="password"
                            autoComplete="off"
                            value={ticket}
                            onChange={(e) => setTicket(e.target.value.replace(/^.*#(?:ticket|access)=/, ""))}
                            className="h-11 w-full rounded-xl border border-border bg-surface px-3 text-sm focus:border-accent focus:outline-none"
                          />
                        </div>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setShowTicket(true)}
                          className="inline-flex items-center gap-1 text-xs text-muted hover:text-fg cursor-pointer"
                        >
                          <ChevronDown className="size-3" aria-hidden /> I have an access link
                        </button>
                      )}
                      <button
                        type="submit"
                        disabled={busy || (showTicket && !ticket.trim())}
                        className="flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-accent text-sm font-medium text-accent-fg disabled:opacity-50 cursor-pointer"
                      >
                        {busy && <Loader2 className="size-4 animate-spin" aria-hidden />}
                        Continue with demo sign-in
                      </button>
                    </form>
                  </div>
                )}
                {error && <ErrorNotice message={error} />}
              </>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

export default function SignInPage() {
  return (
    <Suspense fallback={null}>
      <SignIn />
    </Suspense>
  );
}
