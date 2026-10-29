"use client";

// Microsoft sign-in (Entra ID) in the browser: SPA authorization code + PKCE
// via MSAL. operator_api is a resource server; it only ever sees the access
// token in Authorization: Bearer. Configuration comes from the public
// GET /api/auth/config (identifiers only, no secrets).
//
// Live-unverified: exercised only through types and the sign-in page wiring;
// the first real Entra sign-in needs the app registration's redirect URI.

import {
  createStandardPublicClientApplication,
  InteractionRequiredAuthError,
  type AccountInfo,
  type IPublicClientApplication,
} from "@azure/msal-browser";
import { checkEntraConfig } from "./entra-config";
import type { EntraConfig } from "./types";

let pca: IPublicClientApplication | null = null;
let scopes: string[] = [];

export interface EntraStart {
  account: AccountInfo | null;
  /** Where to go after a sign-in redirect completed (a same-origin path), if any. */
  next: string | null;
}

export async function startEntra(raw: EntraConfig): Promise<EntraStart> {
  const cfg = checkEntraConfig(raw); // fails closed: a /common authority never reaches MSAL
  scopes = cfg.scopes;
  if (!pca) {
    pca = await createStandardPublicClientApplication({
      auth: {
        clientId: cfg.client_id,
        authority: cfg.authority,
        redirectUri: cfg.redirect_uri ?? `${window.location.origin}/signin`,
        postLogoutRedirectUri: `${window.location.origin}/signin`,
      },
      cache: { cacheLocation: "sessionStorage" },
    });
  }
  const result = await pca.handleRedirectPromise();
  const account = result?.account ?? pca.getActiveAccount() ?? pca.getAllAccounts()[0] ?? null;
  if (account) pca.setActiveAccount(account);
  return { account, next: result?.state ?? null };
}

/** A current access token for operator_api, or null (caller treats as signed out). */
export async function entraToken(): Promise<string | null> {
  if (!pca) return null;
  const account = pca.getActiveAccount();
  if (!account) return null;
  try {
    const r = await pca.acquireTokenSilent({ scopes, account });
    return r.accessToken;
  } catch (e) {
    if (e instanceof InteractionRequiredAuthError) {
      await pca.acquireTokenRedirect({ scopes, account });
    }
    return null;
  }
}

export async function entraSignIn(next: string): Promise<void> {
  if (!pca) throw Object.assign(new Error("entra not configured"), { code: "microsoft_sign_in_failed" });
  await pca.loginRedirect({ scopes, state: next, prompt: "select_account" });
}

export async function entraSignOut(): Promise<void> {
  if (!pca) return;
  const account = pca.getActiveAccount() ?? undefined;
  await pca.logoutRedirect({ account });
}
