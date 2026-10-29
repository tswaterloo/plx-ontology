# Permissions — one entitlement layer for every source

Status 2026-10-01: the per-employee resolver exists and the case runtime
consults it (`operating/employee_permissions.py`, Portal mirror
`operating/employee_permissions_portal.py`, wiring in `operating/case_runtime.py`;
tests `operating/tests/test_employee_permissions.py`,
`operating/tests/test_employee_case_runtime.py`; example
`examples/employee_permission_policy.example.json`). Evidence: isolated-fixture
(Portal and Entra inputs) and injected-transport (SharePoint reader). It is
**not live**: two inputs are blocked (see "Blockers"), so no real request
carries an employee today and the case host still runs as one configured actor.

## Why
The `plxwb_` key is one shared DB login with column grants only, and the
Graph/BC app token sees every mailbox, site and company.
No upstream source trims per employee, so the Workbench must, **before
retrieval, ranking and caching** (GOV-09, WS-08, ING-14).

## Rule: intersect, never union
For an employee principal (Entra `tid` + `oid`) the resolver derives the
Workbench audience principals (the vocabulary `AudienceMap`, the case store,
originals and the live audiences use) and a clearance. A principal is held only
if **every** source says yes:

| Source | Input (cached, cited read) | Decides |
|---|---|---|
| (a) Portal role | `PortalAccessRead`: `app_user` role, `accessLevel`, `isActive`, `entraTenantId/entraObjectId`, `email`, MRP flags, `UserDepartmentMembership`, `UserAccessScope`, `AccessPolicyGrant` (+ `source`, `read_at`) | the principal's declared capability on its declared resource under **both** Portal paths: legacy `can(role, permission)` (+ `requireMrpModule` predicate when the rule names MRP modules) **and** canonical `resolveEffectiveAccess` (identity, explicit DENY, required scope context, actor scopes, base level). The Workbench cannot see `PLX_RBAC_MODE`, so it takes the stricter |
| (b) Entra groups | `EntraGroupRead` (`read` only when every page was read; else `unavailable` + reason) | only **restricts**: a rule naming `entra_groups` also needs membership in one. Unavailable for the recorded consent blocker → note `group_membership_unavailable`, group-gated principals withheld, the rest decided by the Portal role alone; any other unread membership, a failing provider, a non-2xx directory read or an unreadable Portal snapshot is an outage: denied with `rights_unresolved` (MC follow-ups say `audience rights unresolved`), never narrowed into data; only an unknown, mismatched or disabled user or no Portal row is a refusal. With the directory provider, a membership read refused for consent (403) derives the M365 groups of the user's joined Teams (`group_basis: joined_teams`, note `group_membership_derived:joined_teams`): a listed group is a membership, an unlisted one stays `unavailable`; a refused, partial or malformed joinedTeams listing derives none and is an outage (`rights_unresolved`, denied). Once the membership read succeeds it wins. Groups never add a principal or clearance |
| (c) SharePoint item permissions | the item's permission list, read GET-only by the reader for every original/citation read | an employee reads an item only if the list was read completely (`status: read`) and grants one of the employee's own identities (oid, Portal email, `group:<id>` only for a read membership). `partial`, `unreadable`, `not_requested`, missing → deny. Checked before any byte is downloaded |

Clearance = `min(level for accessLevel, level for LEGACY_ROLE_BOOTSTRAP[role])`
(policy `clearance_by_access_level`). Portal mirror source, read line by line:
`portal/src/lib/auth/{roles.ts, rbac-policy.ts, rbac-adapters.ts:legacyCapabilityAllowed,
effective-access.ts:resolveEffectiveAccess, mrp-access.ts, api-auth.ts:requireMrpModule,
data-key-registry.ts}` and `portal/prisma/schema.prisma` (`User`, `UserAccessScope`,
`AccessPolicyGrant`, `UserDepartmentMembership`) at
plx-customer-portal@`8da33e4571171acbb23f8f63b7a99cee89be955e`. A drift test re-reads
roles.ts / rbac-policy.ts / data-key-registry.ts at that commit and compares the
matrices. It finds the Portal clone wherever this repo is checked out:
`$PLX_PORTAL_REPO`, else a `plx-customer-portal` sibling of the checkout or of
any parent, else `~/plx-customer-portal`; with no clone holding the commit it
skips and names the variable (CI without a Portal clone runs no drift check).
It compares the matrices only; the logic of `legacyCapabilityAllowed` and
`resolveEffectiveAccess` is covered by the rule-order tests, not re-read.
Not ported: temporary elevations (they only widen; a decision needing
one denies).

