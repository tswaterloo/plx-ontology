// Fail-closed check of the Entra sign-in configuration from /api/auth/config.
// The authority must name PLX's tenant: a multi-tenant authority (/common,
// /organizations, /consumers) would let any Microsoft account start sign-in.

import type { EntraConfig } from "./types";

const MULTI_TENANT = new Set(["common", "organizations", "consumers"]);
const TENANT = /^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[a-z0-9-]+(?:\.[a-z0-9-]+)+)$/;

export class EntraConfigError extends Error {
  readonly code = "microsoft_sign_in_misconfigured";
}

export function checkEntraConfig(cfg: EntraConfig): EntraConfig {
  let url: URL;
  try {
    url = new URL(cfg.authority);
  } catch {
    throw new EntraConfigError("authority is not a URL");
  }
  if (url.protocol !== "https:" || url.host !== "login.microsoftonline.com" || url.search || url.hash) {
    throw new EntraConfigError("authority must be https://login.microsoftonline.com/<tenant>");
  }
  const parts = url.pathname.split("/").filter(Boolean);
  const tenant = (parts[0] ?? "").toLowerCase();
  if (parts.length < 1 || parts.length > 2 || (parts.length === 2 && parts[1] !== "v2.0")) {
    throw new EntraConfigError("unexpected authority path");
  }
  if (!tenant || MULTI_TENANT.has(tenant) || !TENANT.test(tenant)) {
    throw new EntraConfigError("authority must name the tenant");
  }
  if (tenant !== (cfg.tenant_id ?? "").toLowerCase()) {
    throw new EntraConfigError("authority tenant differs from tenant_id");
  }
  if (!cfg.client_id || !Array.isArray(cfg.scopes) || cfg.scopes.length === 0) {
    throw new EntraConfigError("client id and scopes are required");
  }
  return cfg;
}
