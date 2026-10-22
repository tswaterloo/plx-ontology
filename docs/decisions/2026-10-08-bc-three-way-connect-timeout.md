# Decision 2026-10-08: A bounded connect timeout in the guarded sender

- decided: 2026-10-08 (gap gap-ffe65f8caba0)
- status: decided
- replaces: `open-bc-three-way-run-cost-connect-timeout.md`

## Decision

Option B. `integrations/live_source_guard.urllib_sender`, the one guarded sender, opens
each HTTPS connection (TCP connect, proxy CONNECT, TLS handshake) within
`CONNECT_TIMEOUT_SECONDS` (10 s, never more than the request's timeout). The request's
timeout (30 s by default) still bounds each read of the answer. It is the same opener,
connection class and TLS context; no new egress path (`scripts/check_http_egress.py` clean).
Connection reuse (option C) stays out: it changes connection lifetime in the egress module
and is not needed to bound a failed attempt.

## Evidence

- 17 hourly `eval.bc_three_way` runs (2026-10-07/08): about 0.4 s per
  GET plus about 16 s per retried attempt; retries rose from 0 to 24 per run while GETs
  stayed at 101-133. Every outright BC failure read `retried.urlerror` (a failure before
  any status: connect, TLS, send), none `timeout`.
- Under one 30 s socket timeout a connect that never completes costs up to 30 s per
  attempt, 3 attempts per GET. With the bound it costs at most 10 s per attempt
  (`tests/test_eval_bc_three_way.py`, `test_a_failed_connect_costs_at_most_the_connect_timeout`).

## Follow-up

Keep the 540 s eval budget (`DEFAULT_BUDGET`) until the next live run. Revisit it from that
run's `cost.retries.slowest_failed_attempt_seconds`, `failed_attempt_seconds_by_cause` and
`cost.phases.*.slowest_answer_seconds`. A slowest answer near 10 s would mean the connect
bound cuts live answers' connects, and the bound must rise.

## What would reverse it

A live run where connects that succeed take longer than the bound (failed attempts at
exactly 10 s with `retried.urlerror` while the same host answers other GETs).