Identity case: Entra object ids are GUIDs and UPNs/SMTP addresses are
case-insensitive at Entra, as in the Portal (`account-link.ts` lower-cases
`tid`/`oid`; `credential-denial.ts` trims and lower-cases emails;
plx-customer-portal@`878705b`). So `employee:` principals are detected
case- and whitespace-insensitively (`Employee:<OID>` is routed to the
resolver, never passed through as a host actor), `agent:`/`sp_` are refused
in any case, and item-ACL identities compare trimmed and lower-cased.
Matching stays exact otherwise: no prefix, suffix, domain or wildcard match.

The policy file (`plx-employee-permission-policy/v1`) maps each Workbench
principal to `{capability, department_ids, company_key, data_key, mrp_modules,
entra_groups}`, or to `portal_roles` (held only by a listed Portal role whose access
level backs it; `review:identity` = ADMIN, SUPER_ADMIN).
It is configuration, not authority. A SCOPED capability without
department ids denies (`MISSING_REQUIRED_SCOPE_CONTEXT`), as in the Portal;
owner-scoped capabilities and `agent:`/`sp_`/`employee:` principal names are
rejected.

## Refusals (default deny)
Agent or service-principal kind, an oid listed in
`service_principal_object_ids`, another Entra tenant (nothing is read), no or
failing Portal read, Portal oid/tenant ≠ the principal, unknown role, CUSTOMER
or SUPPLIER (tenants, not employees), inactive, unverified, no access level.
Every employee also holds `employee:<oid>`; it grants nothing unless an
audience names it.

## Session identity (2026-10-02)
No live data without a real employee identity; the MCP host's `eng` fallback is gone
(`operating/session_identity.py`, tests `operating/tests/test_session_identity.py`).
- Both session hosts build their runtime with `require_employee=True`: the gate then
  refuses any actor without an `employee:` principal (`employee_identity_required`)
  before any read. There is no default principal or host actor.
- MCP (`operating.mcp_server`, author kind `agent`): a verified employee from
  `--session-ticket-env` (a signed ticket with author `agent`, single use: its jti is
  consumed in the durable replay guard under the state directory), resolved through
  `--employee-policy` and `--portal-access` at startup and on every call. An expired
  ticket ends the session (`session_expired`). `--employee-oid <GUID>` authenticates
  nobody: it needs `--dev-configured-employee` and is refused with any real source
  reader, capture, foreign ontology store and on live or fixture state.
- Remote MCP (`python3 -m operating.host mcp-http`, `operating/mcp_http_host.py`, author kind
  `agent`): every request
  carries an Entra access token, verified by `operating/oidc_auth.py` (JWKS signature, issuer,
  audience, expiry, tenant, delegated scope; revoked token ids refused by the durable guard)
  and resolved through the same resolver per request. Loopback only; isolated-fixture,
  live-unverified (needs the Workbench Entra app registration and blockers 2-4 below).
- Console (`operator_ui.case_console`, author kind `human`): the identity comes from each
  authenticated request, whose bearer credential is a signed session ticket (author
  `human`), verified and resolved per request; each employee gets its own workspace.
- Ticket: `v1.<b64url claims>.<b64url HMAC-SHA256>`, the Portal upload-ticket format
  (`portal/src/lib/attachments/upload-ticket.ts@c3ffda3`), claims = the identity part of
  the Portal session user (`portal/src/lib/auth/auth.ts@c3ffda3`), secret
  `PLX_WORKBENCH_SESSION_SECRET` (no fallback), at most 12 h.
- `--portal-access` is a `plx-portal-access-snapshot/v1` export of Portal user rows
  (`PortalAccessSnapshot`, re-read per call; Prisma `User` field names as
  `rbac-adapters.ts` loads them). Without it every employee is refused.
- Fixture identities (`--fixture --principal ...`) are refused with any real source
  reader, and a state directory is labelled `fixture` or `live` on first use
  (`session-state-label.json`); unlabelled case state counts as live. The two never mix.
