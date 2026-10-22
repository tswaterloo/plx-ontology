# 2026-10-04 Goal-2 identity model: sub/act on every step, SoD on one path, oid-matched

Status: **decided 2026-10-04** (option A, decision items 1-6 below). Item 5
(MC upstream) stays parked on later_ask `mc-approval-requestedby-actor`; no MC write.

Builds on `2026-10-04-decider-subject.md` (landed e3873518, f2b701e8, 70753f6a) and
3dd80032 (subject check moved inside the committing write).

## Question
Before a real dispatcher, what identity model guarantees that (a) every
intent, gate request, decision and dispatch records `sub` and `act`; (b) a decider is never a
subject, across co-requests and restarts; (c) agents are never principals; (d) it fits MC
(requestedBy = operator only) and Portal session identity; (e) no live data without a real
employee?

## What is already true on origin/main
- `sub`/`act` come only from a verified `SessionIdentity` (`operating/session_identity.py:334-353`,
  `:453-462`) and are pinned on the first, hash-chained `intent.registered` event; co-requests
  append `intent.co_requested` (`operating/action_path.py:936-945`); unknown subject fails
  closed (`subject_identities`, `:2109-2116`); `prepare_gate`/`approve_call`/`request_gate`
  refuse `subject_unknown`; `record_decision` refuses `decider_is_subject`.
- State is folded from the SQLite event log on every load, so a restart cannot lose a subject.
- No `eng` fallback remains in non-test code (`session_identity.py:3-4`; grep finds none).

## Gaps (what "not end to end" means)
1. `act` names no actor: `[{kind, channel, proof}]` (`session_identity.py:352`). RFC 8693 §4.1
   `act` identifies the acting party (and nests for a delegation chain); ours cannot tell two
   agent sessions apart.
2. Only registration carries `sub`/`act`. Every later event records `actor_label(actor)` =
   sorted principals (`action_path.py:675`, `operating/cases.py:163-165`); `record_decision`,
   `dispatch` take no session (`action_path.py:1365,1411`), so who triggered a decision poll or
   a dispatch is not attributable beyond principals.
3. SoD compares an email: MC's decider label is the session email, falling back to oid
   (PLX_MC `src/lib/routing/mutations/actors.ts:51-60` @ 073f192); MC knows the oid
   (`actorId`, `:67`) but stores only the label (`src/lib/compliance/approvals.ts:111` @ 073f192).
   Subject identities are oid + Portal email; an Entra alias (UPN != Portal email) passes.
4. SoD logic exists in three places: `_subject_refusal` (`action_path.py:2127`), the
   `decide()` closure (candidate 3dd80032), and nothing in `dispatch`, which relies on the
   state machine (co-request withdrawal) rather than re-checking on the dispatch write.
5. `ApprovalRecord` (`action_path.py:300-313`) does not record which subject set it was
   checked against.
6. `TicketReplayGuard` is in memory (`session_identity.py:216-232`): a restart forgets used
   jtis within the 2 h TTL (identity, not SoD).

## Options
A. Keep `Actor` = read-authorization principals; carry `sub`/`act` as a separate attribution
   object from `SessionIdentity`, required on every mutating `ActionPath` step (chosen).
B. Put `sub`/`act` into `ontology.types.Actor` (`ontology/types.py:53`). Rejected: `Actor` is
   the ACL/read key used across the read path; adding the agent makes it look like a principal
   (violates c) and mixes two concerns.
C. Set `X-MC-Operator-Email` to the subject employee per intent so MC's own
   `separationOfDutiesViolation` (`approvals.ts:70-78`) blocks them. Rejected: impersonation
   (operator email is an admission gate on `PLX_MC_ALLOWED_USERS` and becomes reporter /
   accountableOwner); cannot represent several co-requesting subjects; the agent, not
   the employee, made the request.
D. Use Portal `Approval` (`portal/prisma/schema.prisma:5411` @ 82549dd). Rejected: document
   approvals, `approverId` only, no requester/subject; MC owns runtime gates.
E. A real RFC 8693 token-exchange STS between Portal, MC and the Workbench. Rejected for now:
   neither upstream issues or accepts exchanged tokens; adopt the claim shape only.

## Decision
1. **Data.** `intent_identity` v2 (`plx-intent-identity/v2`):
   `sub = {principal, kind, proof, identities: [entra_oid, portal_email, ...], oid}`;
   `act = [{kind: agent|human|service, channel, client, session}]` where `client` is the host id
   (and, once issued, MC service principal `sp_mcp_workbench`) and `session` is
   `sha256(jti or host session nonce)`. Never a principal; never from tool arguments.
2. **Every step.** `ActionPath._append` takes `act` and stores it in the hashed event body;
   `prepare_gate`, `approve_call`, `request_gate`, `record_decision`, `dispatch`, `verify`,
   `execute_internal` take `session=` (actor derived from `session.actor`; mismatch refused).
   A background dispatcher uses a declared service `act` (`{kind: service, client}`), never a
   principal. `sub` stays bound by `binding_sha256` to the first event.
