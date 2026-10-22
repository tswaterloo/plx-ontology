# Decision 2026-10-07: Oracle strata use closed roots only in every split

- decided: 2026-10-07 (against main 488da5b7)
- status: decided

## Decision

Option 1: every split uses closed roots only (resolves open-ovs-oracle-strata-open-bc-orders).

## Evidence

- Live 1303d122: the mail stratum had 200 docs, 0 named a number, bc_looked_up 0, so the dev exception yielded nothing.
- A split-specific closure rule would be a second path.
