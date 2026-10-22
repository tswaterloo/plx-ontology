# Decision 2026-10-20: which session proof qualifies `session.employee_recorded` (A9 console sign-in)

- decided: 2026-10-20 (option 1 of the open question)
- replaces: `open-a9-console-signin-qualifying-proof` (deleted)
- paths: operator_ui/session_journal.py, operating/session_identity.py,
  verify/live-checks/session.employee_recorded.json (no change)
- status: decided; no code or check-spec change; live stays blocked (`later_ask`)

## Question

Before A9 a session qualified when the host minted its ticket. A9 asks for a real employee,
not the loopback operator. Should a host-minted ticket session still qualify beside an Entra
sign-in?

## Decision

Option 1, Entra only (the built default). A session qualifies `session.employee_recorded`
only when every journalled request carries `proof = entra_access_token`
(`PROOF_ENTRA` in `operating/session_identity.py` and `operator_ui/session_journal.py`).
Host-minted ticket sessions stay counted as `host_minted` and never qualify.

Why: anyone holding `PLX_WORKBENCH_SESSION_SECRET` can mint a `human` ticket for any
employee (a known limitation), so a
ticket cannot tell the operator from the employee. Green must prove the row's claim; an Entra access token is the employee's own proof to Microsoft.

Option 2 (Entra or host-minted) is not taken. It would keep the WS-16 run book path green
but the check could not distinguish operator from employee.

## Consequences

- No code, test or check spec changes: the built behaviour already is option 1
  (`operator_ui/session_journal.py`; fixtures in `operating/tests/test_session_identity.py`
  and `tests/test_session_employee_recorded.py`, isolated-fixture evidence only).
- `verify/live-checks/session.employee_recorded.json` is unchanged and still reads
  `blocked` on `entra_app_registration_absent` until a human supplies the registration.
- The Entra registration stays the later ask `console-entra-app-registration`: tenant id,
  Application ID URI (audience), delegated scope, console redirect URI and tenant JWKS, in a
  `plx-entra-resource/v1` file named by `PLX_CONSOLE_ENTRA_RESOURCE` and passed to
  `operator_ui.case_console --entra-resource`. None of these values is guessed or committed.
- Update 2026-10-21: the file is committed as
  `operating/entra_resource.json`. Sign-in uses the existing app registration, not a
  separate app (acceptable: `oidc_auth` refuses app-only tokens and requires the delegated
  scope). Its identifiers, not secrets, as relayed from the meeting with Vince and UAT:
  - Tenant ID `dc28356c-e440-4a9e-b8e6-e40967bfee06`
  - Client ID `8ea4aa90-4223-4327-b004-cd7658f9be63`
  - App ID URI `api://8ea4aa90-4223-4327-b004-cd7658f9be63`
  - Scope `api://8ea4aa90-4223-4327-b004-cd7658f9be63/Workbench.Use` (the token's `scp`
    carries `Workbench.Use`)
  - Assignment required; Vince and the owner assigned.

  The resource URI (the host's URL) and the tenant's JWKS file stay a typed
  `{"unset": "console-entra-app-registration"}` until the Azure VM hosts the console; then
  the owner sets `PLX_WORKBENCH_STATE_DIR` there and asks UAT for the console redirect URI
  (the later ask also keeps the separate sign-in app question). Until then the check reads
  `entra_app_registration_incomplete` (later_ask). Nothing is guessed.
