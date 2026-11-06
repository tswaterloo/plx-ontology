"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/auth";
import { AppIcon, DevBanner } from "@/components/shell";
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

  const field = "h-9 w-full rounded-lg bg-surface px-3 text-[13px] ring-1 ring-border-strong ring-inset placeholder:text-faint focus:ring-2 focus:ring-ring focus:outline-none";

  return (
    <div className="relative flex min-h-dvh flex-col bg-sidebar">
      <div className="absolute top-3 right-3 sm:top-4 sm:right-4">
        <ThemeToggle className="rounded-lg border-0 bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] p-0.5 text-[12px] [&>button]:rounded-md [&>button]:border-0 [&>button]:px-2.5 [&>button]:py-1 [&>button[aria-checked=true]]:bg-bg [&>button[aria-checked=true]]:shadow-sm" />
      </div>
      <main id="main" className="flex flex-1 flex-col items-center justify-center px-4 pt-16 pb-12">
        <div className="w-full max-w-[360px]">
          <div className="flex flex-col items-center text-center">
            <AppIcon className="size-14 rounded-[15px] shadow-[inset_0_-1.5px_0_rgb(0_0_0/0.18),0_6px_16px_-6px_rgb(27_94_64/0.55)] [&>svg]:size-[38px]" />
            <h1 className="mt-5 text-[20px] leading-[26px] font-semibold tracking-[-0.02em]">
              Sign in to <span className="whitespace-nowrap">PLX Workbench</span>
            </h1>
          </div>
          <div className="mt-7 space-y-4 rounded-xl bg-bg p-5 shadow-lift dark:bg-surface">
            {state.status === "loading" || state.status === "signed-in" ? (
              <Pending label="Checking your session" className="py-6 text-center" />
            ) : state.status === "error" ? (
              <ErrorNotice message={state.message} onRetry={refresh} />
            ) : accessNote ? (
              <div className="space-y-3">
                <ErrorNotice message={accessNote} />
                <button
                  type="button"
                  onClick={() => void signOut()}
                  className="h-9 w-full rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] cursor-pointer"
                >
                  Sign out and use another account
                </button>
              </div>
            ) : (
              <>
                <div className="space-y-2">
                  <button
                    type="button"
                    onClick={microsoft}
                    disabled={!entra || busy}
                    className="flex h-10 w-full items-center justify-center gap-2.5 rounded-lg bg-bg dark:bg-surface-2 text-[13px] font-medium text-fg shadow-sm ring-1 ring-border-strong hover:bg-surface disabled:cursor-not-allowed disabled:opacity-55 cursor-pointer"
                  >
                    <MicrosoftMark />
                    {busy && entra ? "Signing in…" : "Sign in with Microsoft"}
                  </button>
                  <p className="text-center text-[12px] leading-4 text-muted">
                    {entra ? "Your PLX work account. You see only what your role allows." : "Microsoft sign-in is off on this demo server."}
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
                        void demo(showTicket ? ticket : undefined);
                      }}
                    >
                      {showTicket && (
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
                        disabled={busy || (showTicket && !ticket.trim())}
                        className="h-10 w-full rounded-lg bg-accent text-[13px] font-medium text-accent-fg shadow-sm hover:brightness-110 disabled:opacity-50 cursor-pointer"
                      >
                        Continue with demo sign-in
                      </button>
                      {!showTicket && (
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
