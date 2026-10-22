# Decision 2026-10-02: oracle-pinning (runner translates the oracle's time into mirror generations; rows compared by relation + key)

- decided: 2026-10-02T00:40Z
- status: decided

## Context and a defect it exposes

Oracle and agent answer from the same snapshot. The oracle must not read the L1
mirror (it would share its bugs), so it pins `as_known_at.time` (replica time) and
`l1_generations` = digests of its own reads (evals/oracle/generator.py:362-363).

The scorer requires every `api://` citation to be on a pinned generation
(evals/score.py:346-387, `citation_off_generation`): `handle_generation(handle) in
{as_known_at.l1_generations values}`. Agent citations carry *mirror* capture ids, never
oracle read digests. So as written, **every live oracle item fails every expected value
with `citation_off_generation`**, whatever the agent does. The runner also tells the agent
to cite those oracle digests as "capture generations" (evals/runner.py:199-206), which no
Portal-reading tool returns. Nothing maps one to the other today.

## Options

(a) runner maps oracle time to mirror generations; (b) oracle reads the mirror's raw
pages; (c) C0 adds an `oracle_read` key and a citation-equivalence rule.

## Decision

(a), with an explicit drift check; (b) rejected (couples oracle to L1, the thing the
eval must be able to catch); (c) not needed (no C0 change).

1. The oracle item keeps its fields as today (time + its read digests, `api://` citations
   naming relation + key).
2. Before the agent runs, the runner writes a **run pin**: for each relation the item's
   expected citations name, the first completed mirror capture whose completion time is
   at or after `as_known_at.time`. The agent is told that pin; the scorer reads
   `pinned` from the run pin, not from the item.
3. **Drift check:** the runner compares the expected rows' cited fields in the pinned
   mirror version with the oracle's read (digest of the cited fields only). If they
   differ, the item is scored `pin_drift` (excluded from pass rate, counted and
   reported), never as an agent failure.
4. Row matching stays relation + key (`row_identity`), as score.py already does.

## Consequences

Small runner + scorer change. Items over volatile rows drop out as
`pin_drift`; the generator already samples closed transactions, so drift should be rare.

## How we will know this was wrong

`pin_drift` above 10% of live items over 3 runs: the mirror lags the replica too much;
then the oracle and the agent must run inside one capture window (oracle reads just
before the sync, item time = sync start).