- Batch harness scripts (`case_workspace_live`, ...) still take
  explicit, required `--principal`s (no fallback) for the live-check specs.

## Where it is enforced
`CaseRuntime(..., employee_permissions=resolver, require_employee=...)`. Every public
method of the mirror, the originals store, the case store and the runtime routes its
`actor` through `EmployeeActorGate`:
- no `employee:` principal → refused when `require_employee` (every session host
  serving live data); otherwise unchanged (in-process harnesses, fixture sessions);
- an `employee:<oid>` principal → re-resolved on **every** call (revocation of a
  role, scope or group applies on the next read); a bare employee principal
  becomes the resolved actor; extra presented principals only narrow;
- refused: no resolver configured, several employees, any `agent:`/`sp_`
  principal mixed in, other tenant, denied entitlement.
For employees the runtime also checks (c) on `sharepoint://` and mail citations,
SharePoint original reads (`source_gate`), attach (metadata first), mail
messages and live listings. A listing with withheld items (by the reader's
audience/ACL or the employee's item permissions) reports
`completeness.complete = false`, `withheld = true` and counts only
(`employee_acl_withheld`, `employee_acl_withheld_reasons`), never a
withheld identifier. Mail permission is decided from the mailbox before any
message is read: `GraphMailReader.mailbox_owner_acl` does the directory
lookup only, so no subject, metadata or body of a mailbox the employee may not
read is fetched.

Shared and delegated mailboxes: an employee reads a mailbox only when its
derived owner ACL (object id, UPN, mail, SMTP proxies) names one of the
employee's identities. Shared-mailbox membership and delegated access
(Exchange `FullAccess`/`ReadPermission`) are Exchange mailbox permissions;
Graph v1.0 has no API that lists them (only `Get-MailboxPermission` in
Exchange Online PowerShell, which is not a Graph read and is not used here),
and the app token reads every mailbox, so it cannot stand in for the
employee. Whether such access exists therefore cannot be determined from
available data: the read is **denied and labelled
`undetermined: shared mailbox`** (the `CitationUnresolvable` text, the
`reason` of `read_live`, the listing's `withheld_reason`), not silently
denied. It is reached only after the host's own audience resolved the
mailbox, so the label says the host may read it and the employee's access is
unproven. Lifting it needs a cited source of mailbox permissions (e.g. a
Portal/Exchange-side export or a signed assertion), not a Graph widening. For the SharePoint reader
to read the item ACL at all, the live audience must list `sharepoint:*` in
`source_acl_required`; otherwise employees are denied (unknown). Caches key on
`EmployeeEntitlement.fingerprint` (policy digest + principals + clearance +
identities), never on the shared credential.

## Blockers (recorded, not done)
1. **A Portal-minted session ticket.** The hosts now require an employee and verify
   the signed session ticket format above, but the Portal does not mint it yet, so
   whoever holds `PLX_WORKBENCH_SESSION_SECRET` can still name any active employee
   (`--employee-oid` is development only and reads no real source). Needed: the Portal (or Entra sign-in,
   OIDC/MSAL delegated) issues the ticket to the signed-in user.
2. **`GroupMember.Read.All` consent (read-only) on the shared Entra app.** The
   live role list has `User.Read.All` but not
   `GroupMember.Read.All`, `Group.Read.All` or `Directory.Read.All`. The default
   provider `groups_unavailable` records
   `group_membership_unavailable:graph_permission_missing:GroupMember.Read.All`;
   group-gated principals stay withheld. Owner: Vince/PLX tenant admin. After
   consent: a GET-only provider for
   `GET /users/{id}/transitiveMemberOf/microsoft.graph.group?$select=id` (all
   pages, else `unavailable`) plus its grammar template and a live-check spec.
   `getMemberGroups`/`checkMemberGroups` are POSTs and stay refused.
   Status 2026-10-07: no longer a blocker. The directory provider reads that
   membership (`integrations/graph_directory.py`), and the live check
   read it at commit ee84ff16 (`graph.directory_entitlements` pass, 3 groups). When
   the read is refused (403), the reader derives the M365 groups from the
   user's joined Teams; Portal and MC roles
   and mailbox ACLs name no Entra group membership, so they derive none.
3. **Portal user/RBAC rows per employee.** `app_user`, `app_user_access_scope`,
   `app_access_policy_grant`, `app_user_department_membership` are not served by
   the reporting API. Options: Portal adds them to the manifest
   (operator change by Vince), or the Portal passes the caller's effective
   access in the signed assertion of blocker 1. Until then `PortalAccessRead`
   has no live provider and every employee is denied
   (`portal_access_unavailable`). File clearance (Finance, Business Central)
   reads `app_user_department_membership` from the access snapshot
   (`departmentMemberships`, PORTAL/PROJECTED rows of an active department); a
   read without it clears nothing, and the Entra department only narrows it
   (still not in services/workbench-api/manifest/allowlist.json@12db891).
4. **PLX-confirmed policy values**: Portal Department ids per principal, group
   object ids, which capability each Workbench principal stands for. The
   example policy is not a grant.
5. Not modelled: temporary elevations, SharePoint site groups (`sitegroup:<id>`),
   sharing links (`link-scope:*` never matches a person), shared/delegated
   mailbox permissions (denied as `undetermined: shared mailbox`, above),
   per-customer row scoping, per-actor column trimming of the 305 D3
   columns. The live readers keep one static live audience per
   host: an employee also needs the host's identities map to pass the reader's
   own ACL check (both must pass; intersection), so a per-request reader
   audience (`live_source_audience_document`) is a follow-up.

## Mission Control and brain: agent-side permissions (2026-10-01)
Enforced in code (`integrations/plx_hub_guard.py`, `integrations/mc.py`,
`integrations/knowledge_leads.py`; tests `integrations/tests/test_mc_write_rules.py`,
`test_mc_brain_guard.py`):
- MC is read-only by default: only the read tools of `docs/reference/mission-control.md` §3 rows 1-12, each
  call recorded in a local audit log. W-A writes (`mc_create_task`,
  `mc_checkout_task`, `mc_report_progress`, `mc_complete_task`,
  `mc_request_approval`) need the client's explicit opt-in **and** a single-use
  recorded human authorization naming the exact call, used once (durable
  local ledger, checked and recorded before I/O); the MC operator, or a
  `+tag` alias of it, can never approve its own writes. The NO rows are never
  sent. Checkout always names `petralabx/<name>`; 409 `checkout_held` is a
  typed result.
- Known limitation: an `McWriteAuthorization` is an unverified local record.
  Nothing proves a human wrote it, and `evidence_ref` is not checked. Code
  that can call the client could write one naming a configured approver. The
  authority for any business action is MC's approval gate decided by Vince or
  another admin, re-read with `approval_decision`. Binding each write
  authorization to such a read-back is a follow-up. Deleting the ledger file
  also resets single use, so keep it with the Workbench state.
- Brain/knowledge hits are untrusted leads, never business facts, trimmed per
  actor (`KnowledgeEntitlements`, fed by the resolved `EmployeeEntitlement.actor`) before
  the per-actor cache. No write-back.

Missing inputs (not blockers; the read-only path works without them):
1. **A dedicated Workbench MC service principal** (`sp_mcp_workbench`), owner
   Vince, as a PLX_MC PR: add it to `MCP_AGENT_SERVICE_PRINCIPAL_IDS`
   (`src/lib/permissions/types.ts`), a `service_principals` migration, a
   `SERVICE_GRANTS` entry (start `task.read`; later `task.create`,
   `task.progress`, `approval.request`), an `MCP_TOOL_ALLOWLISTS` entry naming
   exactly the tools we use (`src/lib/mcp/tool-allowlist.ts`, CG-07b pattern),
   a key in `plx/prod/mc/mcp-agent-keys/v1`, and our operator email on
   `PLX_MC_ALLOWED_USERS` (`docs/reference/mission-control.md` §8 R5). Today `MC_MCP_API_KEY` likely maps to
   the shared `sp_mcp_cursor` key with MC's full write bundle; our allowlist is
   the only read-only boundary until MC enforces one server-side.
2. **A `brn_` brain key at R7** (Vince): env `BRAIN_API_KEY` paired with
   `BRAIN_BASE_URL` (default `https://brain.plxcustomer.io`). Without it the
   direct brain reads keep using `VMC_API_URL` + `VMC_API_KEY`, which stops
   working for brain routes at R11 (`docs/reference/vmc-brain-tools.md` §0.4, §6 R7). `mc_search_knowledge`
   is unaffected (MC migrates its own caller).
