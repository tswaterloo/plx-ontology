"use client";

import { usePathname, useRouter } from "next/navigation";
import { createContext, Fragment, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { getAuthConfig, getMe, logout, setTokenProvider } from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
import { safeNext } from "@/lib/format";
import { clearPending } from "@/lib/pending";
import { clearSavedFor, setSavedUser } from "@/lib/saved";
import { resetSessionCaches } from "@/lib/session-cache";
import { captureTicket } from "@/lib/ticket";
import type { AuthConfig, Me } from "@/lib/types";

export type AuthState =
  | { status: "loading"; config: AuthConfig | null }
  | { status: "signed-in"; config: AuthConfig; me: Me }
  | { status: "signed-out"; config: AuthConfig; message?: string }
  | { status: "error"; config: AuthConfig | null; message: string };

interface AuthCtx {
  state: AuthState;
  refresh: () => void;
  /** True once the session is ended; false when the server could not end it (still signed in; `signOutError` says so). */
  signOut: () => Promise<boolean>;
  /** Set when the last sign-out did not finish: you are still signed in. */
  signOutError: string | null;
  /** Set by the Entra start-up when a sign-in redirect returned with a destination. */
  redirectNext: string | null;
}

const Ctx = createContext<AuthCtx>({
  state: { status: "loading", config: null },
  refresh: () => undefined,
  signOut: async () => true,
  signOutError: null,
  redirectNext: null,
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading", config: null });
  const [redirectNext, setRedirectNext] = useState<string | null>(null);
  const configRef = useRef<AuthConfig | null>(null);
  const principalRef = useRef<string | null>(null);

  // Any page an access link opens: take the #ticket= out of the URL first.
  useLayoutEffect(() => captureTicket(), []);

  useEffect(() => {
    const principal = state.status === "signed-in" ? state.me.principal : null;
    principalRef.current = principal;
    setSavedUser(principal);
  }, [state]);

  const refresh = useCallback(() => {
    const c = new AbortController();
    (async () => {
      let config = configRef.current;
      try {
        if (!config) {
          config = await getAuthConfig(c.signal);
          configRef.current = config;
        }
        if (config.mode === "entra") {
          if (!config.entra) {
            setState({ status: "error", config, message: plainError(new ApiError(503, "not_served")) });
            return;
          }
          const msal = await import("@/lib/msal");
          const start = await msal.startEntra(config.entra);
          if (start.next) setRedirectNext(safeNext(start.next));
          if (!start.account) {
            setTokenProvider(null);
            setState({ status: "signed-out", config });
            return;
          }
          setTokenProvider(msal.entraToken);
        } else {
          setTokenProvider(null);
        }
        const me = await getMe(c.signal);
        // another person in this tab (an access link or dev sign-in over a session):
        // nothing read for the last one is kept
        if (principalRef.current !== null && principalRef.current !== me.principal) resetSessionCaches();
        setState({ status: "signed-in", config, me });
      } catch (e) {
        if (c.signal.aborted) return;
        if (e instanceof ApiError && e.status === 401 && config) {
          if (principalRef.current !== null) resetSessionCaches();   // the session ended
          setState({ status: "signed-out", config });
        } else if (e instanceof ApiError && e.status === 403 && config) {
          // Signed in, but no Workbench access: say so plainly, offer sign-out.
          setState({ status: "signed-out", config, message: plainError(e) });
        } else {
          setState({ status: "error", config, message: plainError(e) });
        }
      }
    })();
    return () => c.abort();
  }, []);

  const [signOutError, setSignOutError] = useState<string | null>(null);
  const signOut = useCallback(async (): Promise<boolean> => {
    const config = configRef.current;
    setSignOutError(null);
    try {
      await logout();
    } catch (e) {
      // the server did not end the session: the cookie may still be good, so this browser is
      // still signed in and says so rather than showing a signed-out page
      if (config?.mode !== "entra") {
        setSignOutError(`You are still signed in: the Workbench could not end your session (${plainError(e).replace(/\.$/, "")}). Try again before you leave this computer.`);
        return false;
      }
    }
    setTokenProvider(null);
    resetSessionCaches();
    clearSavedFor(principalRef.current);
    clearPending();
    if (config?.mode === "entra") {
      const msal = await import("@/lib/msal");
      await msal.entraSignOut();
      return true;
    }
    if (config) setState({ status: "signed-out", config });
    return true;
  }, []);

  useEffect(() => refresh(), [refresh]);
  // Keyed by who is signed in: a different person remounts every page, so no component
  // keeps state or an in-flight read from the last one.
  const who = state.status === "signed-in" ? state.me.principal : "";
  return (
    <Ctx.Provider value={{ state, refresh, signOut, signOutError, redirectNext }}>
      <Fragment key={who}>{children}</Fragment>
    </Ctx.Provider>
  );
}

export function useAuth() {
  return useContext(Ctx);
}

/** Sends a signed-out visitor to /signin, keeping where they were going. Fails closed. */
export function RequireAuth({ children, fallback }: { children: React.ReactNode; fallback: React.ReactNode }) {
  const { state } = useAuth();
  const router = useRouter();
  const path = usePathname();
  useEffect(() => {
    if (state.status === "signed-out") {
      const next = typeof window !== "undefined" ? window.location.pathname + window.location.search : path;
      router.replace(`/signin?next=${encodeURIComponent(next)}`);
    }
  }, [state.status, router, path]);
  if (state.status === "signed-in") return <>{children}</>;
  return <>{fallback}</>;
}
