"use client";

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
  const { state, refresh, signOut, signOutError, redirectNext } = useAuth();
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
  // the live server without Microsoft sign-in takes access links: it is no demo, so it says so
  // in its own words and asks for the link at once
  const link = !entra && config?.sign_in === "access_link";
  const askTicket = showTicket || link;

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

  const field = "h-9 w-full rounded-md border border-border-strong bg-bg px-3 text-[13px] text-fg placeholder:text-faint transition-colors focus:border-accent focus:outline-none dark:bg-bg";

  return (
    <div className="relative flex min-h-dvh flex-col bg-sidebar">
      <div className="absolute top-3 right-3 sm:top-4 sm:right-4">
        <ThemeToggle />
      </div>
      <main id="main" className="flex flex-1 flex-col items-center justify-center px-4 pt-16 pb-12">
        <div className="w-full max-w-[368px]">
          <div className="flex flex-col items-center text-center">
            <Logo className="text-[21px] leading-7" />
            <h1 className="mt-3.5 text-[14px] leading-5 font-medium text-muted">Sign in to your workspace</h1>
          </div>
          <div className="mt-6 space-y-4 rounded-lg border border-border bg-bg p-5 shadow-sm dark:border-border-strong dark:bg-surface">
            {state.status === "loading" || state.status === "signed-in" ? (
              <Pending label="Checking your session" className="py-6 text-center" />
            ) : state.status === "error" ? (
              <ErrorNotice message={state.message} onRetry={refresh} />
            ) : accessNote ? (
              <div className="space-y-3">
                <ErrorNotice message={accessNote} />
                {signOutError && <ErrorNotice message={signOutError} />}
                <button
                  type="button"
                  onClick={() => void signOut()}
                  className="h-9 w-full rounded-md border border-border-strong bg-surface text-[13px] font-medium text-fg hover:bg-surface-2 cursor-pointer"
                >
                  Sign out and use another account
                </button>
              </div>
            ) : (
              <>
                <div className="space-y-2.5">
                  <button
                    type="button"
                    onClick={microsoft}
                    disabled={!entra || busy}
                    className="flex h-10 w-full items-center justify-center gap-2.5 rounded-md border border-border-strong bg-bg text-[13px] font-medium text-fg shadow-xs transition-[background-color,border-color,transform] duration-150 hover:border-control hover:bg-surface active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-50 dark:bg-surface-2 dark:hover:bg-raised cursor-pointer"
                  >
                    <MicrosoftMark />
                    {busy && entra ? "Signing in" : "Sign in with Microsoft"}
                  </button>
                  <p className="text-center text-[12px] leading-4 text-muted">
                    {entra ? "Your PLX work account. You see only what your role allows." : link ? "Microsoft sign-in is not on here yet." : "Microsoft sign-in is off on this demo server."}
                  </p>
                </div>
                {devAvailable && (
                  <div className="space-y-3">
                    <div className="flex items-center gap-3 pt-1 text-[11px] leading-4 text-faint">
                      <span className="h-px flex-1 bg-border" aria-hidden />
                      or
                      <span className="h-px flex-1 bg-border" aria-hidden />
                    </div>
                    {note && <p className="text-center text-[12px] leading-[18px] text-muted">{note}</p>}
                    <form
                      className="space-y-3"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void demo(askTicket ? ticket : undefined);
                      }}
                    >
                      {askTicket && (
                        <div>
                          <label htmlFor="ticket" className="mb-1.5 block text-[12px] leading-4 font-medium text-muted">
                            Access link or code
                          </label>
                          <input
                            id="ticket"
                            type="password"
                            autoComplete="off"
                            value={ticket}
                            onChange={(e) => setTicket(e.target.value.replace(/^.*#(?:ticket|access)=/, ""))}
                            className={field}
                          />
                        </div>
                      )}
                      <button
                        type="submit"
                        disabled={busy || (askTicket && !ticket.trim())}
                        className="h-10 w-full rounded-md bg-accent text-[13px] font-medium text-accent-fg shadow-xs transition-[filter,transform] duration-150 hover:brightness-110 active:scale-[0.99] disabled:opacity-50 cursor-pointer"
                      >
                        {link ? "Continue with your access link" : "Continue with demo sign-in"}
                      </button>
                      {!askTicket && (
                        <button
                          type="button"
                          onClick={() => setShowTicket(true)}
                          className="mx-auto block h-7 rounded-md px-2 text-[12px] text-muted hover:bg-hover hover:text-fg cursor-pointer"
                        >
                          Use an access link
                        </button>
                      )}
                    </form>
                  </div>
                )}
                {error && <ErrorNotice message={error} />}
              </>
            )}
          </div>
          <DevBanner className="mt-5 text-center text-[12px] text-faint [&>span:first-child]:font-normal [&>span:first-child]:text-faint" />
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
