# Decision 2026-10-05: how a real employee reaches the product, and the live entitlement source (D1)

- decided: 2026-10-05
- status: decided; MCP side built in `operating/oidc_auth.py`, `operating/mcp_http_host.py`
  (`python3 -m operating.host mcp-http`); console sign-in not built yet; hosting decided 2026-10-08
  (`docs/decisions/2026-10-08-employee-host-hosting.md`)

## Question

Live data is allowed only to a real employee session. Today no live employee can
read anything: the Portal mints no session ticket, GroupMember.Read.All is not consented and
Portal RBAC rows are not served, so every live reader is denied `portal_access_unavailable`
(docs/reference/permissions.md, blockers 1-3). The MCP host was stdio only. The
first-employee-workflow acceptance and the live real-agent metric both
need a real employee at a served interface.

## Options

- A. Wait for the Portal: ticket minting plus RBAC relations in the reporting manifest.
  Upstream-faithful, no new auth code. Unbounded wait (Portal API 500 since 2026-10-03; Vince's
  team owns the fix).
- B. Workbench-side Entra sign-in. Console: OIDC auth code + PKCE, ID token verified against the
  tenant JWKS, minting the existing ticket (`operating/session_identity.py`
  `mint_session_ticket`). MCP: Streamable HTTP as an OAuth resource server per the MCP
  authorization spec 2025-06-18 (RFC 9728 protected-resource metadata; RFC 8707 audience
  binding). Interim entitlements: a PLX-confirmed policy file (Entra groups -> principals)
  intersected with item ACLs; Portal RBAC joins the intersection when served.
- C. Stdio host on the employee's machine with a dev ticket. Rejected: credentials on laptops;
  the secret holder can name any employee (PERMISSIONS blocker 1).

## Decision

B. The Workbench signs employees in through Entra (OIDC) and serves MCP as an OAuth resource
server: it validates Entra-issued access tokens for its own audience (signature against the
tenant JWKS, issuer, audience, expiry, tenant, delegated scope) and mints no token of its own
beyond the existing session ticket. Rights come from the PLX-confirmed policy file intersected
with item ACLs, never unioned, until Portal RBAC rows are served; then they join the
intersection. There is no fallback principal. Where production hosting lives
is an open decision; nothing is deployed.

Built (2026-10-05, isolated-fixture, live-unverified):

- `operating/oidc_auth.py`: RS256 verification in the standard library (no dependency),
  JWKS from an operator-kept file or injected keys (an unknown `kid` fails closed; no key is
  fetched: no HTTP client outside the GET-only module), claim checks, `entra_session` through
  the existing resolver, the token id checked against the durable revocation guard.
- `operating/mcp_http_host.py`: Streamable HTTP at `/mcp`, loopback only, one `MCPServer` per
  MCP session bound to one employee, every request re-authenticated and re-resolved (a revoked
  employee is refused on the next request and their sessions closed), RFC 9728 metadata and
  `WWW-Authenticate` challenges.
- `operating/agent_loop.py`: the bounded loop over the read-only ontology tools for questions
  no plan covers; claims count only with citations the run's tools returned.

## Evidence

MCP spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization (not
fetched here: egress blocked; summarised from search results). DCR is deprecated in the
2025-11-25 revision in favour of Client ID Metadata Documents
(https://den.dev/blog/mcp-november-authorization-spec/). Entra ID supports neither, as far as
known (unverified): an MCP client must use a pre-registered Entra client id.

## Live use needs

An Entra app registration for the Workbench API (Application ID URI, a delegated scope, a
pre-authorized or registered client with redirect URIs); GroupMember.Read.All consent (tenant
admin); a PLX-confirmed policy file; the tenant JWKS kept current in the configured file; an
approved reachable host (Tailscale/loopback, no public deploy). The live real-agent evaluation
runs as a named employee through B, never as an `eval:oracle` reader in the agent tier.

## What would reverse it

The Portal commits a date for ticket minting and the RBAC relations. PLX refuses a
Workbench-held sign-in app. Or Claude MCP clients cannot authenticate against Entra without
DCR/CIMD: then ship the console only and broker MCP later.