3. **One SoD path.** `_sod_refusal(state, person, role)` (generalising `_subject_refusal`) is
   called inside the committing write by `approve_call` (approver), `record_decision`
   (decider; candidate 3dd80032) and `dispatch` (decider of the standing decision, on the
   dispatch write's fresh state). `ApprovalRecord` gains `subjects_sha256` (sorted canonical
   subject identities checked).
4. **oid match.** `ApproverRoster` entries carry `{email: {role, oid}}` (person-supplied with
   `source_ref`); the decider's roster oid is compared with the subjects' oids as well as the
   email. A roster member without an oid cannot authorize (`decider_oid_unknown`).
5. **Parked on MC (upstream ask, extends later_ask `mc-approval-requestedby-actor`).**
   `mc_request_approval{taskId, reason, onBehalfOf: [{oid, email}]}` → gate stores
   `requestedBy` (operator), `requestedVia` (service principal id), `requestedFor` (subject oids);
   `decideApprovalGate` refuses a decider oid in `requestedFor`; the gate stores
   `decidedByActorId` (oid). When available, `record_decision` additionally requires MC's
   `requestedFor` to equal the intent's subject set (`gate_subjects_stale` otherwise).
6. Unchanged: no live data without an employee session (`check_runtime`), Portal tickets
   mirror NextAuth `entraObjectId` (`portal/src/lib/auth/auth.ts:382-383` @ 82549dd) and are
   not minted yet (PERMISSIONS blocker 1).

## Build slice (on top of 3dd80032)
- `session_identity.py`: v2 `intent_identity` with `act.client`/`act.session`, `sub.oid`.
- `action_path.py`: `act` in `_append`; `session=` on the steps above; `_sod_refusal` used by
  approve_call / record_decision / dispatch; `subjects_sha256` on `ApprovalRecord`.
- `goal2_mc_approval.py`: `ApproverRoster` oid per member.
- Acceptance tests (`operating/tests/test_action_path.py`):
  1. every event after registration carries `act` with `client` and `session`; a session of
     another actor is refused on each step;
  2. decider whose email differs from the subject's but whose roster oid equals the subject
     oid -> `decider_is_subject`;
  3. co-request recorded between the MC read and the decision write -> `decider_is_subject`
     (candidate test kept);
  4. a decision event forged/injected as authorizing for a subject decider (state built by
     direct event append) -> `dispatch` refuses on the fresh write, zero owner writes;
  5. close and reopen the store between co-request and decision -> still refused (restart);
  6. `act` never appears in `Actor.principals` nor any ACL check; tool arguments with
     `sub`/`act` keys are ignored/refused;
  7. pre-v2 intents (no `act.session`) read as `act` unknown and still dispatch only if the
     subject is known (no silent upgrade).

## What would reverse this
- MC ships `requestedFor` + oid SoD natively: the Workbench check stays as an independent
  re-check, the roster oid becomes redundant.
- Portal D16 business-run approvals become the gate for Portal writes: the same `sub`/`act`
  fields move to that request; SoD path unchanged.
- Evidence that MC decider emails always equal Portal emails (one SMTP identity per person)
  would make the oid roster optional.

## Residual risks
- Roster oids are person-supplied; a wrong oid defeats item 4 until MC exposes the decider oid.
- Recorded owner policy: an oid the host reads GET-only from Graph `/users` by the roster UPN is a valid source; it stays in the host state (never pinned into the committed roster), each approval records it as `graph_users` with its read time, and it counts only while at most 24 h old at that check.
- An authorizing decision already dispatched cannot be undone by a later co-request.
- In-memory `TicketReplayGuard` loses state on restart (separate slice: persist used jtis).

