# Decision 2026-10-08: The employee MCP host runs behind Tailscale Funnel

- decided: 2026-10-08 by the owner
- status: decided; values below are pending from PLX

## Decision

Tailscale Funnel, a new option. The open record's Option A was Tailscale Serve (tailnet only, the tailnet ACL limits who connects, no public endpoint). Funnel gives public HTTPS, so claude.ai and Desktop connectors, which connect from Anthropic's cloud, can reach the host. Serve is enough for Claude Code. `mcp-http` keeps its loopback bind (`operating/mcp_http_host.py`); Tailscale terminates TLS. Context: `docs/decisions/2026-10-05-employee-door.md`.

## Consequence

Funnel is public: anyone on the internet can reach the endpoint, and no tailnet ACL stands in front of it. Entra token validation in `mcp-http` (`operating.oidc_auth.verify_access_token`: JWKS signature, issuer, audience, expiry, tenant, delegated scope), then the employee resolver, is the only gate. A defect in that validation is an open door, so its tests stay at the gate.

## Who deploys

The Workbench deploys nothing and changes no PLX infrastructure (the brief forbids deployments). The owner or PLX runs the host and switches Funnel on. Until then the host is fixture-tested only.

## Values still needed

- Owner: the host, its `ts.net` name, and Funnel switched on.
- PLX tenant admin: an Entra app registration with an Application ID URI and a delegated scope; a client id with redirect `https://claude.ai/api/mcp/auth_callback`; `GroupMember.Read.All` consent.

## Evidence

- The MCP authorization spec requires HTTPS for non-loopback resource URIs. `EntraResource` refuses a non-loopback `http://` resource.
- Tailscale already serves local inference, so no new provider.

## What would reverse it

Entra refuses Claude's RFC 8707 resource parameter (AADSTS901002). Then broker the MCP behind our own authorization server, or ship the console first. If public exposure is refused, fall back to the open record's Option A (Serve) and Claude Code only.
