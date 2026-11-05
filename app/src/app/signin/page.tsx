"use client";

import { ChevronDown, FlaskConical, Loader2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/auth";
import { DevBanner, Logo } from "@/components/shell";
import { ThemeToggle } from "@/components/theme";
import { ErrorNotice } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
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
    <div className="flex min-h-dvh flex-col bg-bg">
      <DevBanner />
      <div className="flex items-center justify-between px-4 py-3 sm:px-8">
        <Logo />
        <ThemeToggle />
      </div>
      <main id="main" className="flex flex-1 items-start justify-center px-4 pt-[14vh] pb-16">
        <div className="w-full max-w-[360px]">
          <div className="space-y-2 border-b border-rule pb-5">
            <p className="text-eyebrow uppercase text-muted">PLX Workbench</p>
            <h1 className="text-display">Sign in</h1>
            <p className="text-row text-muted">
              Orders, suppliers, customers and lots across Business Central, the Portal, mail, Teams and files, each answer with its sources.
            </p>
          </div>
          <div className="space-y-4 pt-6">
            {state.status === "loading" || state.status === "signed-in" ? (
              <Pending label="Checking your session" />
            ) : state.status === "error" ? (
              <ErrorNotice message={state.message} onRetry={refresh} />
            ) : accessNote ? (
              <div className="space-y-3">
                <ErrorNotice message={accessNote} />
                <button
                  type="button"
                  onClick={() => void signOut()}
                  className="h-10 w-full rounded border border-control text-row font-medium hover:bg-surface cursor-pointer"
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
                  className="flex h-10 w-full items-center justify-center gap-3 rounded border border-control bg-surface text-row font-medium text-fg transition-colors hover:bg-raised disabled:cursor-not-allowed disabled:opacity-55 cursor-pointer"
                >
                  {busy && entra ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <MicrosoftMark />}
                  Sign in with Microsoft
                </button>
                <p className="text-meta text-muted">
                  {entra
                    ? "Use your PLX work account. You'll only see what your role allows."
                    : "Microsoft sign-in is off on this demo server."}
                </p>
                {devAvailable && (
                  <div className="space-y-3 border-t border-border pt-4">
                    <p className="flex items-center gap-1.5 text-eyebrow uppercase text-risk">
                      <FlaskConical className="size-3.5" strokeWidth={1.5} aria-hidden /> Demo server
                    </p>
                    {note && <p className="text-row text-muted">{note}</p>}
                    <form
                      className="space-y-3"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void demo(showTicket ? ticket : undefined);
                      }}
                    >
                      {showTicket ? (
                        <div>
                          <label htmlFor="ticket" className="mb-1 block text-meta text-muted">
                            Access link or code
                          </label>
                          <input
                            id="ticket"
                            type="password"
                            autoComplete="off"
                            value={ticket}
                            onChange={(e) => setTicket(e.target.value.replace(/^.*#(?:ticket|access)=/, ""))}
                            className="h-10 w-full rounded border border-control bg-surface px-3 text-row focus:border-fg"
                          />
                        </div>
                      ) : (
                        <button
                          type="button"
                          onClick={() => setShowTicket(true)}
                          className="inline-flex items-center gap-1 text-meta text-muted hover:text-fg cursor-pointer"
                        >
                          <ChevronDown className="size-3" aria-hidden /> I have an access link
                        </button>
                      )}
                      <button
                        type="submit"
                        disabled={busy || (showTicket && !ticket.trim())}
                        className="flex h-10 w-full items-center justify-center gap-2 rounded bg-accent text-row font-medium text-accent-fg hover:brightness-110 disabled:opacity-50 cursor-pointer"
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