## As built
- `operating/session_identity.py`: `plx-intent-identity/v2`. `sub` gains `oid` (the employee's
  Entra object id; a fixture subject's only when the host configures `subject_oid`). `act` is
  `[{kind, channel, client, session}]`: `client` is the host id (`employee_session` /
  `fixture_session` `client=`, default `plx-workbench-mcp` / `plx-workbench-console`),
  `session` is SHA-256 of the ticket `jti`, or of a 32-byte nonce drawn per `SessionIdentity`
  for a configured or fixture session. `act.proof` is gone (the proof is on `sub`).
  `TicketReplayGuard(path)` keeps used and revoked jti digests in SQLite, so a restart within
  the 2 h TTL forgets nothing. No host constructs a guard yet (`operating/mcp_server.py` and
  `operator_ui/case_console.py` do not), so wiring it is a host change outside this slice.
- `operating/action_path.py`: `_append(..., act)` stores `act` in the hashed body of every
  event (the fold strips it from the step payload and exposes it per `history` entry);
  `prepare_gate`, `approve_call`, `request_gate`, `reconcile_gate`, `record_decision`,
  `dispatch`, `read_back`, `escalate` take `session=` (another actor's session, an expired
  one or a mapping is refused before any read or write). `session=None` records `act: null`
  (unknown), the same rule as `register` without a session (an in-process harness); making it
  mandatory would break every in-process caller without adding a check, so the guarantee is
  "a host that has a session records it on every step". `_sod_refusal(state, role, email,
  oids)` replaces `_subject_refusal` and the `decide()` subject checks; it runs inside the
  committing write of `approve_call`, `request_gate`, `record_decision` and `dispatch`.
  Dispatch withdraws a standing decision whose decider is a subject (recorded `decider_oid`
  or the roster's current oid) or whose intent has no known subject:
  `approval_withdrawn:decider_is_subject|subject_unknown`, nothing sent. `ApprovalRecord` gains
  `decider_oid` and `subjects_sha256`; `public_view` gains `act_known` (false for pre-v2).
- `operating/goal2_mc_approval.py`: `ApproverRoster` entries are `{email: {role, oid}}` (a plain
  role string is refused); `oid_of(email)`; a member without an oid cannot authorize
  (`decider_oid_unknown`).
- `operating/action_tools.py`: `plx.act verify` passes the host session to `record_decision`,
  `reconcile_gate` and `read_back`. Its arguments still carry no identity (`sub`/`act`/
  `session` are `unknown_arguments`).
- Not built: a declared service `act` for a background dispatcher (none exists yet).
- Source pins: PLX_MC `src/lib/routing/mutations/actors.ts:51-67` and
  `src/lib/compliance/approvals.ts:70-78,111` @ 073f192 (decider label = email, oid known as
  `actorId` but not stored).

## Decision 2026-10-04: session required on every mutating step
Open question: "As built" above made `session=None` record
`act: null`. Decided: every mutating `ActionPath` step carries the acting
session; a missing session fails closed with an explicit refusal and is never recorded as
`act` null.

As built (base 357a87cc):
- `session_identity.intent_identity(None, actor)` raises
  `SessionIdentityRefused("session_required")` (no unknown-identity fallback). `ActionPath.register`, `prepare_gate`, `approve_call`,
  `request_gate`, `reconcile_gate`, `record_decision`, `dispatch`, `read_back`, `escalate`
  raise `ActionPathError("session_required")` and `execute_internal` raises
  `ActionRefused("session_required")` before any read, MC call or write. `_append` itself
  refuses an empty `act`, so no current code path writes an event with `act` null. Events
  written before v2 (`act` null) still fold and verify (`chain_ok`) and read as `act` unknown.
- Hosts: `plx.act` (`operating/action_tools.py`) without a host session gets
  `action_refused:session_required` from `ActionPath` on `propose` (after the case-visibility and
  guard reads, which keep their own refusals) and on `verify` before any MC or owner read;
  `status` stays a read.
  `operating/mcp_server.py` and `operator_ui/case_console.py` already pass their session (the
  console only lists intents); neither needed a change. The isolated harness
  (`operating/goal2_exec.py`) passes its explicit fixture `SESSION` on every step.
- Call-approver SoD by oid: `call_approvers` may be `{email: oid}`; the
  approver's oid is the configured one, else the `ApproverRoster`'s. `approve_call` refuses an
  approver whose oid is a subject's (`call_approver_is_subject`) or who has no known oid
  (`call_approver_oid_unknown`); `CallApproval` records `approver_oid`; `request_gate` re-checks
  the recorded and the configured oid on its write (a pre-oid approval with none configured is
  `gate.request_refused: call_approver_oid_unknown`, nothing sent).
- Dispatch upgrade window: a standing decision with no recorded `decider_oid`
  and no roster oid for its decider is withdrawn on the dispatch write
  (`approval_withdrawn:decider_oid_unknown`), nothing dispatched.
- Callers outside this slice changed only to pass a fixture session: `evals/injection/run.py`,
  `tests/test_injection_guard.py`, `operating/tests/test_capability_guards.py`,
  `operator_ui/tests/case_fixtures.py`.

## Decision 2026-10-11: one age rule for Graph oids
`request_gate`, `send_call` and `dispatch` re-checked the oid recorded
with the approval or decision beside the roster's current one and passed on either, so a
`graph_users` oid recorded at 23 h could authorize at 47 h.

As built:
- One helper, `ActionPath._standing_oid_refusal`, at all three sites. A recorded `graph_users`
  oid (or one whose provenance this build does not know) stands only while the roster's copy
  is fresh (`approver_oid_fresh` through `ApproverRoster.oid_of`) and names the same oid:
  else `<role>_oid_unverified` or `<role>_oid_changed`. Committed oids, records written
  before provenance and pre-v2 records keep the old rule (the recorded or the current oid).
- Load-only. Graph `/users` is read at host start only (`operating/host.py`
  `resolve_approvers`). A long-running host whose copy of an approver's oid is past 24 h
  re-reads the host state through the same load (`ActionApprovers.reload`, passed as
  `approver_roster_reload`; `resolved_approver_oids` stays the one reader), so a host start
  elsewhere refreshes it; when nobody re-resolved, the approver is unverified until one does.
- `resolved_approver_oids` takes no entry read after the moment it reads the file.
- Unchanged by the recorded owner policy: every `unreadable:*` but `record_invalid`, a 401
  included, stays transient and keeps the earlier oid within its original 24 h; a failed
  state-file write leaves the summary `state_file: write_failed`, status `unverified`.
