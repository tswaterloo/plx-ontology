# Decision 2026-10-07: the live real-agent eval mints one agent ticket per session

- decided: 2026-10-07 (resolves open-live-agent-metric-reader-ticket)
- status: decided

## Context

`eval.real_agent --live` measures the real agent through the real product tools: about 24
headless Claude Code sessions a day, each on the persistent host's own MCP server
(`operating.host mcp`), plus the harness's own read sessions (the root probe, the
deterministic path and the citation re-reads). That server accepts one employee per session,
only from a signed, single-use start-up ticket (`operating/session_identity.py`: author
`agent`, the jti consumed in the state directory's durable replay guard, TTL at most 2 h;
`operating/host.py` `mcp_arguments` takes the employee only from `--session-ticket-env`).
One ticket cannot start every session. The question was where the tickets come from.

## Options

A. **The harness mints one agent ticket per session** for the named reader (`PLX_EVAL_READER`:
   JSON `uid`, `email`, `entraTenantId`, `entraObjectId`), signed with
   `PLX_WORKBENCH_SESSION_SECRET`, the secret the host verifies with.
B. **The Portal sign-in mints a batch of tickets**, handed over as a list variable. Tickets
   expire in 2 h, so an unattended daily run needs a fresh batch from a human sign-in each day.
C. **A development employee** (`--dev-configured-employee --employee-oid`). The host refuses
   it with any real source reader or capture, by design.

## Decision

Option A. It is the only option that keeps both the real product path and the existing
permission checks intact and still runs unattended:

- Every session starts `operating.host mcp` exactly as an employee's own Claude does: ticket
  verification, the durable replay guard (each ticket single-use), the employee resolver
  (policy, Portal access snapshot, Entra when configured) and the per-read trim all run
  unchanged. No `--fixture`, `--principal` or development-employee path is used.
- The harness reads (root probe, deterministic path, citation re-reads through the reader's
  own `plx.read`) go through the same host as the same reader, so the eval sees nothing the
  employee may not.
- B keeps the same path but cannot run daily without a human; C bypasses the employee gate.

## What the secret does and does not bound (corrected 2026-10-07)

- `PLX_WORKBENCH_SESSION_SECRET` is not reader-scoped: whoever holds it can sign a valid
  ticket for any employee, not only the configured reader. The ticket proves only that the
  holder of the secret named that employee.
- The bound is the host's resolver, after the signature: it refuses a reader who is not an
  active, verified employee in the policy and the Portal access snapshot (and Entra when
  configured), and trims every read to that employee's grant. It never widens a reader. A
  reader the resolver refuses as an employee (its reason one of `READER_REFUSALS` in
  `scripts/eval_real_agent.py`: not in the Portal snapshot, inactive, unverified, not an
  employee role, refused by Entra) is reported `blocked: reader_refused` before any source
  read (exit 0, the spec's `expect_blocked`). The host refuses a session identity for other
  reasons too (an invalid, expired or replayed ticket, a tenant or state mismatch); those
  are harness or host faults and the run errors (exit 1), never a blocked check.
- Owner policy: secrets carry no location restriction, so no reader-scoped
  credential is required for this eval. Holding the secret in the verifier environment is
  therefore an accepted, stated exposure, not a control: whoever can set `PLX_EVAL_READER`
  (or holds the secret) decides which employee the eval reads as.

## Consequences

- The verifier environment holds the session secret, so it can sign for any employee; the
  harness signs only for the identity in the one operator-set variable and never chooses or
  widens the reader.
- One reader per run: the report labels the access tier `single_reader` with its reader
  count (`eval.tiers.access.scope`, `readers`). The harness never adds a reader; a second
  reader (a cross-reader trim comparison) runs only when the operator names one, and the
  harness has no setting for it yet.
- Without `PLX_EVAL_READER` (or with the secret, host state or host config missing) the live
  part is blocked (`no_employee_identity`, `session_secret_missing`, ...) before any source
  read or session; the daily check is then blocked, never a pass, and the fixture parts still
  measure. These variables stay in the spec's `optional_env`.
- Which employee the eval reads as stays an operator setting. If minting must move to the
  Portal only, switching to B changes `LiveHost.ticket` in `scripts/eval_real_agent.py` alone.

## Amendment 2026-10-19: the eval principal

Owner directive (2026-10-19): when `PLX_EVAL_READER` is absent, the answer evals
(`eval.case_cards_answers` and the live parts of `eval.ask_agent` and `eval.real_agent`)
no longer block `no_employee_identity`. They read as the declared eval principal
(`operating.eval_principal`, `examples/eval_principal.json`): roles `staff:operations` and
`staff:sales`, no marking, no action path, no Portal row, no file clearance, and identities
that match no item ACL or mailbox. The host resolves it only with `--eval-principal`, from the
committed declaration alone; the session proof is `eval_principal` and every result labels
the reader `eval_principal`. It is never a person: the A6 permission proof (`denied_leaks`
with a real identity) still requires `PLX_EVAL_READER`, and the result says so. With
`PLX_EVAL_READER` set, the evals read as that employee as above. `eval.objective_agent` is
unchanged (still blocked without a reader). `eval.reader_coverage` checks that the card set
runs as the eval principal.
