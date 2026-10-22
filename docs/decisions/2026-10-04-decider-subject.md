# 2026-10-04 decider != subject: sub/act on every intent, enforced at record_decision

## Question
Separation of duties: the human who decides an approval must never be the employee who is
the subject of the request (the employee on whose behalf the agent acts). Mission Control
records only our operator as `requestedBy` (PLX_MC `src/lib/mcp/approval-actions.ts:37` @
`f4ed0b1`; later_ask `mc-approval-requestedby-actor`), so MC's own `separation_of_duties`
check (decider != requestedBy) never sees the employee. `ontology.core.Actor` carries
principals only. Nothing in the goal-2 path enforced decider != subject.

## Decision
1. **Where `sub`/`act` come from.** Only a verified `SessionIdentity`
   (`operating/session_identity.py`). `SessionIdentity.identities` holds the subject
   employee's own identities: the Entra object id and the Portal email the resolver matched
   (`EmployeeEntitlement.source_identities` without `group:` entries), plus a ticket's
   email. `SessionIdentity.intent_identity(actor)` returns
   `{schema: plx-intent-identity/v1, sub: {principal, kind, proof, identities}, act: [{kind,
   channel, proof}]}`. It refuses an expired session and an actor other than the session's
   (`session_actor_mismatch`). The module-level `intent_identity(session, actor)` refuses
   anything that is not exactly a `SessionIdentity` (a mapping from tool arguments or
   source text can never stand in), and returns `sub`/`act` None for no session (an
   in-process harness).
2. **Recorded on the intent.** `ActionPath.register(..., session=)` pins `sub` and `act` in
   `ActionIntent` (the first, hash-chained `intent.registered` event). `plx.act` gets the
   session from its host (`ActTool(session=)`, passed by `MCPServer`), never from its
   arguments; its input schema has no identity field. A repeat registration keeps the
   first registration's subject. Intents recorded before this change have no `sub`:
   unknown subject.
3. **Enforced at the approval boundary.** `ActionPath.record_decision`, after the existing
   checks (human email, not `sp_`/`agent:`, not the operator, `requestedBy` our operator,
   exact reason, decision time) and before the MC admin/owner roster check:
   - no recorded subject identity -> `subject_unknown` (fail closed);
   - `canonical_email(decider)` in the subject's identities -> `decider_is_subject`.
   Both read the subjects (and the gate) from the state loaded inside the write that
   commits the decision, never from the snapshot taken before the MC reads, so a
   co-request recorded during those reads is checked.
   Either is a `gate.decision` with `authorizes: false`, so the intent is
   `gate_not_authorizing` and can never dispatch (parked). `next_safe_step` says why:
   for `decider_is_subject` a new human-approved request decided by a different MC
   admin/owner; for `subject_unknown` nothing on this intent (a new proposal registered
   from an employee session).
4. **Workbench-internal actions.** `ActionPath.execute_internal(..., session=)` runs the
   catalog's internal action (identity link review, correction, follow-up, draft) and
   appends `sub`/`act` with the execution record to an append-only `internal_action`
   table; `plx.act` propose (internal) goes through it.
5. **Fixture sessions.** `fixture_session(..., subject_email=)` lets a fixture host name a
   fixture subject (host configuration, never a tool argument). Without it, the subject is
   unknown and no decision authorizes.

## Follow-up (after e3873518)
1. Every registering session is a subject: a repeat registration whose subject is not yet
   recorded appends `intent.co_requested` (bound, hash-chained). `decider_is_subject` checks
   the union; any unknown co-requester makes the subject unknown (fail closed).
2. An unknown-subject intent never gets an MC gate: `prepare_gate` refuses `subject_unknown`
   (after the production guard), `approve_call` and `request_gate` refuse it too, and the
   next safe step is `needs_session_subject` ("needs a session subject").
3. The internal-action audit row (`started`, with sub/act) is committed before the
   case-store write (a separate SQLite file, so no shared transaction); `executed` /
   `refused` follow. A crash leaves the attempt as `unknown`, never lost.
4. The per-call approver is checked against the subjects (`call_approver_is_subject`) when
   recorded and again before the send.

## Options considered
(a) Explicit session object passed by the host (chosen): the only source is the verified
session; no tool argument can carry it. (b) Derive the subject from the `employee:<oid>`
principal of the `Actor` plus a resolver held by `ActionPath`: no `act`/channel, and fixture
actors have no employee, so every fixture test would park. (c) Rely on MC: MC records only
the operator as requester, so it cannot know the subject (later_ask open upstream).

## Residual risks
- MC labels the decider by Entra session email; the subject is matched on the Portal email,
  the Entra object id and a ticket email, compared after `canonical_email` (lower case,
  `+tag` stripped). A different alias of the same person (another SMTP proxy address) is not
  caught. MC does not expose the decider's object id.
- A co-request that arrives after the gate was decided cannot undo an authorizing decision
  already recorded; it applies to every later decision and request.
